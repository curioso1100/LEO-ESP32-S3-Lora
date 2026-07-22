# =========================================================================
# MODULO: datos_satelites.py
# =========================================================================
import socket
import ssl
import gc
import json
import time
import os

from logger import log_info, log_debug, log_warn, log_error

from configuracion import obtener_config
from tiempo import obtener_tiempo_actual
CONFIG = obtener_config()

FILE_AGENDA = "agenda.json"

_OFFSET_VERANO_S  = 7200   # UTC+2 (CEST)
_OFFSET_INVIERNO_S = 3600  # UTC+1 (CET)


def obtener_desfase_espana(timestamp_utc):
    """Devuelve el desfase en segundos (7200 en verano, 3600 en invierno)
    calculando el ultimo domingo de marzo y octubre segun la norma europea."""
    tupla_utc = time.localtime(timestamp_utc)
    ano = tupla_utc[0]

    # Ultimo domingo de marzo  -> inicio horario de verano (01:00 UTC)
    t_marzo31   = time.mktime((ano, 3, 31, 1, 0, 0, 0, 0, 0)) - 946684800
    w_marzo     = time.localtime(t_marzo31 + 946684800)
    ultimo_domingo_marzo  = 31 - ((w_marzo[6] + 1) % 7)
    limite_verano = time.mktime((ano, 3, ultimo_domingo_marzo, 1, 0, 0, 0, 0, 0))

    # Ultimo domingo de octubre -> fin horario de verano (01:00 UTC)
    t_octubre31  = time.mktime((ano, 10, 31, 1, 0, 0, 0, 0, 0)) - 946684800
    w_octubre    = time.localtime(t_octubre31 + 946684800)
    ultimo_domingo_octubre = 31 - ((w_octubre[6] + 1) % 7)
    limite_invierno = time.mktime((ano, 10, ultimo_domingo_octubre, 1, 0, 0, 0, 0, 0))

    if limite_verano <= timestamp_utc < limite_invierno:
        return _OFFSET_VERANO_S
    return _OFFSET_INVIERNO_S


_DOPPLER_BAJO_HZ  =  3000   # elevacion < 20deg
_DOPPLER_MEDIO_HZ =  6000   # 20deg <= elevacion <= 60deg
_DOPPLER_ALTO_HZ  =  9000   # elevacion > 60deg


def _resolver_parametros_lora(info, grupo_data, c):
    # Extrae y resuelve los parametros LoRa con jerarquia satelite -> grupo -> global.
    def res(clave, defecto):
        return info.get(clave, grupo_data.get(clave, c.get(clave, defecto)))

    return {
        "sf": int(res("lora_sf", 7)),
        "cr": int(res("lora_cr", 5)),
        "bw_khz": float(res("ancho_banda_hz", 125000)) / 1000.0,
        "sw": int(res("lora_sync_word", 18)),
        "pr": int(res("lora_preamble_len", 8)),
    }


