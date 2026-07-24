# =========================================================================
# MODULO: alertas.py
# =========================================================================
import gc
import json
import time

from logger import log_info, log_debug, log_warn, log_error
from config_system import obtener_config, version, nombre_proyecto
from tiempo_satelites import obtener_unix_utc_real, obtener_tiempo_actual

CONFIG = obtener_config()


def _limpiar_texto_cabecera(texto):
    return str(texto).replace("\r", " ").replace("\n", " ").strip()


def _leer_respuesta_smtp(sock, codigo_esperado, debug_activo=False, multilinea=False, max_lecturas=60):
    for _ in range(max_lecturas):
        linea_b = sock.readline()
        if not linea_b:
            time.sleep_ms(50)
            continue

        linea = linea_b.decode("utf-8", "ignore").strip()
        if debug_activo:
            log_debug("SMTP", "Servidor: {}".format(linea))

        if len(linea) < 3 or not linea[:3].isdigit():
            continue

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
        return "Desconocida", []


def enviar_correo_bloques(asunto, modo_reporte=False, texto_telemetria="", debug_activo=False):
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
        return False

    try:
        _, hora_arranque, _ = obtener_tiempo_actual()
        gc.collect()  # liberar memoria usada por calculo de tiempo
        desfase_segundos = obtener_desfase_espana(obtener_unix_utc_real())
        gc.collect()

        if debug_activo:
            log_debug("SMTP", "Hora local calculada: {}".format(hora_arranque))

    except Exception as e_time:
        log_error("SMTP", "Fallo procesando hora local: {}".format(e_time))
        return False

    sock = None
    raw_sock = None

    try:
        gc.collect()  # --- antes de DNS ---
        if debug_activo:
            log_debug("SMTP", "Resolviendo DNS de smtp.gmail.com...")

        res_dns = socket.getaddrinfo("smtp.gmail.com", 465)
        sockaddr = res_dns[-1][-1]
        del res_dns       # liberar lista de resultados DNS
        gc.collect()

        if debug_activo:
            log_debug("SMTP", "Conectando a {}...".format(sockaddr))

        raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw_sock.settimeout(timeout_red)
        raw_sock.connect(sockaddr)
        gc.collect()
        time.sleep_ms(200)  # dejar estabilizar TCP antes del handshake SSL

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

        if debug_activo:
            log_debug("SMTP", "Esperando saludo 220...")
        _leer_respuesta_smtp(sock, 220, debug_activo=debug_activo)

        if debug_activo:
            log_debug("SMTP", "Enviando EHLO...")
        sock.write(b"EHLO esp32\r\n")
        _leer_respuesta_smtp(sock, 250, debug_activo=debug_activo, multilinea=True)

        if debug_activo:
            log_debug("SMTP", "Preparando credenciales...")

        import ubinascii  # lazy: solo se usa aqui
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

        del user_b64, pass_b64  # liberar credenciales codificadas
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
            cuerpo_limpio = str(texto_telemetria).replace("->", " pasa a ").replace("|", " ")
            cuerpo_limpio = cuerpo_limpio.replace("\r", " ").replace("\n", "\r\n")
            sock.write((cuerpo_limpio + "\r\n").encode())
            del cuerpo_limpio  # liberar si el cuerpo es grande
            gc.collect()
        else:
            gc.collect()  # --- antes de cargar agenda (puede ser grande) ---
            encabezado = "Reporte diario de pases {} {}\r\n".format(nombre_proyecto(), version())
            sock.write(encabezado.encode())
            sock.write(b"===========================\r\n")

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

            del fecha_agenda, pases  # liberar agenda de memoria
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
        return False

    finally:
        for s in (sock, raw_sock):
            try:
                if s:
                    s.close()
            except Exception:
                pass
        gc.collect()


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
