# =========================================================================
# MODULO: alertas.py
# =========================================================================

import gc
import json
import time
import os

from logger import log_info, log_debug, log_warn, log_error, log_persistente
from config_system import obtener_config, version, nombre_proyecto
from tiempo_satelites import obtener_unix_utc_real, obtener_tiempo_actual, formatear_fecha_local, obtener_desfase_espana

CONFIG = obtener_config()


def _limpiar_texto_cabecera(texto):
    return str(texto).replace("\r", " ").replace("\n", " ").strip()


def _leer_respuesta_smtp(sock, codigo_esperado, debug_activo=False, multilinea=False, timeout_seg=30):
    t_inicio = time.ticks_ms()
    timeout_ms = timeout_seg * 1000
    for _ in range(2000):
        if time.ticks_diff(time.ticks_ms(), t_inicio) > timeout_ms:
            raise Exception("Timeout SMTP ({}s)".format(timeout_seg))
        try:
            linea_b = sock.readline()
        except OSError as e:
            raise Exception("Timeout leyendo respuesta SMTP: {}".format(e))
        if not linea_b:
            time.sleep_ms(50)
            continue
        linea = linea_b.decode("utf-8", "ignore").strip()
        if debug_activo:
            log_debug("SMTP", "Servidor: {}".format(linea))
        if len(linea) < 3 or not linea[:3].isdigit():
            continue
        if linea[0] == '5':
            raise Exception("Error SMTP permanente: {}".format(linea))
        if multilinea:
            if linea.startswith("{} ".format(codigo_esperado)):
                return linea
        else:
            if linea.startswith(str(codigo_esperado)):
                return linea
    raise Exception("Timeout o respuesta SMTP inesperada")


def _cargar_agenda_segura():
    try:
        with open("agenda.json", "r") as aj:
            agenda = json.load(aj)
        if not isinstance(agenda, dict):
            raise ValueError("agenda.json no contiene un objeto valido")
        fecha_agenda = agenda.get("fecha_creacion", "Desconocida")
        pases = agenda.get("pases", [])
        if not isinstance(pases, list):
            pases = []
        return fecha_agenda, pases
    except Exception as e_agenda:
        log_warn("SMTP", "No se pudo leer agenda.json: {}".format(e_agenda))
        log_persistente("SMTP", "No se pudo leer agenda.json: {}".format(e_agenda), "WARN")
        return "Desconocida", []


def _sock_write_all(sock, data, chunk_size=256, pausa_ms=100):
    total = len(data)
    enviados = 0
    while enviados < total:
        try:
            to_send = data[enviados:enviados + chunk_size]
            n = sock.write(to_send)
            if n is None or n == 0:
                time.sleep_ms(pausa_ms)
                continue
            enviados += n
            if enviados < total:
                time.sleep_ms(pausa_ms)
        except OSError as e:
            if e.args[0] in (11, 35):
                time.sleep_ms(pausa_ms)
                continue
            raise


