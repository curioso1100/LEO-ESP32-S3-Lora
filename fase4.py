#######################################################
# fase4.py - ENVIO DE EMAIL DE ESTADO PENDIENTE + ITV
#######################################################


import machine
import time
import json
import os
import gc

from placa import led_on, led_off, led_blink, reiniciar, prg_pulsado
from config_system import guardar_fase, obtener_config, version, nombre_proyecto
from logger import (
    log_info, log_debug, log_warn, log_error, log_exception,
    leer_estado_pendiente, borrar_estado_pendiente
)
from red import conectar_wifi, apagar_wifi, sincronizar_ntp
from tiempo import obtener_tiempo_actual, obtener_unix_utc_real, formatear_fecha_utc
from datos_satelites import obtener_horas_pendientes_estado

CONFIG = obtener_config()
DEBUG_MODO = CONFIG.get("debug_consola", True)

# RAM minima para envio seguro (SSL ~35KB + email ~15KB + margen)
_MIN_RAM_ENVIO = 22000

# Tamano maximo de payload por email de capturas (chars)
_MAX_PAYLOAD_CAPTURAS_CHARS = 8500


def _ram_libre():
    gc.collect()
    return gc.mem_free()
def _borrar_logs_originales():
    for f in ("heartbeat.log", "errores.log", "errores.log.old"):
        try:
            os.remove(f)
        except OSError:
            pass


def _fragmentar_capturas(capturas):
    """Divide la lista de capturas en trozos que quepan en ~9KB de payload."""
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
                             horas_pendientes=None):
    """Construye el cuerpo del Email 1: Estado + Heartbeats."""
    partes = []
    partes.append("ESTADO DEL SISTEMA")
    partes.append("Heartbeats acumulados: {}".format(num_hb))

    if temp_cpu is not None:
        partes.append("Temperatura CPU: {:.1f}C".format(temp_cpu))
    partes.append("Ventilador: {}".format("ENCENDIDO" if ventilador_on else "APAGADO"))
    if fs_libre_kb is not None:
        partes.append("Espacio filesystem: {:.0f}KB libres".format(fs_libre_kb))

    # Contadores de recepcion para diagnostico
    partes.append("Paquetes capturados: {} | Descartados: {}".format(
        paquetes_capturados, paquetes_descartados))

    if num_hb > 0:
        partes.append("BASE: {} | PASE: {}".format(base_count, pase_count))
        # Horas pendientes de envio de estado
        if horas_pendientes:
            partes.append("Horas pendientes de envio de email de estado: {}".format(
                ", ".join(horas_pendientes)))
        partes.append("")
        partes.append("=== TODOS LOS HEARTBEATS ({}) ===".format(num_hb))
        partes.extend(heartbeats)
    else:
        partes.append("(Sin heartbeats acumulados)")
        # Horas pendientes incluso sin heartbeats
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

    return "\n".join(partes)


def _construir_email_capturas(trozo_capturas, num_trozo, total_trozos,
                               num_cap_total, linea_inicio, linea_fin):
    """Construye el cuerpo de un email de capturas fragmentado."""
    partes = []
    partes.append("=== CAPTURAS ACUMULADAS ({}) ===".format(num_cap_total))
    partes.append("Fragmento {} de {} -- lineas {} a {}".format(
        num_trozo, total_trozos, linea_inicio, linea_fin))
    partes.append("")
    partes.extend(trozo_capturas)
    return "\n".join(partes)


def _enviar_email_smtp(asunto, cuerpo, debug_activo):
    """Envia un email via SMTP. Retorna True/False."""
    import alertas
    return alertas.enviar_correo_bloques(
        asunto,
        modo_reporte=False,
        texto_telemetria=cuerpo,
        debug_activo=debug_activo
    )


