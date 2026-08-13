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
    # Reset completo del interfaz STA. Apaga, espera a que el PHY se libere, y vuelve a encender. El ESP32 necesita >=2s para apagarse del todo.
    try:
        if wlan.active():
            try:
                wlan.disconnect()
            except Exception:
                pass
            time.sleep_ms(500)
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

    # Delay adicional para estabilizacion del PHY tras encender
    time.sleep_ms(1500)


def _intentar_conexion(wlan, ssid, password, max_intentos, etiqueta=""):
    # Intenta conectar y espera hasta max_intentos ciclos de 1s. Retorna (conectado: bool, estado_final: int)
    # Verificar que el interfaz esta activo antes de conectar
    if not wlan.active():
        log_warn("WIFI", "{}Interfaz no activo. Abortando conexion.".format(etiqueta))
        return False, 1000

    # Desactivar reconexion automatica del driver para evitar interferencias
    try:
        wlan.config(reconnects=0)
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

            # Si es un error definitivo, contar
            if estado in _ESTADOS_ERROR:
                errores_consecutivos += 1
                # Si hay 2 errores consecutivos, abortar esta ronda
                if errores_consecutivos >= 2:
                    log_warn("WIFI", "{}Error definitivo detectado ({}). Abortando ronda.".format(
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

    wlan = network.WLAN(network.STA_IF)

    # --- Si ya esta conectado, reutilizar ---
    if wlan.isconnected():
        log_info("WIFI", "Ya habia conexion previa. IP: {}".format(wlan.ifconfig()[0]))
        return True

    # --- Calentamiento WiFi tras encendido en frio ---
    if machine.reset_cause() == machine.PWRON_RESET:
        log_debug("WIFI", "POWERON detectado. Esperando estabilizacion RF...")
        time.sleep_ms(3500)  # Aumentado de 2000 a 3500ms

    # ============================================================
    # RONDA 1: reset completo + conexion
    # ============================================================
    log_debug("WIFI", "Ronda 1: reset completo del interfaz STA...")
    gc.collect()  # Liberar memoria antes de activar WiFi
    _reset_wifi_completo(wlan)

    conectado, estado_final = _intentar_conexion(wlan, ssid, password, max_intentos, "R1 ")

    if conectado:
        log_info("WIFI", "Conectado (Ronda 1)! IP: {}".format(wlan.ifconfig()[0]))
        return True

    msg_r1 = "Ronda 1 fallo. Estado final: {}".format(_nombre_estado(estado_final))
    log_warn("WIFI", msg_r1)
    log_persistente("WIFI", msg_r1, "WARN")

    # ============================================================
    # RONDA 2: disconnect + delay largo + reconexion
    # ============================================================
    log_debug("WIFI", "Ronda 2: reconexion suave tras delay...")
    try:
        wlan.disconnect()
    except Exception:
        pass
    time.sleep_ms(5000)  # Aumentado de 3000 a 5000ms

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
    try:
        wlan.disconnect()
    except Exception:
        pass
    time.sleep_ms(500)

    try:
        wlan.active(False)
    except Exception:
        pass
    time.sleep_ms(4000)  # Delay muy largo para liberar completamente el PHY

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
    _reset_wifi_completo(wlan)
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
