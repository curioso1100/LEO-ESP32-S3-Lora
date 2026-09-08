# =========================================================================
# MÓDULO: red.py  -  Funciones comunes WiFi, NTP
# =========================================================================

import network
import time
import json
import machine
import gc

from logger import log_info, log_debug, log_warn, log_persistente
from placa import led_patron_error

NTP_SERVERS = (
    "pool.ntp.org",
    "0.pool.ntp.org",
    "1.pool.ntp.org",
    "2.pool.ntp.org",
)

# =========================================================================
# MAPEO DE ESTADOS WiFi - MicroPython ESP32
# =========================================================================
_ESTADOS_WIFI = {
    1000: "IDLE",
    1001: "CONECTANDO",
    1010: "GOT_IP",
    200:  "BEACON_TIMEOUT",
    201:  "NO_AP_FOUND",
    202:  "WRONG_PASSWORD/AUTH_FAIL",
    203:  "ASSOC_FAIL",
    204:  "HANDSHAKE_TIMEOUT",
    211:  "NO_AP_AUTHMODE_THRESHOLD",
    212:  "NO_AP_RSSI_THRESHOLD",
    # --- Reason codes ESP-IDF (desconexión) ---
    1:    "UNSPECIFIED",
    2:    "AUTH_EXPIRE",
    3:    "AUTH_LEAVE",
    4:    "ASSOC_EXPIRE",
    5:    "ASSOC_TOOMANY",
    6:    "NOT_AUTHED",
    7:    "NOT_ASSOCED",
    8:    "ASSOC_LEAVE",
    9:    "ASSOC_NOT_AUTHED",
    10:   "DISASSOC_PWRCAP_BAD",
    11:   "DISASSOC_SUPCHAN_BAD",
    12:   "IE_INVALID",
    13:   "MIC_FAILURE",
    14:   "4WAY_HANDSHAKE_TIMEOUT",
    15:   "GROUP_KEY_UPDATE_TIMEOUT",
    16:   "IE_IN_4WAY_DIFFERS",
    17:   "GROUP_CIPHER_INVALID",
    18:   "PAIRWISE_CIPHER_INVALID",
    19:   "AKMP_INVALID",
    20:   "UNSUPP_RSN_IE_VERSION",
    21:   "INVALID_RSN_IE_CAP",
    22:   "802_1X_AUTH_FAILED",
    23:   "CIPHER_SUITE_REJECTED",
    24:   "INVALID_PMKID",
    25:   "BEACON_TIMEOUT",
    26:   "NO_AP_FOUND",
    27:   "AUTH_FAIL",
}

# Estados que indican progreso (no son errores definitivos)
_ESTADOS_PROGRESO = (1000, 1001)

# Estados/Reason codes que indican error definitivo en esta ronda.
# Cualquier código < 1000 es un reason code del ESP-IDF (error de capa inferior).
_ESTADOS_ERROR = (201, 203, 204, 211, 212)


def _nombre_estado(codigo):
    return _ESTADOS_WIFI.get(codigo, "DESCONOCIDO({})".format(codigo))


def _es_reason_code_error(codigo):
    # Devuelve True si el código es un reason code de error del ESP-IDF.
    # Cualquier valor entre 1 y 255 (reason codes) o los errores conocidos >=200
    return (1 <= codigo <= 255) or (codigo in _ESTADOS_ERROR)


# =========================================================================
# WIFI
# =========================================================================

def _apagar_todo_wifi():
    # Apaga tanto STA como AP para evitar conflictos de modo dual
    for modo in (network.STA_IF, network.AP_IF):
        try:
            w = network.WLAN(modo)
            if w.active():
                try:
                    w.disconnect()
                except Exception:
                    pass
                time.sleep_ms(500)
            try:
                w.active(False)
            except Exception:
                pass
        except Exception:
            pass
    gc.collect()