def enviar_correo_bloques(asunto, modo_reporte=False, texto_telemetria="", debug_activo=False, rssi_wifi=None, texto_extra=""):
    import socket
    import ssl
    from tiempo_satelites import obtener_desfase_espana

    log_info("SMTP", "Gestionando el envio de email")
    if debug_activo:
        log_debug("SMTP", "Entrando en la funcion enviar_correo_bloques")
    gc.collect()

    try:
        c = CONFIG
        remitente = str(c["remitente_gmail"]).strip()
        clave = str(c["clave_aplicacion"]).strip()
        destinatario = str(c["destinatario"]).strip()
        timeout_red = int(c["seguridad_hardware"]["timeout_red_segundos"])
    except Exception as e_cfg:
        log_error("SMTP", "Fallo leyendo configuracion: {}".format(e_cfg))
        log_persistente("SMTP", "Fallo leyendo configuracion: {}".format(e_cfg), "ERROR")
        return False

    try:
        _, hora_arranque, _ = obtener_tiempo_actual()
        gc.collect()
        desfase_segundos = obtener_desfase_espana(obtener_unix_utc_real())
        gc.collect()
        if debug_activo:
            log_debug("SMTP", "Hora local calculada: {}".format(hora_arranque))
    except Exception as e_time:
        log_error("SMTP", "Fallo procesando hora local: {}".format(e_time))
        log_persistente("SMTP", "Fallo procesando hora local: {}".format(e_time), "ERROR")
        return False

    sock = None
    raw_sock = None

    try:
        gc.collect()
        if debug_activo:
            log_debug("SMTP", "Resolviendo DNS de smtp.gmail.com...")
        res_dns = socket.getaddrinfo("smtp.gmail.com", 465)
        sockaddr = res_dns[-1][-1]
        del res_dns
        gc.collect()

        if debug_activo:
            log_debug("SMTP", "Conectando a {}...".format(sockaddr))

        raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw_sock.settimeout(timeout_red)
        raw_sock.connect(sockaddr)
        gc.collect()
        time.sleep_ms(200)

        if debug_activo:
            log_debug("SMTP", "Envolviendo socket en capa SSL...")
            log_debug("SMTP", "MEMORIA LIBRE antes SSL: {}".format(gc.mem_free()))
            log_debug("SMTP", "MEMORIA ASIGNADA antes SSL: {}".format(gc.mem_alloc()))

        try:
            sock = ssl.wrap_socket(raw_sock, server_hostname="smtp.gmail.com", do_handshake_on_connect=True)
        except TypeError:
            if debug_activo:
                log_debug("SMTP", "Usando SSL alternativo sin parametros extendidos")
            gc.collect()
            time.sleep_ms(250)
            gc.collect()
            sock = ssl.wrap_socket(raw_sock)
        try:
            sock.settimeout(timeout_red)
        except Exception:
            pass

        if debug_activo:
            log_debug("SMTP", "Esperando saludo 220...")
        _leer_respuesta_smtp(sock, 220, debug_activo=debug_activo)

        if debug_activo:
            log_debug("SMTP", "Enviando EHLO...")
        sock.write(b"EHLO esp32\r\n")
        _leer_respuesta_smtp(sock, 250, debug_activo=debug_activo, multilinea=True)

        if debug_activo:
            log_debug("SMTP", "Preparando credenciales...")

        import ubinascii
        user_b64 = ubinascii.b2a_base64(remitente.encode()).decode().strip()
        pass_b64 = ubinascii.b2a_base64(clave.encode()).decode().strip()

        if debug_activo:
            log_debug("SMTP", "Autenticando...")
        sock.write(b"AUTH LOGIN\r\n")
        _leer_respuesta_smtp(sock, 334, debug_activo=debug_activo)
        sock.write((user_b64 + "\r\n").encode())
        _leer_respuesta_smtp(sock, 334, debug_activo=debug_activo)
        sock.write((pass_b64 + "\r\n").encode())
        _leer_respuesta_smtp(sock, 235, debug_activo=debug_activo)

        del user_b64, pass_b64
        gc.collect()

        if debug_activo:
            log_debug("SMTP", "Configurando MAIL FROM y RCPT TO...")
        sock.write(("MAIL FROM:<{}>\r\n".format(remitente)).encode())
        _leer_respuesta_smtp(sock, 250, debug_activo=debug_activo)
        sock.write(("RCPT TO:<{}>\r\n".format(destinatario)).encode())
        _leer_respuesta_smtp(sock, 250, debug_activo=debug_activo)

        if debug_activo:
            log_debug("SMTP", "Enviando DATA...")
        sock.write(b"DATA\r\n")
        _leer_respuesta_smtp(sock, 354, debug_activo=debug_activo)

        asunto_limpio = _limpiar_texto_cabecera(asunto)
        remitente_limpio = _limpiar_texto_cabecera(remitente)
        destinatario_limpio = _limpiar_texto_cabecera(destinatario)

        sock.write(("From: {}\r\n".format(remitente_limpio)).encode())
        sock.write(("To: {}\r\n".format(destinatario_limpio)).encode())
        sock.write(("Subject: {}\r\n".format(asunto_limpio)).encode())
        sock.write("Content-Type: text/plain; charset=UTF-8\r\n\r\n".encode())

        if not modo_reporte:
            encabezado = "Datos de captura {} {}\r\n".format(nombre_proyecto(), version())
            sock.write(encabezado.encode())
            sock.write(b"=========================\r\n")
            linea_envio = "Enviado: {} CEST\r\n".format(hora_arranque)
            sock.write(linea_envio.encode())
            sock.write(b"=========================\r\n")

            cuerpo_limpio = str(texto_telemetria).replace("->", " pasa a ").replace("|", " ")
            cuerpo_limpio = cuerpo_limpio.replace("\r", " ").replace("\n", "\r\n")

            lineas = cuerpo_limpio.split("\r\n")
            for idx in range(len(lineas)):
                if lineas[idx].startswith("."):
                    lineas[idx] = ".." + lineas[idx]
            cuerpo_limpio = "\r\n".join(lineas)

            cuerpo_bytes = (cuerpo_limpio + "\r\n").encode()
            del cuerpo_limpio
            gc.collect()
            _sock_write_all(sock, cuerpo_bytes)
            del cuerpo_bytes
            gc.collect()

            time.sleep_ms(300)

        else:
            gc.collect()
            encabezado = "Reporte diario de pases {} {}\r\n".format(nombre_proyecto(), version())
            sock.write(encabezado.encode())
            sock.write(b"===========================\r\n")
            if rssi_wifi is not None:
                linea_rssi = "RSSI WiFi: {} dBm\r\n".format(rssi_wifi)
                sock.write(linea_rssi.encode())

            fecha_agenda, pases = _cargar_agenda_segura()
            linea_fecha = "Fecha Agenda: {} (Hora Local: {})\r\n\r\n".format(fecha_agenda, hora_arranque)
            sock.write(linea_fecha.encode())

            for p in pases:
                try:
                    ini = p["tiempo"]["inicio"]
                    fin = p["tiempo"]["fin"]
                    nom = p["satelite"]["nombre"]
                    elev = p["satelite"]["max_elevacion"]
                    frec = p["lora"]["frecuencia_hz"]
                    ts_inicio_1970 = int(p["tiempo"]["utc_ini_timestamp"])
                    ts_local_2000 = ts_inicio_1970 - 946684800 + desfase_segundos
                    tupla_tiempo = time.localtime(ts_local_2000)
                    fecha_pase = "{:02d}/{:02d}".format(tupla_tiempo[2], tupla_tiempo[1])

                    linea_pas = "* [{}] Pase: {} a {} - Satélite: {} (Elev: {} grados - Frec: {} Hz)\r\n".format(
                        fecha_pase, ini, fin, nom, elev, frec
                    )
                    sock.write(linea_pas.encode())
                except Exception as e_pase:
                    log_warn("SMTP", "Pase omitido por datos invalidos: {}".format(e_pase))

            try:
                horas_estado = c.get("email_estado_horas_fijas", [])
                if horas_estado:
                    horas_str = ", ".join(horas_estado)
                else:
                    horas_str = "Ninguna (modo manual o sin huecos suficientes)"
                linea_horas = "\r\nHoras previstas de envio de email de estado: {}\r\n".format(horas_str)
                sock.write(linea_horas.encode())
            except Exception as e_horas:
                log_warn("SMTP", "No se pudo incluir horas de estado: {}".format(e_horas))

            # --- Añadir logs operativos al final del reporte ---
            if texto_extra:
                try:
                    extra_limpio = str(texto_extra).replace("\r", " ").replace("\n", "\r\n")
                    lineas_extra = extra_limpio.split("\r\n")
                    for idx in range(len(lineas_extra)):
                        if lineas_extra[idx].startswith("."):
                            lineas_extra[idx] = ".." + lineas_extra[idx]
                    extra_bytes = ("\r\n=== LOGS OPERATIVOS ===\r\n" + "\r\n".join(lineas_extra) + "\r\n").encode()
                    _sock_write_all(sock, extra_bytes)
                    del extra_bytes
                    gc.collect()
                except Exception as e_extra:
                    log_warn("SMTP", "No se pudo incluir texto extra: {}".format(e_extra))

            del fecha_agenda, pases
            gc.collect()

        if debug_activo:
            log_debug("SMTP", "Enviando fin de mensaje...")
        sock.write(b".\r\n")
        _leer_respuesta_smtp(sock, 250, debug_activo=debug_activo)

        if debug_activo:
            log_debug("SMTP", "Cerrando conexion...")
        sock.write(b"QUIT\r\n")
        _leer_respuesta_smtp(sock, 221, debug_activo=debug_activo)

        log_info("SMTP", "Correo enviado correctamente")
        return True

    except Exception as e_flujo:
        log_error("SMTP", str(e_flujo))
        log_persistente("SMTP", str(e_flujo), "ERROR")
        return False

    finally:
        try:
            if sock:
                sock.write(b"QUIT\r\n")
                time.sleep_ms(100)
        except Exception:
            pass
        for s in (sock, raw_sock):
            try:
                if s:
                    s.close()
            except Exception:
                pass
        gc.collect()


