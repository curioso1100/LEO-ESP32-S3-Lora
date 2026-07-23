# =========================================================================
# MÓDULO: red.py  -  Funciones comunes WiFi, NTP
# =========================================================================
import network
import time
import gc

from config_system import obtener_config
from logger import log_debug, log_warn
from placa import led_patron_error

CONFIG = obtener_config()

SSID     = CONFIG["wifi_ssid"]
WIFI_PASS = CONFIG["wifi_pass"]

MAX_INTENTOS_WIFI = int(
    CONFIG["seguridad_hardware"]["max_intentos_wifi"]
)

NTP_SERVERS = (
    "pool.ntp.org",
    "0.pool.ntp.org",
    "1.pool.ntp.org",
    "2.pool.ntp.org",
)

# =========================================================================
# WIFI
# =========================================================================

def conectar_wifi():
    # Activa la interfaz STA y conecta con las credenciales configuradas
    wlan = None

    try:
        gc.collect()
        wlan = network.WLAN(network.STA_IF)

        try:
            wlan.disconnect()
        except Exception:
            pass

        try:
            wlan.active(False)
        except Exception:
            pass

        time.sleep(2)

        wlan.active(True)
        time.sleep_ms(500)

        wlan.connect(SSID, WIFI_PASS)

        intentos = 0
        while not wlan.isconnected() and intentos < MAX_INTENTOS_WIFI:
            time.sleep(2)
            intentos += 1

        if not wlan.isconnected():
            log_warn("WIFI", "Tiempo de espera agotado tras {} intentos".format(intentos))
            led_patron_error() # indica el error con el led de la placa

        return wlan.isconnected()

    except Exception as e:
        log_debug("WIFI", "Error interno WiFi: {}".format(e))
        log_debug("WIFI", repr(e))
        log_warn("WIFI",  "Error interno del driver WiFi")
        led_patron_error() # indica el error con el led de la placa

        try:
            if wlan:
                wlan.active(False)
        except Exception:
            pass

        gc.collect()
        return False


def apagar_wifi():
    # Desconecta y desactiva la interfaz STA
    try:
        wlan = network.WLAN(network.STA_IF)

        try:
            wlan.disconnect()
        except Exception:
            pass

        try:
            wlan.active(False)
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
    import ntptime  # no está arriba por tema de uso mínimo de memoria
    if hasattr(ntptime, "timeout"):
        ntptime.timeout = 3

    for host in NTP_SERVERS:
        try:
            ntptime.host = host
            log_debug("NTP", "Intentando con {}".format(host))
            ntptime.settime()
            return True, host

        except Exception as e:
            log_debug("NTP", "Fallo con {}: {}".format(host, e))

    return False, None