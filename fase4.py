# ====================================================================
# fase4.py - Envio de email pendiente
# ====================================================================

import machine
import time
import os
import gc
import json
import network

import placa
from placa import led_on, led_off, led_blink, reiniciar
from config_system import guardar_fase, obtener_config, version, nombre_proyecto, incrementar_reinicios, leer_f4_fallos, guardar_f4_fallos
from logger import (
    log_info, log_debug, log_warn, log_error, log_exception, log_persistente,
    leer_estado_pendiente, borrar_estado_pendiente
)
from red import conectar_wifi, apagar_wifi, sincronizar_ntp
from tiempo_satelites import obtener_tiempo_actual, obtener_unix_utc_real, obtener_desfase_espana
from alertas import obtener_horas_pendientes_estado

CONFIG = obtener_config()
DEBUG_MODO = CONFIG.get("debug_consola", True)

# RAM minima para envio seguro
_MIN_RAM_ENVIO = 22000

# --- MOCK PARA PRUEBAS SIN WIFI (solo si existe mock_email.txt) ---
_MOCK_EMAIL = False
try:
    with open("mock_email.txt", "r") as f:
        _MOCK_EMAIL = True
except OSError:
    pass

def _mock_enviar_email_smtp(asunto, cuerpo, debug_activo, rssi_wifi=None):
    try:
        with open("email_simulado.txt", "a") as f:
            f.write("="*60 + "\n")
            f.write("ASUNTO: " + str(asunto) + "\n")
            f.write("CUERPO:\n" + str(cuerpo) + "\n")
            f.write("="*60 + "\n\n")
            f.flush()
    except Exception as e:
        log_warn("MOCK", "Error escribiendo mock: {}".format(e))
    return True

# Reemplazar _enviar_email_smtp si estamos en modo mock
if _MOCK_EMAIL:
    _enviar_email_smtp = _mock_enviar_email_smtp
    log_info("MOCK", "Modo MOCK activo: emails se escriben en email_simulado.txt")
# --- FIN MOCK ---

# Tamano maximo de payload por email de capturas (chars)
_MAX_PAYLOAD_CAPTURAS_CHARS = 5000
# Heartbeats maximo por email. 
# Valor configurable via config.json (clave max_hb_email). Fallback 40.
_MAX_HB_EMAIL = int(CONFIG.get("max_hb_email", 40))


def _ram_libre():
    gc.collect()
    return gc.mem_free()

def _borrar_logs_originales():
    for f in ("heartbeat.log", "errores.log", "errores.log.old"):
        try:
            os.remove(f)
        except OSError:
            pass


def _marcar_estado_enviado():
    # Persiste el flag estado_enviado=True para evitar reenvio spam del estado
    try:
        with open("estado_pendiente.json", "r") as f:
            ep = json.load(f)
        ep["estado_enviado"] = True
        with open("estado_pendiente.json", "w") as f:
            json.dump(ep, f)
            f.flush()
            os.sync()
        log_debug("FASE4", "Flag estado_enviado=True guardado")
    except Exception as e:
        log_warn("FASE4", "No se pudo marcar estado como enviado: {}".format(e))

def _fragmentar_capturas(capturas):
    if not capturas:
        return []
    trozos = []
    trozo_actual = []
    tam_actual = 0
    for linea in capturas:
        tam_linea = len(linea) + 1
        if tam_actual + tam_linea > _MAX_PAYLOAD_CAPTURAS_CHARS and trozo_actual:
            trozos.append(trozo_actual)
            trozo_actual = [linea]
            tam_actual = tam_linea
        else:
            trozo_actual.append(linea)
            tam_actual += tam_linea
    if trozo_actual:
        trozos.append(trozo_actual)
    return trozos


