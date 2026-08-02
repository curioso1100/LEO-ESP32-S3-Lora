# =========================================================================
# MÓDULO: red.py  -  Funciones comunes WiFi, NTP
# =========================================================================

import network
import time
import json
import machine
import gc

from logger import log_info, log_debug, log_warn
from placa import led_patron_error

NTP_SERVERS = (
    "pool.ntp.org",
    "0.pool.ntp.org",
    "1.pool.ntp.org",
    "2.pool.ntp.org",
)

# Códigos de estado WiFi en MicroPython ESP32
_ESTADOS_WIFI = {
    0: "IDLE",
    1: "CONECTANDO",
    2: "PASS_ERR",
    3: "NO_AP",
    4: "FAIL",
    5: "CONECTADO",
}

# =========================================================================
# WIFI
# =========================================================================

def _reset_wifi_agresivo(wlan):
    """
    Reset completo del interfaz STA para limpiar estado heredado
    tras soft reboots. El ESP32 mantiene basura en el módulo WiFi
    interno tras machine.reset() si no se apaga explícitamente.
    """
    try:
        if wlan.active():
            try:
                wlan.disconnect()
            except Exception:
                pass
            time.sleep_ms(200)
    except Exception:
        pass

    try:
        wlan.active(False)
    except Exception:
        pass

    # CRÍTICO: el chip WiFi del ESP32 necesita >=1s para apagarse
    # completamente y liberar los buffers internos.
    time.sleep_ms(1500)

    try:
        wlan.active(True)
    except Exception:
        pass

    # Tiempo para que el interfaz se estabilice tras encender
    time.sleep_ms(1000)


def conectar_wifi():
    gc.collect()
    """
    Activa la interfaz STA y conecta con las credenciales configuradas.
    Versión V8.4: incluye reset agresivo del módulo WiFi antes de conectar
    para evitar estados corruptos heredados de soft reboots previos.
    """
    # Leer config localmente
    try:
        with open("config.json", "r") as cf:
            c = json.load(cf)
            ssid         = c["wifi_ssid"]
            password     = c["wifi_pass"]
            max_intentos = int(c.get("seguridad_hardware", {}).get("max_intentos_wifi", 10))
    except Exception as e_cfg:
        log_warn("WIFI", "No se pudo leer config.json: {}".format(e_cfg))
        return False

    if not ssid:
        log_warn("WIFI", "SSID vacio. Abortando.")
        return False

    # --- Calentamiento WiFi tras encendido en frío (POWERON) ---
    if machine.reset_cause() == 1:  # POWERON_RESET
        log_debug("WIFI", "POWERON detectado. Esperando calentamiento RF...")
        time.sleep_ms(1500)

    wlan = network.WLAN(network.STA_IF)

    # --- Si ya está conectado, reutilizar ---
    if wlan.isconnected():
        log_info("WIFI", "Ya habia conexion previa. IP: {}".format(wlan.ifconfig()[0]))
        return True

    # --- V8.4 FIX: reset agresivo para limpiar estado heredado ---
    # Tras soft reboots repetidos (fase3->fase4), el módulo WiFi interno
    # del ESP32 acumula basura. Sin este reset, connect() entra en bucle
    # de estados 1001/202 y nunca asocia.
    log_debug("WIFI", "Reset agresivo del interfaz STA...")
    _reset_wifi_agresivo(wlan)

    # --- Intentar conexión ---
    wlan.connect(ssid, password)
    time.sleep_ms(500)

    intentos = 0
    ultimo_estado = -1
    while not wlan.isconnected() and intentos < max_intentos:
        estado = wlan.status()
        if estado != ultimo_estado:
            ultimo_estado = estado
            log_debug("WIFI", "Intento {}/{}  Estado: {}".format(
                intentos + 1, max_intentos,
                _ESTADOS_WIFI.get(estado, "DESCONOCIDO({})".format(estado))))
        time.sleep(2)
        intentos += 1

    if wlan.isconnected():
        log_info("WIFI", "Conectado! IP: {}".format(wlan.ifconfig()[0]))
        return True
    else:
        estado_final = wlan.status()
        log_warn("WIFI", "Imposible conectar. Estado final: {}".format(estado_final))
        # Apagar limpiamente antes de salir para no dejar basura
        # al siguiente ciclo de fase4
        _reset_wifi_agresivo(wlan)
        led_patron_error()
        return False


def apagar_wifi():
    # Desconecta y desactiva la interfaz STA de forma limpia
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
    # Intenta sincronizar el RTC con los servidores NTP definidos
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
