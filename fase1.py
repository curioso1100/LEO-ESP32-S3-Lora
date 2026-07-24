# =========================================================================
# MÓDULO: fase1.py - SINCRONIZACIÓN DIARIA E INYECCIÓN HORARIA
# =========================================================================


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

from logger import (log_info, log_debug, log_warn, log_error, log_exception)
from red import conectar_wifi, apagar_wifi, sincronizar_ntp

# Carga de parámetros locales
CONFIG = obtener_config()

SSID = CONFIG["wifi_ssid"]
WIFI_PASS = CONFIG["wifi_pass"]
DEBUG_MODO = CONFIG.get("debug_consola", True)
MAX_INTENTOS_WIFI = int(CONFIG["seguridad_hardware"]["max_intentos_wifi"])

_VENTILADOR_GPIO = int(CONFIG.get("ventilador_gpio", 38))
_VENTILADOR_ACTIVO = CONFIG.get("ventilador_activo", False)


def ejecutar():
    led_blink(1)
    led_on()

    # Ventilador de mantenimiento
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
        try:
            from tiempo_satelites import obtener_desfase_espana, descargar_agenda_completa

            log_info("WIFI", "Conectado. Iniciando sincronización NTP")

            ok_ntp, host_usado = sincronizar_ntp()
            if not ok_ntp:
                raise RuntimeError("No se pudo sincronizar la hora por NTP tras varios intentos")

            log_debug("NTP", "Sincronizado con {}".format(host_usado))

            utc_ahora = int(time.time())
            t_utc = time.localtime(utc_ahora)

            machine.RTC().datetime((
                int(t_utc[0]), int(t_utc[1]), int(t_utc[2]), int(t_utc[6]),
                int(t_utc[3]), int(t_utc[4]), int(t_utc[5]), 0
            ))

            desfase = obtener_desfase_espana(utc_ahora)
            local_segundos = utc_ahora + desfase
            t_loc = time.localtime(local_segundos)

            fecha_hoy = f"{t_loc[0]}-{t_loc[1]:02d}-{t_loc[2]:02d}"

            log_debug("RTC", "Desfase España: {} seg".format(desfase))
            log_debug("RTC", "RTC configurado en UTC")
            log_debug("RTC", "Hora local España {:02d}:{:02d}:{:02d}".format(t_loc[3], t_loc[4], t_loc[5]))

            gc.collect()
            if descargar_agenda_completa(fecha_hoy):
                # Apagar ventilador ANTES de reiniciar
                if ventilador is not None:
                    ventilador.apagar()
                    log_info("VENT", "Ventilador apagado")

                guardar_fase(2)
                led_off()
                log_info("FASE1", "Agenda e inyección listas. Avanzando a Fase 2")
                time.sleep(2)
                reiniciar()
            else:
                raise RuntimeError("Fallo en la descarga de datos de la agenda satelital")

        except Exception as e:
            log_exception("FASE1", e)
        finally:
            apagar_wifi()
            led_off()
            # Apagar ventilador en caso de error
            if ventilador is not None:
                ventilador.apagar()
                log_info("VENT", "Ventilador apagado (error)")

        led_patron_error(3)
        log_warn("FASE1", "Modo resiliencia activado. Reintento en 5 minutos")
        time.sleep(300)
        reiniciar()

    else:
        log_warn("WIFI", "Imposible establecer conexión con wifi")
        apagar_wifi()
        led_off()
        # Apagar ventilador si WiFi falla
        if ventilador is not None:
            ventilador.apagar()
            log_info("VENT", "Ventilador apagado (WiFi fallido)")
        led_patron_error()
        log_warn("WIFI", "Router no disponible. Durmiendo 5 minutos")
        time.sleep(300)
        reiniciar()