def _reset_wifi_completo(wlan):
    _apagar_todo_wifi()

    # CRITICO: el PHY WiFi del ESP32 necesita >=3s para liberar buffers tras un reason code de error (AUTH_EXPIRE, ASSOC_EXPIRE, etc.)
    time.sleep_ms(3500)

    # Volver a encender solo STA
    try:
        wlan.active(True)
    except Exception:
        pass

    # Desactivar power management. El PM del ESP-IDF puede interferir con el timing del handshake WPA2 tras un fallo previo.
    try:
        wlan.config(pm=network.WLAN.PM_NONE)
    except Exception:
        pass

    # Esperar a que el interfaz esté realmente activo
    intentos = 0
    while not wlan.active() and intentos < 15:
        time.sleep_ms(300)
        try:
            wlan.active(True)
        except Exception:
            pass
        intentos += 1

    # Delay aumentado a 4s para estabilización del PHY tras encender, especialmente tras reason codes de error.
    time.sleep_ms(4000)


def _intentar_conexion(wlan, ssid, password, max_intentos, etiqueta=""):
    # Intenta conectar y espera hasta max_intentos ciclos de 1s. Retorna (conectado: bool, estado_final: int)

    if not wlan.active():
        log_warn("WIFI", "{}Interfaz no activo. Abortando conexion.".format(etiqueta))
        return False, 1000

    # Desactivar reconexion automatica del driver ANTES de cualquier otra cosa
    try:
        wlan.config(reconnects=0)
    except Exception:
        pass

    # Asegurar que no hay conexion/colgada pendiente del IDF. Aumentado a 1s para que el driver libere completamente la asociación anterior.
    try:
        wlan.disconnect()
        time.sleep_ms(1000)
    except Exception:
        pass

    wlan.connect(ssid, password)

    # Espera inicial para que el proceso de asociacion comience
    time.sleep_ms(2500)

    intentos = 0
    ultimo_estado = -1
    errores_consecutivos = 0

    while not wlan.isconnected() and intentos < max_intentos:
        estado = wlan.status()
        if estado != ultimo_estado:
            ultimo_estado = estado
            log_debug("WIFI", "{}Intento {}/{}  Estado: {}".format(
                etiqueta, intentos + 1, max_intentos, _nombre_estado(estado)))

            # Detectar reason codes del ESP-IDF (< 1000) como errores definitivos y abortar inmediatamente. El driver está en estado corrupto.
            if _es_reason_code_error(estado):
                errores_consecutivos += 1
                if errores_consecutivos >= 1:
                    log_warn("WIFI", "{}Reason code ESP-IDF detectado ({}). Abortando ronda.".format(
                        etiqueta, _nombre_estado(estado)))
                    break
            elif estado in _ESTADOS_ERROR:
                errores_consecutivos += 1
                if errores_consecutivos >= 2:
                    log_warn("WIFI", "{}Error definitivo detectado ({}). Abortando ronda.".format(
                        etiqueta, _nombre_estado(estado)))
                    break
            elif estado not in _ESTADOS_PROGRESO and estado != 1010:
                errores_consecutivos += 1
                if errores_consecutivos >= 2:
                    log_warn("WIFI", "{}Estado anomalo persistente ({}). Abortando ronda.".format(
                        etiqueta, _nombre_estado(estado)))
                    break
            else:
                errores_consecutivos = 0

        time.sleep(1)
        intentos += 1

    return wlan.isconnected(), wlan.status()


