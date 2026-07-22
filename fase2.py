# =========================================================================
# MÓDULO: fase2.py - REPORTE DIARIO DE PASES  (v6.21 SIMPLIFICADO)
# =========================================================================

import machine
import time
import json
import network
import gc
import os

from placa import led_on, led_off, led_blink, reiniciar
from red import conectar_wifi, apagar_wifi
from estado import guardar_fase
from logger import log_info, log_debug, log_warn, log_error, log_exception
from configuracion import obtener_config, version, nombre_proyecto

# Carga de parámetros locales
CONFIG = obtener_config()

SSID = CONFIG["wifi_ssid"]
WIFI_PASS = CONFIG["wifi_pass"]
DEBUG_MODO = CONFIG.get("debug_consola", True)
MAX_INTENTOS_WIFI = int(CONFIG["seguridad_hardware"]["max_intentos_wifi"])

ARCHIVO_LOGS = "logs.txt"
DELAY_POST_CONEXION = 3  # segundos; estabiliza interfaz de red antes de envío


def ejecutar():
    # Envía reporte diario de pases y volcado asíncrono de logs.
    # Siempre finaliza avanzando a Fase 3 y reiniciando el dispositivo.
    led_blink(2)
    led_on()
    log_info("FASE2", "Iniciando reporte diario de pases")

    wifi_ok = conectar_wifi()

    try:
        if wifi_ok:
            # Imports locales para optimizar memoria (handshake SSL)
            import alertas

            gc.collect()
            time.sleep(DELAY_POST_CONEXION)

            # Variables para controlar ambos envíos de forma independiente
            resultado_principal = False
            resultado_logs = False

            # --- Envío 1: Reporte diario de pases ---
            try:
                resultado_principal = alertas.enviar_correo_bloques(
                    "{}: Pases diarios {}".format(nombre_proyecto(), version()),
                    modo_reporte=True,
                    debug_activo=DEBUG_MODO
                )
            except Exception as exc:
                log_exception("FASE2", "Fallo envío reporte principal: {}".format(exc))

            # --- Envío 2: Volcado asíncrono de logs pendientes ---
            if ARCHIVO_LOGS in os.listdir():
                try:
                    with open(ARCHIVO_LOGS, "r") as f_log:
                        pendientes = f_log.read()

                    if pendientes.strip():  # evita enviar solo newlines/espacios
                        gc.collect()
                        try:
                            resultado_logs = alertas.enviar_correo_bloques(
                                "{}: Volcado asíncrono {}".format(nombre_proyecto(), version()),
                                modo_reporte=False,
                                texto_telemetria=pendientes,
                                debug_activo=DEBUG_MODO
                            )
                        except Exception as exc:
                            log_exception("FASE2", "Fallo envío volcado logs: {}".format(exc))

                        # Solo trunca si el envío tuvo éxito, para no perder datos
                        if resultado_logs:
                            open(ARCHIVO_LOGS, "w").close()
                        else:
                            log_warn("FASE2", "Logs conservados para reintento posterior")

                except OSError as exc:
                    # captura específica de errores de archivo; no silenciar todo
                    log_error("FASE2", "Error accediendo a {}: {}".format(ARCHIVO_LOGS, exc))
                except Exception as exc:
                    log_exception("FASE2", "Error inesperado con logs: {}".format(exc))

            # --- ELIMINADO v6.21: Envío 3 (Diagnóstico diario) ---
            # El diagnóstico diario con errores.log + heartbeat.log + satelites_cazados.txt
            # se ha movido al mecanismo unificado de fase3 -> estado_pendiente.json -> fase4.
            # Ver configuración "email_estado_horas_fijas": ["23:59"] para equivalente.

            # --- Resumen de resultados ---
            ok_principal = "OK" if resultado_principal else "FALLO"
            ok_logs = "OK" if resultado_logs else "FALLO/SIN_DATOS"
            log_info("FASE2", "Resumen: Principal={} | Logs={}".format(ok_principal, ok_logs))

            log_info("FASE2", "Avanzando a Fase 3")
            guardar_fase(3)
        else:
            log_warn("FASE2", "Sin conexión WiFi. Se omite envío y se continúa con Fase 3")
            guardar_fase(3)

    finally:
        apagar_wifi()

    reiniciar()