_MIN_RAM_ENVIO_ITV = 22000


def construir_email_itv(email_data):
    motivos = email_data.get("motivos", [])
    metricas = email_data.get("metricas", {})
    checklist = email_data.get("checklist", [])
    acciones = email_data.get("acciones", [])
    dias = email_data.get("dias_desde_ultima_itv", 0)
    timestamp = email_data.get("timestamp", 0)
    timestamp_envio = email_data.get("timestamp_envio", 0)

    asunto = "{}: ITV {} - {}".format(
        nombre_proyecto(), version(),
        "; ".join(motivos[:2]) if motivos else "rutinaria"
    )

    # Fechas
    fecha_detectado = formatear_fecha_local(timestamp) if timestamp else "N/A"
    fecha_envio = formatear_fecha_local(timestamp_envio) if timestamp_envio else "N/A"

    # Leer reinicios totales del sistema
    try:
        from config_system import leer_reinicios
        reinicios_totales = leer_reinicios()
    except Exception:
        reinicios_totales = 0

    partes = [
        "ITV LEO {} - Revision periodica".format(version()),
        "=" * 50,
        "Detectado: {}".format(fecha_detectado),
        "Enviado:   {}".format(fecha_envio),
        "Dias desde ultima ITV: {}".format(dias),
        "",
        "ALERTAS:",
    ]
    for m in motivos:
        partes.append("  [!] {}".format(m))
    partes.append("")

    partes.extend([
        "METRICAS:",
        "  Dias: {} | Reinicios totales: {}".format(
            metricas.get("dias_acumulados", "N/A"),
            metricas.get("reinicios_total", "N/A")),
        "  Ventilador: {} activaciones (7d)".format(
            metricas.get("ventilador_activaciones_7d", "N/A")),
        "  Temp max: {}".format(metricas.get("temp_max_7d", "N/A")),
        "  Capturas: {} total | {} (7d)".format(
            metricas.get("capturas_total_estimado", "N/A"),
            metricas.get("capturas_7d", "N/A")),
        "",
    ])

    rssi_resumen = metricas.get("rssi_por_satelite", {})
    if rssi_resumen:
        partes.append("RSSI:")
        for sat, val in rssi_resumen.items():
            partes.append("  {}: {}".format(sat, val))
        partes.append("")

    if checklist:
        partes.append("CHECKLIST (revisar al bajar):")
        for i, item in enumerate(checklist, 1):
            partes.append("  [{}] {}".format(i, item))
        partes.append("")

    if acciones:
        partes.append("ACCION:")
        for i, acc in enumerate(acciones, 1):
            partes.append("  {}. {}".format(i, acc))
        partes.append("")

    partes.extend([
        "=" * 50,
        "Para marcar ITV realizada: pulsa PRG 1 vez en fase3",
    ])

    return asunto, "\n".join(partes)