def conectar_wifi():
    gc.collect()

    try:
        with open("config.json", "r") as cf:
            c = json.load(cf)
            ssid         = c["wifi_ssid"]
            password     = c["wifi_pass"]
            max_intentos = int(c.get("seguridad_hardware", {}).get("max_intentos_wifi", 10))
    except Exception as e_cfg:
        log_warn("WIFI", "No se pudo leer config.json: {}".format(e_cfg))
        log_persistente("WIFI", "No se pudo leer config.json: {}".format(e_cfg), "WARN")
        return False

    if not ssid:
        log_warn("WIFI", "SSID vacio. Abortando.")
        log_persistente("WIFI", "SSID vacio. Abortando.", "WARN")
        return False

    log_debug("WIFI", "SSID='{}' | PASS_len={}".format(ssid, len(password)))

    # Limpieza agresiva en POWERON antes de crear el objeto principal. Apagar tanto STA como AP para evitar estado dual del IDF.
    if machine.reset_cause() == machine.PWRON_RESET:
        log_debug("WIFI", "POWERON detectado. Limpiando estado WiFi persistente...")
        _apagar_todo_wifi()
        log_debug("WIFI", "Esperando estabilizacion RF (POWERON)...")
        # 8s para asegurar calibracion PHY tras encendido en frio
        time.sleep_ms(8000)

    # Crear objeto STA. En MicroPython es un singleton, pero lo creamos limpio tras apagar todo.
    wlan = network.WLAN(network.STA_IF)

    # ============================================================
    # RONDA 1: reset completo + conexion
    # ============================================================
    log_debug("WIFI", "Ronda 1: reset completo del interfaz STA...")
    gc.collect()
    _reset_wifi_completo(wlan)

    conectado, estado_final = _intentar_conexion(wlan, ssid, password, max_intentos, "R1 ")

    if conectado:
        log_info("WIFI", "Conectado (Ronda 1)! IP: {}".format(wlan.ifconfig()[0]))
        return True

    msg_r1 = "Ronda 1 fallo. Estado final: {}".format(_nombre_estado(estado_final))
    log_warn("WIFI", msg_r1)
    log_persistente("WIFI", msg_r1, "WARN")

    # ============================================================
    # RONDA 2: apagado total + encendido + reconexion
    # ============================================================
    log_debug("WIFI", "Ronda 2: reset suave con objeto nuevo...")
    gc.collect()
    _apagar_todo_wifi()
    # Aumentado a 8s. El PHY necesita tiempo tras reason codes.
    time.sleep_ms(8000)

    wlan = network.WLAN(network.STA_IF)
    try:
        wlan.active(True)
        wlan.config(pm=network.WLAN.PM_NONE)
    except Exception:
        pass
    time.sleep_ms(4000)

    conectado, estado_final = _intentar_conexion(wlan, ssid, password, max_intentos, "R2 ")

    if conectado:
        log_info("WIFI", "Conectado (Ronda 2)! IP: {}".format(wlan.ifconfig()[0]))
        return True

    msg_r2 = "Ronda 2 fallo. Estado final: {}".format(_nombre_estado(estado_final))
    log_warn("WIFI", msg_r2)
    log_persistente("WIFI", msg_r2, "WARN")

    # ============================================================
    # RONDA 3: reset ultra-agresivo con apagado total
    # ============================================================
    log_debug("WIFI", "Ronda 3: reset ultra-agresivo...")
    gc.collect()
    _apagar_todo_wifi()
    # 10s de descanso. El driver ESP-IDF necesita tiempo para limpiar los reason codes de la memoria interna del PHY.
    time.sleep_ms(10000)

    wlan = network.WLAN(network.STA_IF)
    try:
        wlan.active(True)
        wlan.config(pm=network.WLAN.PM_NONE)
    except Exception:
        pass
    time.sleep_ms(4000)

    conectado, estado_final = _intentar_conexion(wlan, ssid, password, max_intentos, "R3 ")

    if conectado:
        log_info("WIFI", "Conectado (Ronda 3)! IP: {}".format(wlan.ifconfig()[0]))
        return True

    msg_r3 = "Ronda 3 fallo. Estado final: {}".format(_nombre_estado(estado_final))
    log_warn("WIFI", msg_r3)
    log_persistente("WIFI", msg_r3, "WARN")

    # Apagar limpiamente antes de salir
    _apagar_todo_wifi()
    led_patron_error()
    return False


def apagar_wifi():
    _apagar_todo_wifi()
    gc.collect()
    log_debug("WIFI", "Interfaz WiFi apagada")

# =========================================================================
# NTP
# =========================================================================

def sincronizar_ntp():
    import ntptime
    if hasattr(ntptime, "timeout"):
        ntptime.timeout = 3

    for host in NTP_SERVERS:
        try:
            ntptime.host = host
            log_debug("NTP", "Intentando con {}".format(host))
            ntptime.settime()
            gc.collect()
            return True, host
        except Exception as e:
            log_debug("NTP", "Fallo con {}: {}".format(host, e))

    gc.collect()
    return False, None