def _construir_email_estado(heartbeats, num_hb, base_count, pase_count,
                             temp_cpu, ventilador_on, fs_libre_kb, errores,
                             paquetes_capturados=0, paquetes_descartados=0,
                             horas_pendientes=None, rssi_wifi=None):
    partes = []
    partes.append("ESTADO DEL SISTEMA")
    if rssi_wifi is not None:
        partes.append("RSSI WiFi: {} dBm".format(rssi_wifi))
    partes.append("Heartbeats acumulados: {}".format(num_hb))
    if temp_cpu is not None:
        partes.append("Temperatura CPU: {:.1f}C".format(temp_cpu))
    partes.append("Ventilador: {}".format("ENCENDIDO" if ventilador_on else "APAGADO"))
    if fs_libre_kb is not None:
        partes.append("Espacio filesystem: {:.0f}KB libres".format(fs_libre_kb))
    partes.append("Paquetes capturados: {} | Descartados: {}".format(
        paquetes_capturados, paquetes_descartados))
    if num_hb > 0:
        partes.append("BASE: {} | PASE: {}".format(base_count, pase_count))
        if horas_pendientes:
            partes.append("Horas pendientes de envio de email de estado: {}".format(
                ", ".join(horas_pendientes)))
        partes.append("")
        partes.append("=== TODOS LOS HEARTBEATS ({}) ===".format(num_hb))
        partes.extend(heartbeats)
    else:
        partes.append("(Sin heartbeats acumulados)")
        if horas_pendientes:
            partes.append("Horas pendientes de envio de email de estado: {}".format(
                ", ".join(horas_pendientes)))
    if errores:
        partes.append("")
        partes.append("=== ERRORES.LOG ===")
        partes.append(errores)
    else:
        partes.append("")
        partes.append("(Sin errores/alertas relevantes en el periodo)")
    # V9.1: bloque de metadatos estructurados para parsing externo
    try:
        from tiempo_satelites import obtener_tiempo_actual
        _, ts_str, _ = obtener_tiempo_actual()
        fecha_meta = ts_str[:10]
        hora_meta = ts_str[11:]
        tz_meta = "CEST" if obtener_desfase_espana(obtener_unix_utc_real()) == 7200 else "CET"
        partes.append("")
        partes.append("---BEGIN_META---")
        partes.append("v={}|d={}|t={}|tz={}|wifi={}|cpu={}|fan={}|fs={}|hb={}|cap={}|drop={}|base={}|pase={}".format(
            version(), fecha_meta, hora_meta, tz_meta,
            rssi_wifi if rssi_wifi is not None else "N/A",
            "{:.1f}".format(temp_cpu) if temp_cpu is not None else "N/A",
            "1" if ventilador_on else "0",
            "{:.0f}".format(fs_libre_kb) if fs_libre_kb is not None else "N/A",
            num_hb, paquetes_capturados, paquetes_descartados,
            base_count, pase_count))
        partes.append("---END_META---")
    except Exception:
        pass
    return "\n".join(partes)


def _construir_email_capturas(trozo_capturas, num_trozo, total_trozos,
                               num_cap_total, linea_inicio, linea_fin):
    partes = []
    partes.append("=== CAPTURAS ACUMULADAS ({}) ===".format(num_cap_total))
    partes.append("Fragmento {} de {} -- lineas {} a {}".format(
        num_trozo, total_trozos, linea_inicio, linea_fin))
    partes.append("")
    partes.extend(trozo_capturas)
    return "\n".join(partes)


def _enviar_email_smtp(asunto, cuerpo, debug_activo, rssi_wifi=None):
    import alertas
    return alertas.enviar_correo_bloques(
        asunto,
        modo_reporte=False,
        texto_telemetria=cuerpo,
        debug_activo=debug_activo,
        rssi_wifi=rssi_wifi
    )