def enviar_email_itv(email_data, debug_activo=False):
    asunto, cuerpo = construir_email_itv(email_data)

    if gc.mem_free() < _MIN_RAM_ENVIO_ITV:
        msg_ram = "RAM insuficiente para email ITV ({} < {} bytes)".format(
            gc.mem_free(), _MIN_RAM_ENVIO_ITV)
        log_warn("ITV_ALERT", msg_ram)
        log_persistente("ITV_ALERT", msg_ram, "WARN")
        del asunto, cuerpo
        gc.collect()
        return False

    exito = enviar_correo_bloques(
        asunto,
        modo_reporte=False,
        texto_telemetria=cuerpo,
        debug_activo=debug_activo
    )
    del asunto, cuerpo
    gc.collect()
    return exito


def _guardar_config_con_horas_estado(horas_estado):
    from config_system import actualizar_linea_config

    if horas_estado:
        horas_fmt = ", ".join(['"{}"'.format(h) for h in horas_estado])
        nueva_linea = '  "email_estado_horas_fijas": [ {} ],\n'.format(horas_fmt)
    else:
        nueva_linea = '  "email_estado_horas_fijas": [],\n'

    exito, msg = actualizar_linea_config('"email_estado_horas_fijas"', nueva_linea)

    if exito:
        log_info("ESTADO_AUTO", "config.json actualizado. Horas: {}".format(horas_estado))
        return True
    else:
        log_error("ESTADO_AUTO", "Fallo actualizando config.json: {}".format(msg))
        log_persistente("ESTADO_AUTO", "Fallo actualizando config.json: {}".format(msg), "ERROR")
        return False


