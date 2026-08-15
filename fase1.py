# ==========================================================================
# MÓDULO: fase1.py - SINCRONIZACIÓN DIARIA E INYECCIÓN HORARIA
# ==========================================================================

import machine
import time
import json
import network
import gc
import os

from placa import (
    led_on, led_off, led_blink, led_patron_error, reiniciar,
    Ventilador
)

from config_system import guardar_fase, obtener_config

from logger import log_info, log_debug, log_warn, log_error, log_exception, log_persistente
from red import conectar_wifi, apagar_wifi, sincronizar_ntp
from tiempo_satelites import obtener_unix_utc_real, obtener_desfase_espana, descargar_agenda_completa

# Carga de parámetros locales
CONFIG = obtener_config()

SSID = CONFIG["wifi_ssid"]
WIFI_PASS = CONFIG["wifi_pass"]
DEBUG_MODO = CONFIG.get("debug_consola", True)
MAX_INTENTOS_WIFI = int(CONFIG["seguridad_hardware"]["max_intentos_wifi"])

_VENTILADOR_GPIO = int(CONFIG.get("ventilador_gpio", 38))
_VENTILADOR_ACTIVO = CONFIG.get("ventilador_activo", False)

# V8.5: Archivo persistente para contar reintentos de fase1
_F1_RETRY_FILE = "f1_retry.count"
_F1_MAX_RETRIES = 10

_EPOCH_OFFSET = 946684800


def _leer_contador_retry():
    try:
        with open(_F1_RETRY_FILE, "r") as f:
            return int(f.read().strip())
    except:
        return 0


def _escribir_contador_retry(valor):
    try:
        with open(_F1_RETRY_FILE, "w") as f:
            f.write(str(valor))
    except Exception as e:
        log_warn("FASE1", "No se pudo escribir contador retry: {}".format(e))


def _borrar_contador_retry():
    try:
        if _F1_RETRY_FILE in os.listdir():
            os.remove(_F1_RETRY_FILE)
    except:
        pass


def _calcular_backoff(intentos):
    if intentos <= 1:
        return 60
    elif intentos == 2:
        return 120
    else:
        return 300


def ejecutar():
    led_blink(1)
    led_on()

    retry_count = _leer_contador_retry()
    if retry_count > 0:
        log_warn("FASE1", "Reintento #{} tras fallo previo de descarga".format(retry_count))

    ventilador = None
    if _VENTILADOR_ACTIVO:
        ventilador = Ventilador(_VENTILADOR_GPIO)
        if ventilador.inicializar():
            ventilador.encender()
            log_info("VENT", "Ventilador mantenimiento ON")
        else:
            log_warn("VENT", "No se pudo inicializar ventilador GPIO{}".format(_VENTILADOR_GPIO))
            ventilador = None

    log_info("FASE1", "Iniciando sincronización horaria y descarga de agenda")

    if conectar_wifi():
        # === FASE 0: Check actualizacion remota (V8.9) ===
        try:
            import fase0
            if fase0.ejecutar():
                log_info("FASE0", "Update remoto detectado. Reiniciando para aplicar...")
                apagar_wifi()
                time.sleep(1)
                reiniciar()
                return
        except Exception as e:
            log_persistente("FASE0", "Error en check remoto: {}".format(e), "WARN")
        # === Fin FASE 0 ===

        try:
            log_info("WIFI", "Conectado. Iniciando sincronizacion NTP")            

            ok_ntp, host_usado = sincronizar_ntp()
            if not ok_ntp:
                raise RuntimeError("No se pudo sincronizar la hora por NTP tras varios intentos")

            log_debug("NTP", "Sincronizado con {}".format(host_usado))

            # V8.5.2-fix: usar obtener_unix_utc_real() para que obtener_desfase_espana() reciba epoch 1970
            utc_ahora = obtener_unix_utc_real()
            # time.localtime() en MicroPython ESP32 usa epoch 2000, así que restamos offset
            t_utc = time.localtime(utc_ahora - _EPOCH_OFFSET)

            machine.RTC().datetime((
                int(t_utc[0]), int(t_utc[1]), int(t_utc[2]), int(t_utc[6]),
                int(t_utc[3]), int(t_utc[4]), int(t_utc[5]), 0
            ))

            desfase = obtener_desfase_espana(utc_ahora)
            local_segundos = utc_ahora + desfase
            t_loc = time.localtime(local_segundos - _EPOCH_OFFSET)

            fecha_hoy = "{}-{:02d}-{:02d}".format(t_loc[0], t_loc[1], t_loc[2])

            log_debug("RTC", "Desfase España: {} seg".format(desfase))
            log_debug("RTC", "RTC configurado en UTC")
            log_debug("RTC", "Hora local España {:02d}:{:02d}:{:02d}".format(t_loc[3], t_loc[4], t_loc[5]))

            gc.collect()
            if descargar_agenda_completa(fecha_hoy):
                _borrar_contador_retry()

                if ventilador is not None:
                    ventilador.apagar()
                    log_info("VENT", "Ventilador apagado")

                guardar_fase(2)
                led_off()
                log_info("FASE1", "Agenda e inyección listas. Avanzando a Fase 2")
                time.sleep(2)
                reiniciar()
            else:
                raise RuntimeError("Fallo en la descarga de datos de la agenda satelital (umbral N2YO no alcanzado)")

        except Exception as e:
            log_exception("FASE1", e)
        finally:
            apagar_wifi()
            led_off()
            if ventilador is not None:
                ventilador.apagar()
                log_info("VENT", "Ventilador apagado (error)")

        retry_count += 1
        if retry_count > _F1_MAX_RETRIES:
            log_error("FASE1", "Maximo de reintentos ({}) alcanzado. Reiniciando a fase1 limpia.".format(_F1_MAX_RETRIES))
            _borrar_contador_retry()
            backoff = 300
        else:
            _escribir_contador_retry(retry_count)
            backoff = _calcular_backoff(retry_count)
            log_warn("FASE1", "Reintento {}/{}. Esperando {}s antes de reiniciar...".format(
                retry_count, _F1_MAX_RETRIES, backoff))

        led_patron_error(3)
        time.sleep(backoff)
        reiniciar()

    else:
        log_warn("WIFI", "Imposible establecer conexión con wifi")
        apagar_wifi()
        led_off()
        if ventilador is not None:
            ventilador.apagar()
            log_info("VENT", "Ventilador apagado (WiFi fallido)")
        led_patron_error()
        log_warn("WIFI", "Router no disponible. Durmiendo 5 minutos")
        time.sleep(300)
        reiniciar()