def enviar_email_estado(estado_pendiente, rssi_wifi=None):
    log_info("FASE4", "Enviando email de estado pendiente...")

    heartbeats = estado_pendiente.get("heartbeats", [])
    # Limitar heartbeats para no saturar el buffer SSL del ESP32 (~2KB)
    if len(heartbeats) > _MAX_HB_EMAIL:
        heartbeats = heartbeats[-_MAX_HB_EMAIL:]

    capturas = estado_pendiente.get("capturas", [])
    temp_cpu = estado_pendiente.get("temp_cpu", None)
    ventilador_on = estado_pendiente.get("ventilador_on", False)
    fs_libre_kb = estado_pendiente.get("fs_libre_kb", None)
    errores = estado_pendiente.get('errores', '')
    paquetes_capturados = estado_pendiente.get("paquetes_capturados", 0)
    paquetes_descartados = estado_pendiente.get("paquetes_descartados", 0)

    num_hb = len(heartbeats)
    num_cap = len(capturas)
    base_count = sum(1 for hb in heartbeats if "BASE" in hb)
    pase_count = sum(1 for hb in heartbeats if "PASE" in hb)

    email_estado_vacio = CONFIG.get("email_estado_vacio", True)
    if not email_estado_vacio and num_cap == 0:
        log_info("FASE4", "Modo no-vacio activo: 0 capturas, omitiendo envio de estado")
        del estado_pendiente, heartbeats, capturas
        gc.collect()
        borrar_estado_pendiente()
        _borrar_logs_originales()
        return True

    horas_pendientes = obtener_horas_pendientes_estado()

    # --- Flag anti-spam: evitar reenviar estado si ya se envio en ciclo anterior ---
    estado_ya_enviado = estado_pendiente.get("estado_enviado", False)

    del estado_pendiente
    gc.collect()

    log_debug("FASE4", "RAM libre tras extraer datos: {} bytes".format(_ram_libre()))

    if estado_ya_enviado:
        log_info("FASE4", "Email de estado ya enviado previamente. Saltando a capturas.")
        exito1 = True
    else:
        log_info("FASE4", "Preparando Email 1: Estado + Heartbeats...")

        cuerpo_estado = _construir_email_estado(
            heartbeats, num_hb, base_count, pase_count,
            temp_cpu, ventilador_on, fs_libre_kb, errores,
            paquetes_capturados, paquetes_descartados,
            horas_pendientes, rssi_wifi)

        del heartbeats, errores
        gc.collect()

        tam_estado = len(cuerpo_estado)
        log_debug("FASE4", "Tamano Email 1 (Estado+HB): {} bytes".format(tam_estado))
        log_debug("FASE4", "RAM libre antes de enviar Email 1: {} bytes".format(_ram_libre()))

        if gc.mem_free() < _MIN_RAM_ENVIO:
            log_warn("FASE4", "RAM insuficiente para Email 1 ({} < {} bytes)".format(
                gc.mem_free(), _MIN_RAM_ENVIO))
            log_persistente("FASE4", "RAM insuficiente para Email 1 ({} < {} bytes)".format(
                gc.mem_free(), _MIN_RAM_ENVIO), "ERROR")
            del cuerpo_estado
            gc.collect()
            return False

        asunto1 = "{}: Estado {} - {} CAP {} HB".format(
            nombre_proyecto(), version(), num_cap, num_hb)

        exito1 = _enviar_email_smtp(asunto1, cuerpo_estado, DEBUG_MODO, rssi_wifi)
        del cuerpo_estado
        gc.collect()

        if not exito1:
            log_warn("FASE4", "Fallo Email 1 (Estado+HB). Se reintentara en proximo ciclo.")
            log_persistente("FASE4", "Fallo Email 1 (Estado+HB). Se reintentara en proximo ciclo.", "ERROR")
            return False

        log_info("FASE4", "Email 1 (Estado+Heartbeats) enviado correctamente")
        _marcar_estado_enviado()

    delay_seg = CONFIG.get("delay_entre_emails_seg", 60)
    if delay_seg > 0 and num_cap > 0:
        log_info("FASE4", "Esperando {}s antes de enviar capturas (anti-rate-limit)...".format(delay_seg))
        time.sleep(delay_seg)
        gc.collect()

    if num_cap <= 0:
        log_info("FASE4", "Sin capturas para enviar.")
        borrar_estado_pendiente()
        _borrar_logs_originales()
        return True

    log_info("FASE4", "Fragmentando {} capturas en emails de ~9KB...".format(num_cap))
    gc.collect()

    trozos = _fragmentar_capturas(capturas)
    total_trozos = len(trozos)
    del capturas
    gc.collect()

    log_info("FASE4", "Capturas divididas en {} fragmento(s)".format(total_trozos))

    linea_actual = 1
    todos_enviados = True

    for i, trozo in enumerate(trozos):
        num_trozo = i + 1
        lineas_en_trozo = len(trozo)
        linea_inicio = linea_actual
        linea_fin = linea_actual + lineas_en_trozo - 1

        log_info("FASE4", "Enviando fragmento {}/{} (lineas {}-{})...".format(
            num_trozo, total_trozos, linea_inicio, linea_fin))

        cuerpo_frag = _construir_email_capturas(
            trozo, num_trozo, total_trozos, num_cap, linea_inicio, linea_fin)
        del trozo
        gc.collect()

        tam_frag = len(cuerpo_frag)
        log_debug("FASE4", "Tamano fragmento {}/{}: {} bytes".format(
            num_trozo, total_trozos, tam_frag))
        log_debug("FASE4", "RAM libre antes de enviar fragmento: {} bytes".format(_ram_libre()))

        if gc.mem_free() < _MIN_RAM_ENVIO:
            log_warn("FASE4", "RAM insuficiente para fragmento {} ({} < {} bytes)".format(
                num_trozo, gc.mem_free(), _MIN_RAM_ENVIO))
            log_persistente("FASE4", "RAM insuficiente para fragmento {} ({} < {} bytes)".format(
                num_trozo, gc.mem_free(), _MIN_RAM_ENVIO), "ERROR")
            del cuerpo_frag
            gc.collect()
            todos_enviados = False
            break

        asunto_frag = "{}: Capturas {} {}/{} -- lineas {}-{} de {}".format(
            nombre_proyecto(), version(), num_trozo, total_trozos,
            linea_inicio, linea_fin, num_cap)

        exito_frag = _enviar_email_smtp(asunto_frag, cuerpo_frag, DEBUG_MODO, rssi_wifi)
        del cuerpo_frag
        gc.collect()

        if not exito_frag:
            log_warn("FASE4", "Fallo envio fragmento {}/{}. Abortando resto.".format(
                num_trozo, total_trozos))
            log_persistente("FASE4", "Fallo envio fragmento {}/{}. Abortando resto.".format(
                num_trozo, total_trozos), "ERROR")
            todos_enviados = False
            break

        log_info("FASE4", "Fragmento {}/{} enviado correctamente".format(
            num_trozo, total_trozos))

        # V9.1 FIX: rate-limit entre fragmentos, no solo 3s
        if num_trozo < total_trozos:
            frag_delay = CONFIG.get("delay_entre_emails_seg", 60)
            if frag_delay > 0:
                log_info("FASE4", "Esperando {}s antes del siguiente fragmento (anti-rate-limit)...".format(frag_delay))
                time.sleep(frag_delay)
            gc.collect()

        linea_actual = linea_fin + 1

    del trozos
    gc.collect()

    if todos_enviados:
        log_info("FASE4", "Todos los fragmentos de capturas enviados correctamente")
        borrar_estado_pendiente()
        _borrar_logs_originales()
        return True
    else:
        log_warn("FASE4", "No todos los fragmentos se enviaron. Se reintentaran en proximo ciclo.")
        log_persistente("FASE4", "No todos los fragmentos se enviaron. Se reintentaran en proximo ciclo.", "ERROR")
        return False