def enviar_email_estado(estado_pendiente):
    log_info("FASE4", "Enviando email de estado pendiente...")

    heartbeats = estado_pendiente.get("heartbeats", [])
    capturas = estado_pendiente.get("capturas", [])
    temp_cpu = estado_pendiente.get("temp_cpu", None)
    ventilador_on = estado_pendiente.get("ventilador_on", False)
    fs_libre_kb = estado_pendiente.get("fs_libre_kb", None)
    errores = estado_pendiente.get('errores', '')
    # Extraer contadores ANTES de liberar estado_pendiente
    paquetes_capturados = estado_pendiente.get("paquetes_capturados", 0)
    paquetes_descartados = estado_pendiente.get("paquetes_descartados", 0)

    num_hb = len(heartbeats)
    num_cap = len(capturas)
    base_count = sum(1 for hb in heartbeats if "BASE" in hb)
    pase_count = sum(1 for hb in heartbeats if "PASE" in hb)

    # Modo no-vacio — omitir envio si no hay capturas
    email_estado_vacio = CONFIG.get("email_estado_vacio", True)
    if not email_estado_vacio and num_cap == 0:
        log_info("FASE4", "Modo no-vacio activo: 0 capturas, omitiendo envio de estado")
        del estado_pendiente, heartbeats, capturas
        gc.collect()
        borrar_estado_pendiente()
        _borrar_logs_originales()
        return True

    horas_pendientes = obtener_horas_pendientes_estado()

    # Liberar estado_pendiente de memoria lo antes posible
    del estado_pendiente
    gc.collect()

    log_debug("FASE4", "RAM libre tras extraer datos: {} bytes".format(_ram_libre()))

    # ============================================================
    # EMAIL 1: ESTADO + HEARTBEATS (prioritario)
    # ============================================================
    log_info("FASE4", "Preparando Email 1: Estado + Heartbeats...")

    cuerpo_estado = _construir_email_estado(
        heartbeats, num_hb, base_count, pase_count,
        temp_cpu, ventilador_on, fs_libre_kb, errores,
        paquetes_capturados, paquetes_descartados,
        horas_pendientes)

    del heartbeats, errores
    gc.collect()

    tam_estado = len(cuerpo_estado)
    log_debug("FASE4", "Tamano Email 1 (Estado+HB): {} bytes".format(tam_estado))
    log_debug("FASE4", "RAM libre antes de enviar Email 1: {} bytes".format(_ram_libre()))

    if gc.mem_free() < _MIN_RAM_ENVIO:
        log_warn("FASE4", "RAM insuficiente para Email 1 ({} < {} bytes)".format(
            gc.mem_free(), _MIN_RAM_ENVIO))
        log_warn("FASE4", "Todo cancelado. Se reintentara en proximo ciclo.")
        del cuerpo_estado
        gc.collect()
        return False

    asunto1 = "{}: Estado {} - {} CAP {} HB".format(
        nombre_proyecto(), version(), num_cap, num_hb)

    exito1 = _enviar_email_smtp(asunto1, cuerpo_estado, DEBUG_MODO)
    del cuerpo_estado
    gc.collect()

    if not exito1:
        log_warn("FASE4", "Fallo Email 1 (Estado+HB). Se reintentara en proximo ciclo.")
        return False

    log_info("FASE4", "Email 1 (Estado+Heartbeats) enviado correctamente")

    # ============================================================
    # EMAILS N: CAPTURAS FRAGMENTADAS
    # ============================================================
    if num_cap <= 0:
        log_info("FASE4", "Sin capturas para enviar.")
        borrar_estado_pendiente()
        _borrar_logs_originales()
        return True

    log_info("FASE4", "Fragmentando {} capturas en emails de ~9KB...".format(num_cap))
    gc.collect()

    # Fragmentar capturas en trozos manejables
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
            log_warn("FASE4", "Abortando envio de capturas. {} fragmento(s) perdido(s).".format(
                total_trozos - num_trozo + 1))
            del cuerpo_frag
            gc.collect()
            todos_enviados = False
            break

        asunto_frag = "{}: Capturas {} {}/{} -- lineas {}-{} de {}".format(
            nombre_proyecto(), version(), num_trozo, total_trozos,
            linea_inicio, linea_fin, num_cap)

        exito_frag = _enviar_email_smtp(asunto_frag, cuerpo_frag, DEBUG_MODO)
        del cuerpo_frag
        gc.collect()

        if not exito_frag:
            log_warn("FASE4", "Fallo envio fragmento {}/{}. Abortando resto.".format(
                num_trozo, total_trozos))
            todos_enviados = False
            break

        log_info("FASE4", "Fragmento {}/{} enviado correctamente".format(
            num_trozo, total_trozos))

        if num_trozo < total_trozos:
            time.sleep_ms(500)
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
        return False


