# =========================================================================
# MÓDULO: fase2.py - REPORTE DIARIO DE PASES + ITV
# =========================================================================

import machine
import time
import json
import network
import gc
import os

from placa import led_on, led_off, led_blink, reiniciar
from red import conectar_wifi, apagar_wifi
from config_system import guardar_fase, obtener_config, version, nombre_proyecto
from logger import log_info, log_debug, log_warn, log_error, log_exception, log_persistente

# Carga de parámetros locales
CONFIG = obtener_config()

SSID = CONFIG["wifi_ssid"]
WIFI_PASS = CONFIG["wifi_pass"]
DEBUG_MODO = CONFIG.get("debug_consola", True)
MAX_INTENTOS_WIFI = int(CONFIG["seguridad_hardware"]["max_intentos_wifi"])

ARCHIVO_LOGS = "logs.txt"
DELAY_POST_CONEXION = 3  # segundos; estabiliza interfaz de red antes de envío


def _enviar_email_itv_pendiente():
    # Envía el email ITV pendiente si existe. Retorna True si se envió o no había
    try:
        from itv_manager import ITVManager
        gc.collect()
    except ImportError:
        log_warn("ITV_F2", "itv_manager.py no disponible")
        return True  # No hay nada que hacer

    itv = ITVManager(CONFIG)
    if not itv.hay_email_itv_pendiente():
        return True

    email_data = itv.obtener_email_itv_pendiente()
    if email_data is None:
        return True

    # añadir timestamp de envío para distinguir de fecha de detección
    try:
        from tiempo_satelites import obtener_unix_utc_real
        email_data["timestamp_envio"] = obtener_unix_utc_real()
    except Exception:
        pass

    gc.collect()

    import alertas
    exito = alertas.enviar_email_itv(email_data, DEBUG_MODO)

    if exito:
        itv.borrar_email_itv_pendiente()
        log_info("ITV_F2", "Email ITV enviado correctamente")
    else:
        log_warn("ITV_F2", "Fallo enviando email ITV. Se reintentara manana.")

    return exito


def ejecutar():
    # Envía reporte diario de pases, volcado asíncrono de logs y email ITV.
    # Siempre finaliza avanzando a Fase 3 y reiniciando el dispositivo.
    led_blink(2)
    led_on()
    log_info("FASE2", "Iniciando reporte diario de pases")

    wifi_ok = conectar_wifi()

    rssi_wifi = None
    if wifi_ok:
        try:
            wlan = network.WLAN(network.STA_IF)
            rssi_wifi = wlan.status('rssi')
            log_debug("FASE2", "RSSI WiFi: {} dBm".format(rssi_wifi))
        except Exception:
            rssi_wifi = None

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
                    debug_activo=DEBUG_MODO,
                    rssi_wifi=rssi_wifi
                )
            except Exception as exc:
                log_exception("FASE2", "Fallo envío reporte principal: {}".format(exc))
                log_persistente("FASE2", "Fallo envio reporte principal: {}".format(exc), "ERROR")

            if not resultado_principal:
                log_persistente("FASE2", "Envio reporte principal retorno False (sin excepcion)", "WARN")

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
                                debug_activo=DEBUG_MODO,
                                rssi_wifi=rssi_wifi
                            )
                        except Exception as exc:
                            log_exception("FASE2", "Fallo envío volcado logs: {}".format(exc))
                            log_persistente("FASE2", "Fallo envio volcado logs: {}".format(exc), "ERROR")

                        # Solo trunca si el envío tuvo éxito, para no perder datos
                        if resultado_logs:
                            open(ARCHIVO_LOGS, "w").close()
                        else:
                            log_warn("FASE2", "Logs conservados para reintento posterior")
                            log_persistente("FASE2", "Envio volcado logs retorno False", "WARN")

                except OSError as exc:
                    # captura específica de errores de archivo; no silenciar todo
                    log_error("FASE2", "Error accediendo a {}: {}".format(ARCHIVO_LOGS, exc))
                except Exception as exc:
                    log_exception("FASE2", "Error inesperado con logs: {}".format(exc))

            # --- Envío 3: Email ITV (máximo una vez al día) ---
            try:
                _enviar_email_itv_pendiente()
            except Exception as exc:
                log_exception("FASE2", "Fallo envío email ITV: {}".format(exc))
                log_persistente("FASE2", "Fallo envio email ITV: {}".format(exc), "ERROR")

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
