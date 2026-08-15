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
# MAPEO DE ESTADOS WiFi - MicroPython ESP32 (códigos actuales)
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
}

# Estados que indican progreso (no son errores definitivos)
_ESTADOS_PROGRESO = (1000, 1001)
# Estados que indican error definitivo en esta ronda
_ESTADOS_ERROR = (201, 203, 204, 211, 212)


def _nombre_estado(codigo):
    return _ESTADOS_WIFI.get(codigo, "DESCONOCIDO({})".format(codigo))


# =========================================================================
# WIFI
# =========================================================================

def _reset_wifi_completo(wlan):
    # FIX: Asegurar disconnect antes de apagar para limpiar estado del IDF
    try:
        if wlan.active():
            try:
                wlan.disconnect()
            except Exception:
                pass
            time.sleep_ms(1000)  # FIX: subido a 1s
    except Exception:
        pass

    try:
        wlan.active(False)
    except Exception:
        pass

    # CRITICO: el PHY WiFi del ESP32 necesita >=2s para liberar buffers
    time.sleep_ms(2500)

    # Verificar que se apago
    intentos = 0
    while wlan.active() and intentos < 5:
        time.sleep_ms(200)
        intentos += 1

    # Volver a encender
    try:
        wlan.active(True)
    except Exception:
        pass

    # Esperar a que el interfaz este realmente activo
    intentos = 0
    while not wlan.active() and intentos < 10:
        time.sleep_ms(200)
        try:
            wlan.active(True)
        except Exception:
            pass
        intentos += 1

    # FIX: Delay adicional aumentado para estabilizacion del PHY tras encender
    time.sleep_ms(2500)


def _intentar_conexion(wlan, ssid, password, max_intentos, etiqueta=""):
    # Intenta conectar y espera hasta max_intentos ciclos de 1s.
    # Retorna (conectado: bool, estado_final: int)

    if not wlan.active():
        log_warn("WIFI", "{}Interfaz no activo. Abortando conexion.".format(etiqueta))
        return False, 1000

    # FIX: Desactivar reconexion automatica del driver ANTES de cualquier otra cosa
    try:
        wlan.config(reconnects=0)
    except Exception:
        pass

    # FIX: Asegurar que no hay conexion/colgada pendiente del IDF
    try:
        wlan.disconnect()
        time.sleep_ms(300)
    except Exception:
        pass

    wlan.connect(ssid, password)

    # Espera inicial para que el proceso de asociacion comience
    time.sleep_ms(2000)

    intentos = 0
    ultimo_estado = -1
    errores_consecutivos = 0

    while not wlan.isconnected() and intentos < max_intentos:
        estado = wlan.status()
        if estado != ultimo_estado:
            ultimo_estado = estado
            log_debug("WIFI", "{}Intento {}/{}  Estado: {}".format(
                etiqueta, intentos + 1, max_intentos, _nombre_estado(estado)))

            if estado in _ESTADOS_ERROR:
                errores_consecutivos += 1
                # Si hay 2 errores consecutivos, abortar esta ronda
                if errores_consecutivos >= 2:
                    log_warn("WIFI", "{}Error definitivo detectado ({}). Abortando ronda.".format(
                        etiqueta, _nombre_estado(estado)))
                    break
            # FIX: Abortar tambien ante estados anomalos persistentes (ej. codigo 2)
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

    # FIX 1: Limpieza agresiva en POWERON antes de crear el objeto principal.
    # El IDF del ESP32 retiene config en NVS tras POWERON y puede dejar el PHY
    # en un estado que devuelve codigos anomalos (ej. 2). Forzamos un ciclo
    # completo de apagado antes de que red.py toque el WiFi.
    if machine.reset_cause() == machine.PWRON_RESET:
        log_debug("WIFI", "POWERON detectado. Limpiando estado WiFi persistente...")
        try:
            w_tmp = network.WLAN(network.STA_IF)
            w_tmp.active(True)
            w_tmp.disconnect()
            time.sleep_ms(1000)
            w_tmp.active(False)
            time.sleep_ms(2000)
        except Exception as e:
            log_debug("WIFI", "Error en limpieza POWERON: {}".format(e))
        log_debug("WIFI", "Esperando estabilizacion RF (POWERON)...")
        time.sleep_ms(6000)  # FIX: 6s para asegurar calibracion PHY tras encendido en frio

    # FIX 2: No reutilizamos un objeto WLAN viejo. Cada ronda crea uno nuevo
    # para evitar que herede estado corrupto del IDF.

    # ============================================================
    # RONDA 1: reset completo + conexion
    # ============================================================
    log_debug("WIFI", "Ronda 1: reset completo del interfaz STA...")
    gc.collect()
    wlan = network.WLAN(network.STA_IF)  # FIX: objeto fresco
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
    try:
        wlan.disconnect()
    except Exception:
        pass
    try:
        wlan.active(False)
    except Exception:
        pass
    time.sleep_ms(5000)

    wlan = network.WLAN(network.STA_IF)  # FIX: objeto fresco
    try:
        wlan.active(True)
    except Exception:
        pass
    time.sleep_ms(2000)

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
    try:
        wlan.disconnect()
    except Exception:
        pass
    try:
        wlan.active(False)
    except Exception:
        pass
    time.sleep_ms(4000)

    wlan = network.WLAN(network.STA_IF)  # FIX: objeto fresco
    try:
        wlan.active(True)
    except Exception:
        pass
    time.sleep_ms(2000)

    conectado, estado_final = _intentar_conexion(wlan, ssid, password, max_intentos, "R3 ")

    if conectado:
        log_info("WIFI", "Conectado (Ronda 3)! IP: {}".format(wlan.ifconfig()[0]))
        return True

    msg_r3 = "Ronda 3 fallo. Estado final: {}".format(_nombre_estado(estado_final))
    log_warn("WIFI", msg_r3)
    log_persistente("WIFI", msg_r3, "WARN")

    # --- Apagar limpiamente antes de salir ---
    try:
        wlan.active(False)
    except Exception:
        pass
    led_patron_error()
    return False


def apagar_wifi():
    try:
        wlan = network.WLAN(network.STA_IF)
        try:
            if wlan.active():
                wlan.disconnect()
                time.sleep_ms(200)
        except Exception:
            pass
        try:
            wlan.active(False)
            time.sleep_ms(500)
        except Exception:
            pass
    except Exception:
        pass
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