# =========================================================================
# ITV: Envio de email ITV pendiente
# =========================================================================

def _enviar_email_itv_pendiente():
    """Detecta y envía email ITV pendiente.
    Se llama DESPUES de conectar WiFi (antes fallaba DNS -202).
    Retorna True si se envió, False si no había pendiente o falló.
    """
    try:
        from itv_manager import ITVManager
        gc.collect()
    except ImportError:
        log_warn("ITV_F4", "itv_manager.py no disponible")
        return False

    itv = ITVManager(CONFIG)
    if not itv.hay_email_itv_pendiente():
        return False

    email_data = itv.leer_email_itv_pendiente()
    if email_data is None:
        return False

    gc.collect()

    motivos = email_data.get("motivos", [])
    metricas = email_data.get("metricas", {})
    checklist = email_data.get("checklist", [])
    acciones = email_data.get("acciones", [])
    dias = email_data.get("dias_desde_ultima_itv", 0)

    # Construir asunto
    asunto = "{}: ITV {} Revision periodica - {}".format(nombre_proyecto(), version(),"; ".join(motivos[:2]) if motivos else "rutinaria")

    # Construir cuerpo
    partes = [
        "=" * 60,
        "  I T V   -   R E V I S I O N   P E R I O D I C A   L E O {}".format(version()),
        "=" * 60,
        "",
        "Fecha: {}".format(formatear_fecha_utc(email_data.get("timestamp", 0))),
        "Dias desde ultima ITV: {}".format(dias),
        "",
        "-" * 60,
        "  MOTIVOS DE LA ALERTA",
        "-" * 60,
    ]
    for m in motivos:
        partes.append("  [!] {}".format(m))
    partes.append("")

    partes.extend([
        "-" * 60,
        "  METRICAS DEL SISTEMA",
        "-" * 60,
        "  Dias acumulados:        {}".format(metricas.get("dias_acumulados", "N/A")),
        "  Heartbeats acumulados:  {}".format(metricas.get("heartbeats_acumulados", "N/A")),
        "  Reinicios (7d):         {}".format(metricas.get("reinicios_7d", "N/A")),
        "  Reinicios (total):      {}".format(metricas.get("reinicios_total", "N/A")),
        "  Ventilador activ. (7d): {}".format(metricas.get("ventilador_activaciones_7d", "N/A")),
        "  Temp max (7d):          {} C".format(metricas.get("temp_max_7d", "N/A")),
        "  Temp max (30d):         {} C".format(metricas.get("temp_max_30d", "N/A")),
        "  Capturas total (est):   {}".format(metricas.get("capturas_total_estimado", "N/A")),
        "  Capturas (7d):          {}".format(metricas.get("capturas_7d", "N/A")),
        "  Emails enviados (7d):   {}".format(metricas.get("emails_7d", "N/A")),
        "",
    ])

    rssi_resumen = metricas.get("rssi_por_satelite", {})
    if rssi_resumen:
        partes.extend([
            "-" * 60,
            "  RSSI POR SATELITE",
            "-" * 60,
        ])
        for sat, val in rssi_resumen.items():
            partes.append("  {}: {}".format(sat, val))
        partes.append("")

    partes.extend([
        "-" * 60,
        "  CHECKLIST FISICO (marcar al bajar la placa)",
        "-" * 60,
    ])
    for i, item in enumerate(checklist, 1):
        partes.append("  [{}] {}".format(i, item))
    partes.append("")

    partes.extend([
        "-" * 60,
        "  ACCIONES POSIBLES",
        "-" * 60,
    ])
    for i, acc in enumerate(acciones, 1):
        partes.append("  {}. {}".format(i, acc))
    partes.append("")

    partes.extend([
        "=" * 60,
        "Para marcar ITV como REALIZADA y resetear contadores:",
        "  1. Baja la placa del techo",
        "  2. Revisa el checklist fisico",
        "  3. Pulsa PRG 3 veces seguidas (cada <1s) en modo fase3",
        "  4. Sube la placa de nuevo",
        "=" * 60,
    ])

    cuerpo = "\n".join(partes)

    if gc.mem_free() < _MIN_RAM_ENVIO:
        log_warn("ITV_F4", "RAM insuficiente para email ITV ({} < {} bytes)".format(
            gc.mem_free(), _MIN_RAM_ENVIO))
        del cuerpo, partes, email_data
        gc.collect()
        return False

    exito = _enviar_email_smtp(asunto, cuerpo, DEBUG_MODO)
    del cuerpo, partes, email_data
    gc.collect()

    if exito:
        log_info("ITV_F4", "Email ITV enviado correctamente")
        return True
    else:
        log_warn("ITV_F4", "Fallo enviando email ITV. Se reintentara en proximo ciclo.")
        return False