def obtener_horas_pendientes_estado():
    horas_fijas = CONFIG.get("email_estado_horas_fijas", [])
    if not horas_fijas:
        return []

    utc_unix, _, t_local = obtener_tiempo_actual()
    hora_actual_min = t_local[3] * 60 + t_local[4]

    horas_min = []
    for h_str in horas_fijas:
        try:
            partes = h_str.split(":")
            h = int(partes[0])
            m = int(partes[1])
            horas_min.append((h * 60 + m, h_str))
        except (ValueError, IndexError):
            continue

    if not horas_min:
        return []

    horas_hoy = []
    horas_manana = []
    idx_salto = None

    for i in range(1, len(horas_min)):
        if horas_min[i][0] < horas_min[i-1][0]:
            idx_salto = i
            break

    if idx_salto is not None:
        horas_hoy = horas_min[:idx_salto]
        horas_manana = horas_min[idx_salto:]
    else:
        horas_hoy = horas_min[:]
        horas_manana = []

    primera_hora_min = horas_min[0][0]
    es_hoy = (hora_actual_min >= primera_hora_min)

    pendientes = []

    if es_hoy:
        encontrada_en_hoy = False
        for minutos, h_str in horas_hoy:
            if minutos > hora_actual_min:
                pendientes.append(h_str)
                encontrada_en_hoy = True
            elif encontrada_en_hoy:
                pendientes.append(h_str)

        for minutos, h_str in horas_manana:
            pendientes.append(h_str)

    else:
        for minutos, h_str in horas_manana:
            if minutos > hora_actual_min:
                pendientes.append(h_str)

    return pendientes


if __name__ == "__main__":
    print("\n--- INICIANDO DIAGNOSTICO DE ALERTAS ---")
    import network

    try:
        with open("config.json", "r") as f:
            cfg = json.load(f)

        debug_local = cfg.get("debug_consola", True)
        ssid = cfg.get("wifi_ssid", "")
        password = cfg.get("wifi_pass", "")

        wlan = network.WLAN(network.STA_IF)
        wlan.active(True)
        wlan.connect(ssid, password)

        intentos = 0
        while not wlan.isconnected() and intentos < 15:
            print(" [WiFi] Conectando... ({}/15)".format(intentos + 1))
            time.sleep(2)
            intentos += 1

        if wlan.isconnected():
            print("[DIAGNOSTICO] WiFi conectado -> IP: {}".format(wlan.ifconfig()[0]))
            resultado = enviar_correo_bloques(
                asunto="{}: Diagnostico autonomo {}".format(nombre_proyecto(), version()),
                modo_reporte=True,
                debug_activo=debug_local
            )
            print("\n[RESULTADO] EXITO" if resultado else "\n[RESULTADO] FALLO")
        else:
            print("[ERROR] No se pudo conectar al WiFi")

    except Exception as e:
        print("[DIAGNOSTICO ERROR]", e)