# =========================================================================
# NUEVO v7.4.1: Calcula horas automaticas de envio de estado entre pases
# =========================================================================
def _calcular_horas_estado_automaticas(pases_ordenados, desfase_segundos):
    """
    Recibe lista de pases ordenados por utc_ini_timestamp.
    Devuelve lista de strings "HH:MM" con las horas de envio de estado.

    Logica:
    - Inicializa lista vacia.
    - Recorre pases detectando superposiciones (grupos).
    - Para cada grupo, toma el utc_fin del ULTIMO pase del grupo.
    - Comprueba hueco hasta el inicio del siguiente grupo/pase.
    - Si hueco >= 15 minutos: anade (utc_fin_ultimo + 5 min) formateado.
    """
    if not pases_ordenados:
        return []

    horas_estado = []
    n = len(pases_ordenados)
    i = 0

    while i < n:
        # --- Identificar grupo de pases superpuestos ---
        # El grupo empieza en el pase i
        utc_fin_grupo = pases_ordenados[i]["utc_ini_timestamp"] + pases_ordenados[i]["duracion_min"] * 60
        j = i + 1

        # Mientras el siguiente pase empiece antes de que termine el grupo actual
        while j < n:
            utc_ini_sig = pases_ordenados[j]["utc_ini_timestamp"]
            if utc_ini_sig < utc_fin_grupo:
                # Superposicion: extender fin del grupo si este pase termina mas tarde
                utc_fin_sig = pases_ordenados[j]["utc_ini_timestamp"] + pases_ordenados[j]["duracion_min"] * 60
                if utc_fin_sig > utc_fin_grupo:
                    utc_fin_grupo = utc_fin_sig
                j += 1
            else:
                break

        # --- Comprobar hueco hasta el siguiente grupo/pase ---
        if j < n:
            utc_ini_siguiente = pases_ordenados[j]["utc_ini_timestamp"]
            hueco_segundos = utc_ini_siguiente - utc_fin_grupo

            if hueco_segundos >= 15 * 60:  # al menos 15 minutos
                hora_envio_utc = utc_fin_grupo + 5 * 60  # +5 minutos
                hora_local = hora_envio_utc + desfase_segundos
                t_local = time.localtime(hora_local)
                hora_str = "{:02d}:{:02d}".format(t_local[3], t_local[4])
                horas_estado.append(hora_str)
                log_debug("ESTADO_AUTO", "Hueco {} min -> hora estado: {}".format(
                    hueco_segundos // 60, hora_str))
            else:
                log_debug("ESTADO_AUTO", "Hueco {} min (insuficiente, < 15)".format(
                    hueco_segundos // 60))

        i = j  # Saltar al siguiente grupo

    return horas_estado


def _guardar_config_con_horas_estado(horas_estado):
    """NUEVO v7.4.1d: Reescribe SOLO la linea email_estado_horas_fijas
    preservando el formato original del config.json. Usa write() en lugar
    de writelines() (no disponible en MicroPython)."""

    CONFIG_FILE = "config.json"
    BACKUP_FILE = "config.json.bak"

    try:
        # --- Paso 1: Leer todo el contenido original ---
        with open(CONFIG_FILE, "r") as f:
            lineas = f.readlines()

        # --- Paso 2: Crear backup del original ---
        try:
            with open(BACKUP_FILE, "w") as fb:
                fb.write("".join(lineas))
        except Exception as e_bak:
            log_warn("ESTADO_AUTO", "No se pudo crear backup: {}".format(e_bak))

        # --- Paso 3: Construir la nueva linea ---
        if horas_estado:
            horas_formateadas = ", ".join(['"{}"'.format(h) for h in horas_estado])
            nueva_linea = '  "email_estado_horas_fijas": [ {} ],\n'.format(horas_formateadas)
        else:
            nueva_linea = '  "email_estado_horas_fijas": [],\n'

        # --- Paso 4: Buscar y reemplazar la linea ---
        indice_encontrado = -1
        for idx, linea in enumerate(lineas):
            if '"email_estado_horas_fijas"' in linea:
                indice_encontrado = idx
                break

        if indice_encontrado < 0:
            log_error("ESTADO_AUTO", "No se encontro la linea email_estado_horas_fijas en config.json")
            return False

        lineas[indice_encontrado] = nueva_linea

        # --- Paso 5: Escribir todo de golpe con write() ---
        contenido = "".join(lineas)
        with open(CONFIG_FILE, "w") as f:
            f.write(contenido)

        log_info("ESTADO_AUTO", "config.json actualizado. Horas: {}".format(horas_estado))
        return True

    except Exception as e:
        log_error("ESTADO_AUTO", "Fallo actualizando config.json: {}".format(e))
        # Intentar restaurar desde backup si existe
        try:
            if BACKUP_FILE in os.listdir():
                with open(BACKUP_FILE, "r") as fb:
                    backup_contenido = fb.read()
                with open(CONFIG_FILE, "w") as fo:
                    fo.write(backup_contenido)
                log_warn("ESTADO_AUTO", "config.json restaurado desde backup")
        except Exception as e_restore:
            log_error("ESTADO_AUTO", "No se pudo restaurar backup: {}".format(e_restore))
        return False


def descargar_agenda_completa(fecha_hoy):
    print(f"Descargando pases oficiales para el dia {fecha_hoy}...")
    pases_consolidados = []

    # -- Lectura de configuracion ----------------------------------------
    try:
        c = CONFIG
        ciudad        = c["ubicacion_actual"]
        geo           = c["configuracion_geografica"][ciudad]
        latitud       = geo["lat"]
        longitud      = geo["lon"]
        altitud       = geo["alt"]
        min_elevacion = geo["min_elev"]
        api_key       = c["n2yo_api_key"]
        timeout_red   = int(c["seguridad_hardware"]["timeout_red_segundos"])
        perfil_activo        = c["grupo_satelites_actual"]
        grupo_data           = c["perfiles_satelites"][perfil_activo]
        satelites_a_rastrear = grupo_data["satelites"]
        debug_consola        = c.get("debug_consola", True)
    except Exception as e:
        print("Error critico leyendo config.json:", e)
        return False

    # -- Bucle de descarga por satelite ----------------------------------
    for nombre_sat, info in satelites_a_rastrear.items():
        id_norad = info["id"]
        path = (
            f"/rest/v1/satellite/radiopasses/"
            f"{id_norad}/{latitud}/{longitud}/{altitud}/1/{min_elevacion}/"
            f"?apiKey={api_key}"
        )

        sock     = None
        raw_sock = None

        gc.collect()
        try:
            res_dns  = socket.getaddrinfo("api.n2yo.com", 443)
            ip_limpia = res_dns[0][4][0]

            raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            raw_sock.settimeout(timeout_red)
            raw_sock.connect((ip_limpia, 443))

            gc.collect()

            try:
                sock = ssl.wrap_socket(raw_sock, server_hostname="api.n2yo.com")
            except TypeError:
                sock = ssl.wrap_socket(raw_sock)

            peticion = f"GET {path} HTTP/1.1\r\nHost: api.n2yo.com\r\nConnection: close\r\n\r\n"
            sock.write(peticion.encode())

            # Lectura caracter a caracter hasta encontrar el inicio del JSON
            json_detectado   = False
            buffer_caracteres = ""
            while True:
                char_b = sock.read(1)
                if not char_b:
                    break
                char = char_b.decode("utf-8", "ignore")
                if not json_detectado:
                    if char == "{":
                        json_detectado = True
                    else:
                        continue
                buffer_caracteres += char

            fin_json = buffer_caracteres.rfind("}")
            if not json_detectado or fin_json == -1:
                log_warn("N2YO", f"{nombre_sat}: respuesta sin JSON valido.")
                continue  # pasa al siguiente satelite directamente

            cuerpo_json = buffer_caracteres[:fin_json + 1]
            json_data   = json.loads(cuerpo_json)

            if "passes" in json_data and json_data["passes"]:
                for p in json_data["passes"]:
                    utc_inicio = int(p.get("startUTC", 0))
                    utc_fin    = int(p.get("endUTC",   0))

                    if utc_inicio <= 0 or utc_fin <= utc_inicio:
                        log_warn("N2YO", f"{nombre_sat}: pase con timestamps invalidos, ignorado.")
                        continue

                    duracion_segundos = utc_fin - utc_inicio
                    duracion_minutos  = max(1, (duracion_segundos + 59) // 60)

                    desfase_inicio = obtener_desfase_espana(utc_inicio)
                    desfase_fin    = obtener_desfase_espana(utc_fin)
                    local_inicio   = utc_inicio + desfase_inicio
                    local_fin      = utc_fin    + desfase_fin

                    t_ini = time.localtime(local_inicio)
                    t_fin = time.localtime(local_fin)
                    elevacion_maxima = int(p.get("maxEl", 0))

                    if elevacion_maxima < 20:
                        delta_doppler = _DOPPLER_BAJO_HZ
                    elif elevacion_maxima <= 60:
                        delta_doppler = _DOPPLER_MEDIO_HZ
                    else:
                        delta_doppler = _DOPPLER_ALTO_HZ

                    lora = _resolver_parametros_lora(info, grupo_data, c)

                    pases_consolidados.append({
                        "sat":            nombre_sat,
                        "inicio":         f"{t_ini[3]:02d}:{t_ini[4]:02d}",
                        "fin":            f"{t_fin[3]:02d}:{t_fin[4]:02d}",
                        "elev":           elevacion_maxima,
                        "frec":           info["frec"],
                        "sf":             lora["sf"],
                        "cr":             lora["cr"],
                        "bw_khz":         lora["bw_khz"],
                        "sw":             lora["sw"],
                        "pr":             lora["pr"],
                        "implicit_header": bool(info.get("implicit_header", False)),
                        "payload_len":    int(info.get("payload_len", 255)),
                        "crc_on":         bool(info.get("crc_on", False)),
                        "rx_iq":          bool(info.get("rx_iq", False)),
                        "utc_ini_timestamp": utc_inicio,
                        "duracion_min":   duracion_minutos,
                        "delta_doppler_hz": delta_doppler,
                    })

                if debug_consola:
                    print(f"-> {nombre_sat}: Descargado (Doppler autoajustado para {elevacion_maxima} grados).")

        except Exception as e:
            log_error("N2YO", "{}: {}".format(nombre_sat, e))

        finally:
            try:
                if sock:
                    sock.close()
            except:
                pass
            try:
                if raw_sock:
                    raw_sock.close()
            except:
                pass
            gc.collect()
            time.sleep_ms(150)

    # -- Serializacion y guardado ----------------------------------------
    try:
        # Ordenacion cronologica por marca Unix
        pases_consolidados.sort(key=lambda x: x["utc_ini_timestamp"])

        # NUEVO v7.4.1: Calcular horas de estado automaticas si esta activado
        email_estado_automatico = c.get("email_estado_automatico", False)
        if email_estado_automatico:
            log_info("ESTADO_AUTO", "Modo automatico activado. Calculando horas de estado...")
            # Usar desfase del primer pase (o actual) para conversion a hora local
            desfase_actual = obtener_desfase_espana(int(time.time()))
            horas_estado = _calcular_horas_estado_automaticas(pases_consolidados, desfase_actual)
            _guardar_config_con_horas_estado(horas_estado)
        else:
            log_debug("ESTADO_AUTO", "Modo automatico desactivado. Sin cambios en horas de estado.")

        if debug_consola:
            print("[DEBUG] Iniciando construccion de texto plano para",
                  len(pases_consolidados), "pases...")

        lineas_pases = []
        for idx, p in enumerate(pases_consolidados):
            if debug_consola:
                print("[DEBUG] Procesando pase index:", idx, "Sat:", p["sat"])

            bloque = (
                "    {\n"
                '      "satelite": {"nombre": "' + str(p["sat"]) + '", "max_elevacion": ' + str(p["elev"]) + ', "delta_doppler_hz": ' + str(p["delta_doppler_hz"]) + '},\n'
                '      "tiempo": {"inicio": "' + str(p["inicio"]) + '", "fin": "' + str(p["fin"]) + '", "duracion_min": ' + str(p["duracion_min"]) + ', "utc_ini_timestamp": ' + str(p["utc_ini_timestamp"]) + '},\n'
                '      "lora": {"frecuencia_hz": ' + str(p["frec"]) +
                ', "bw_khz": '       + str(p["bw_khz"]) +
                ', "sf": '           + str(p["sf"]) +
                ', "cr": '           + str(p["cr"]) +
                ', "sync_word": '    + str(p["sw"]) +
                ', "preamble_len": ' + str(p["pr"]) +
                ', "implicit_header": ' + str(p["implicit_header"]).lower() +
                ', "payload_len": '  + str(p["payload_len"]) +
                ', "crc_on": '       + str(p["crc_on"]).lower() +
                ', "rx_iq": '        + str(p["rx_iq"]).lower() +
                '}\n'
                "    }"
            )
            lineas_pases.append(bloque)

        cuerpo_pases = ",\n".join(lineas_pases)
        json_final = (
            "{\n"
            '  "fecha_creacion": "' + str(fecha_hoy) + '",\n'
            '  "pases": [\n'
            + cuerpo_pases +
            "\n  ]\n"
            "}"
        )

        if debug_consola:
            print("[DEBUG] JSON ensamblado con exito. Guardando...")

        with open(FILE_AGENDA, "w") as f:
            f.write(json_final)

        print(f"-> [SUCCESS] Agenda lista: {len(pases_consolidados)} pases procesados y guardados.")
        return True

    except Exception as e_sort:
        log_error("AGENDA", str(e_sort))
        return False


def obtener_objeto_satelite(utc_api_actual, debug_activo=True):
    """
    Busca si hay un satelite activo en el cielo basandose estrictamente
    en marcas de tiempo Unix.
    """
    try:
        with open(FILE_AGENDA, "r") as archivo_test:
            agenda_local = json.load(archivo_test)
    except Exception as e:
        log_warn("AGENDA", f"No se pudo leer {FILE_AGENDA}: {e}")
        return None

    pases = agenda_local.get("pases", [])
    for p in pases:
        ts_inicio = int(p["tiempo"]["utc_ini_timestamp"])
        ts_fin    = ts_inicio + int(p["tiempo"]["duracion_min"]) * 60
        if ts_inicio <= utc_api_actual <= ts_fin:
            return p
    return None


# =========================================================================
# NUEVO v7.5.1: Obtener horas pendientes de envio de estado
# Movido desde fase4.py para desacoplar logica de tiempo del envio de emails.
# =========================================================================
def obtener_horas_pendientes_estado():
    """Devuelve lista de horas fijas de estado pendientes de envio.
    Gestiona correctamente el cruce de medianoche en la lista de horas."""
    horas_fijas = CONFIG.get("email_estado_horas_fijas", [])
    if not horas_fijas:
        return []

    # Obtener hora local actual
    utc_unix, _, t_local = obtener_tiempo_actual()
    hora_actual_min = t_local[3] * 60 + t_local[4]  # minutos desde 00:00

    # Convertir todas las horas fijas a minutos desde 00:00
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

    # --- PASO 1: Dividir en horas_hoy y horas_manana ---
    # horas_hoy: desde el inicio hasta la ultima hora antes del "salto" a manana
    # horas_manana: desde el primer "salto" detectado hasta el final
    # Un "salto" es cuando una hora es menor que la anterior (ej: 23:24 -> 00:13)
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

    # --- PASO 2: Determinar si estamos en "hoy" o "manana" ---
    primera_hora_min = horas_min[0][0]
    es_hoy = (hora_actual_min >= primera_hora_min)

    # --- PASO 3: Aplicar logica segun caso ---
    pendientes = []

    if es_hoy:
        # CASO A y B: Estamos en el dia de hoy
        encontrada_en_hoy = False
        for minutos, h_str in horas_hoy:
            if minutos > hora_actual_min:
                pendientes.append(h_str)
                encontrada_en_hoy = True
            elif encontrada_en_hoy:
                pendientes.append(h_str)

        # Siempre anadir todas las horas_manana (son del dia siguiente)
        for minutos, h_str in horas_manana:
            pendientes.append(h_str)

    else:
        # CASO C: Ya es "manana" (paso medianoche)
        # Descartar horas_hoy completamente
        for minutos, h_str in horas_manana:
            if minutos > hora_actual_min:
                pendientes.append(h_str)

    return pendientes


# =========================================================================
# BLOQUE DE DIAGNOSTICO AUTONOMO
# =========================================================================
if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("--- DIAGNOSTICO AUTONOMO DE DATOS_SATELITES ---")
    print("=" * 60)

    import network

    try:
        with open("config.json", "r") as cf:
            c = json.load(cf)
            ssid         = c["wifi_ssid"]
            password     = c["wifi_pass"]
            max_intentos = int(c["seguridad_hardware"].get("max_intentos_wifi", 10))
        print("[OK] Credenciales leidas de config.json con exito.")
    except Exception as e_cfg:
        print("[ERROR CRITICO] No se pudo leer config.json:", e_cfg)
        ssid, password, max_intentos = None, None, 0

    if ssid:
        print(f"[DIAGNOSTICO RED] Conectando a SSID: '{ssid}'...")
        wlan = network.WLAN(network.STA_IF)
        try:
            if wlan.isconnected():
                wlan.disconnect()
            wlan.active(False)
            time.sleep_ms(200)
        except:
            pass

        wlan.active(True)
        wlan.connect(ssid, password)

        intentos = 0
        while not wlan.isconnected() and intentos < max_intentos:
            print(f" [WiFi] Intentando... ({intentos + 1}/{max_intentos})")
            time.sleep(2)
            intentos += 1

        if wlan.isconnected():
            print(f"[OK] WiFi Conectado! IP: {wlan.ifconfig()[0]}")
            fecha_hoy = "{:04d}-{:02d}-{:02d}".format(*time.localtime()[:3])
            descargar_agenda_completa(fecha_hoy)
            wlan.active(False)
            print("[INFO] WiFi desconectado para cerrar el diagnostico.")
        else:
            print("[ERROR] Imposible conectar al router.")