# =========================================================================
# ITV: Detectar confirmación ITV desde botón PRG en fase4
# =========================================================================

def _detectar_confirmacion_itv_prg():
    """Detecta 3 pulsaciones cortas de PRG en fase4 para marcar ITV realizada."""
    try:
        from itv_manager import ITVManager
        gc.collect()
    except ImportError:
        return False

    itv = ITVManager(CONFIG)
    if not itv.hay_email_itv_pendiente():
        return False

    pulsaciones = 0
    t_inicio = time.ticks_ms()

    while time.ticks_diff(time.ticks_ms(), t_inicio) < 5000:
        if prg_pulsado():
            t_pulso = time.ticks_ms()
            while prg_pulsado():
                time.sleep_ms(50)
            dur = time.ticks_diff(time.ticks_ms(), t_pulso)
            if dur > 1000:
                pulsaciones = 0
                continue
            pulsaciones += 1
            if pulsaciones >= 3:
                log_info("ITV_F4", "Confirmacion ITV detectada (3 pulsaciones PRG en fase4)")
                itv.marcar_itv_realizada(obtener_unix_utc_real(), "boton_prg_fase4")
                led_blink(5, pausa_ms=100)
                return True
        time.sleep_ms(100)

    return False


def ejecutar():
    led_blink(4)
    led_on()
    log_info("FASE4", "Iniciando despacho de estado")

    gc.collect()
    log_debug("FASE4", "RAM libre al inicio de fase4: {} bytes".format(gc.mem_free()))

    # --- ITV: detectar confirmación PRG antes de enviar nada ---
    if _detectar_confirmacion_itv_prg():
        log_info("FASE4", "ITV confirmada via PRG. Volviendo a fase3.")
        guardar_fase(3)
        apagar_wifi()
        reiniciar()
        return

    # --- Leer estado pendiente ANTES de conectar WiFi ---
    estado_pendiente = leer_estado_pendiente()

    if estado_pendiente is None:
        log_warn("FASE4", "No hay estado pendiente. Volviendo a fase3.")
        guardar_fase(3)
        apagar_wifi()
        reiniciar()
        return

    log_info("FASE4", "Detectado estado pendiente de fase3")
    led_blink(4)
    led_on()

    # --- Conectar WiFi PRIMERO ---
    wifi_conectado = conectar_wifi()
    if not wifi_conectado:
        log_warn("FASE4", "Sin WiFi para enviar estado pendiente")
        apagar_wifi()
        time.sleep(30)
        reiniciar()
        return

    # --- NTP ---
    ok_ntp, servidor = sincronizar_ntp()
    if ok_ntp:
        log_debug("NTP", "Sincronizado con {}".format(servidor))

    # Enviar email ITV DESPUES de tener WiFi ---
    itv_enviado = _enviar_email_itv_pendiente()
    if itv_enviado:
        log_info("FASE4", "Email ITV enviado. Procediendo con email de estado normal...")

    # --- Email de estado normal ---
    exito = enviar_email_estado(estado_pendiente)

    if exito:
        guardar_fase(3)
    else:
        log_warn("FASE4", "Email fallo, manteniendo fase4 para reintento")

    apagar_wifi()
    reiniciar()
    return