def ejecutar():
    led_blink(4)
    led_on()
    log_info("FASE4", "Iniciando despacho de estado")
    t_inicio_fase4 = time.ticks_ms()
    MAX_FASE4_MS = 5 * 60 * 1000

    fallos = leer_f4_fallos()

    try:
        gc.collect()
        log_debug("FASE4", "RAM libre al inicio de fase4: {} bytes".format(gc.mem_free()))

        if fallos >= 5:
            log_warn("FASE4", "Demasiados fallos consecutivos ({}), abandonando email y volviendo a fase3".format(fallos))
            log_persistente("FASE4", "Demasiados fallos consecutivos ({}), abandonando email y volviendo a fase3".format(fallos), "ERROR")
            guardar_f4_fallos(0)
            guardar_fase(3)
            apagar_wifi()
            reiniciar()
            return
        elif fallos >= 2:
            log_warn("FASE4", "Backoff: esperando 3 min antes de reintentar (fallo {}/5)".format(fallos))
            time.sleep(180)

        estado_pendiente = leer_estado_pendiente()

        if estado_pendiente is None:
            log_warn("FASE4", "No hay estado pendiente. Volviendo a fase3.")
            log_persistente("FASE4", "No hay estado pendiente. Volviendo a fase3.", "WARN")
            guardar_fase(3)
            apagar_wifi()
            reiniciar()
            return

        log_info("FASE4", "Detectado estado pendiente de fase3")
        led_blink(4)
        led_on()

        if time.ticks_diff(time.ticks_ms(), t_inicio_fase4) > MAX_FASE4_MS:
            msg_wd = "WATCHDOG: excedido tiempo maximo en fase4, abortando"
            log_warn("FASE4", msg_wd)
            log_persistente("FASE4", msg_wd, "ERROR")
            apagar_wifi()
            reiniciar()
            return

        rssi_wifi = None
        if _MOCK_EMAIL:
            log_info("MOCK", "Saltando WiFi/NTP en modo mock")
        else:
            wifi_conectado = conectar_wifi()
            if wifi_conectado:
                try:
                    wlan = network.WLAN(network.STA_IF)
                    rssi_wifi = wlan.status('rssi')
                    log_debug("FASE4", "RSSI WiFi: {} dBm".format(rssi_wifi))
                except Exception:
                    rssi_wifi = None
            if not wifi_conectado:
                log_warn("FASE4", "Sin WiFi para enviar estado pendiente")
                log_persistente("FASE4", "Sin WiFi para enviar estado pendiente", "ERROR")
                apagar_wifi()
                time.sleep(60)
                incrementar_reinicios()
                reiniciar()
                return

            ok_ntp, servidor = sincronizar_ntp()
            if ok_ntp:
                log_debug("NTP", "Sincronizado con {}".format(servidor))

        if time.ticks_diff(time.ticks_ms(), t_inicio_fase4) > MAX_FASE4_MS:
            msg_wd2 = "WATCHDOG: excedido tiempo maximo antes de email en fase4, abortando"
            log_warn("FASE4", msg_wd2)
            log_persistente("FASE4", msg_wd2, "ERROR")
            apagar_wifi()
            reiniciar()
            return

        exito = enviar_email_estado(estado_pendiente, rssi_wifi)

        if exito:
            guardar_f4_fallos(0)
            # === FASE 0 post-email (preparado, desactivado inicialmente) ===
            # Descomentar las 9 lineas siguientes para activar check post-email:
            # try:
            #     import fase0
            #     if fase0.ejecutar():
            #         log_info("FASE0", "Update remoto post-email detectado. Reiniciando...")
            #         apagar_wifi()
            #         time.sleep(1)
            #         reiniciar()
            #         return
            # except Exception as e:
            #     log_warn("FASE0", "Error post-email: {}".format(e))
            # === Fin FASE 0 ===            
            guardar_fase(3)
        else:
            log_warn("FASE4", "Email fallo, reintentando mas tarde")
            log_persistente("FASE4", "Email fallo, reintentando mas tarde", "ERROR")
            incrementar_reinicios()
            guardar_f4_fallos(fallos + 1)

        apagar_wifi()
        reiniciar()
        return

    except Exception as e:
        log_error("FASE4", "Excepcion no controlada en fase4: {}".format(e))
        log_persistente("FASE4", "Excepcion no controlada en fase4: {}".format(e), "ERROR")
        try:
            guardar_f4_fallos(fallos + 1)
        except Exception:
            pass

    finally:
        log_warn("FASE4", "Saliendo de fase4 -> reiniciando")
        apagar_wifi()
        incrementar_reinicios()
        reiniciar()
