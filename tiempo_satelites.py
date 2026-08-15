# =========================================================================
# MÓDULO: tiempo_satelites.py  
# =========================================================================

import time
import machine
import os
import socket
import ssl
import gc
import json

from logger import log_info, log_debug, log_warn, log_error, log_persistente
from config_system import obtener_config


# -------------------------------------------------------------------------
# CONSTANTES
# -------------------------------------------------------------------------

_EPOCH_OFFSET = 946684800   # Diferencia entre epoch MicroPython (2000) y Unix (1970)

_OFFSET_VERANO_S  = 7200   # UTC+2 (CEST)
_OFFSET_INVIERNO_S = 3600  # UTC+1 (CET)

_DOPPLER_BAJO_HZ  =  3000   # elevacion < 20deg
_DOPPLER_MEDIO_HZ =  6000   # 20deg <= elevacion <= 60deg
_DOPPLER_ALTO_HZ  =  9000   # elevacion > 60deg

FILE_AGENDA = "agenda.json"

CONFIG = obtener_config()


# =========================================================================
# FUNCIONES DE TIEMPO (antes en tiempo.py)
# =========================================================================

def obtener_unix_utc_real():
    # Devuelve timestamp Unix real (epoch 1970) compensando el offset de MicroPython
    return int(time.time()) + _EPOCH_OFFSET


def es_entorno_thonny():
    # Detecta si estamos ejecutando bajo Thonny (soft reset o presencia de directorio)
    try:
        return ("thonny" in os.listdir("/")) or (machine.reset_cause() == 5)
    except:
        return False


def corregir_hora_thonny():
    # Corrige la hora cuando el RTC está desfasado por reinicios de Thonny
    try:
        t = time.localtime()
        unix_local = int(time.mktime(t)) + _EPOCH_OFFSET
        desfase = obtener_desfase_espana(unix_local)
        return unix_local - desfase
    except:
        return obtener_unix_utc_real()


def obtener_tiempo_actual():
    # Devuelve (utc_unix, reloj_str, t_local) teniendo en cuenta corrección Thonny
    if es_entorno_thonny():
        utc_unix = corregir_hora_thonny()
    else:
        utc_unix = obtener_unix_utc_real()

    gc.collect()
    desfase = obtener_desfase_espana(utc_unix)
    local_unix = utc_unix + desfase
    t_local = time.localtime(local_unix - _EPOCH_OFFSET)
    # V9.1: incluir fecha completa para eliminar ambiguedad en logs y emails
    reloj_pantalla_str = "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(
        t_local[0], t_local[1], t_local[2], t_local[3], t_local[4], t_local[5])
    return utc_unix, reloj_pantalla_str, t_local


def parsear_timestamp(ts_str):
    # Convierte '2026-07-16T11:49:57' a timestamp Unix (epoch 1970). Devuelve None si el formato no es válido.
    try:
        partes = ts_str.split("T")
        if len(partes) != 2:
            return None
        fecha = partes[0].split("-")
        hora = partes[1].split(":")
        if len(fecha) != 3 or len(hora) < 2:
            return None
        anio = int(fecha[0])
        mes = int(fecha[1])
        dia = int(fecha[2])
        h = int(hora[0])
        m = int(hora[1])
        s = int(hora[2]) if len(hora) > 2 else 0
        t_mp = (anio, mes, dia, h, m, s, 0, 0)
        return int(time.mktime(t_mp)) + _EPOCH_OFFSET
    except Exception:
        return None


def formatear_fecha_utc(timestamp):
    """Convierte timestamp Unix real (epoch 1970) a 'YYYY-MM-DD HH:MM:SS UTC'.
    IMPORTANTE: time.localtime() en MicroPython ESP32 usa epoch 2000,
    por eso restamos _EPOCH_OFFSET antes de pasar el timestamp.
    """
    try:
        t = time.localtime(timestamp - _EPOCH_OFFSET)
        return "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d} UTC".format(
            t[0], t[1], t[2], t[3], t[4], t[5])
    except Exception:
        return str(timestamp)


def formatear_fecha_local(timestamp):
    """Convierte timestamp Unix real (epoch 1970) a hora local española (CEST/CET)."""
    try:
        desfase = obtener_desfase_espana(timestamp)
        local_ts = timestamp + desfase
        t = time.localtime(local_ts - _EPOCH_OFFSET)
        sufijo = "CEST" if desfase == _OFFSET_VERANO_S else "CET"
        return "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d} {}".format(
            t[0], t[1], t[2], t[3], t[4], t[5], sufijo)
    except Exception:
        return str(timestamp)

# =========================================================================
# FUNCIONES DE SATÉLITES (antes en datos_satelites.py)
# =========================================================================

def obtener_desfase_espana(timestamp_utc):
    # Devuelve el desfase en segundos (7200 en verano, 3600 en invierno)
    # calculando el ultimo domingo de marzo y octubre segun la norma europea.
    # NOTA CRITICA: timestamp_utc viene en epoch Unix (1970), pero time.mktime()
    # y time.localtime() en MicroPython ESP32 usan epoch 2000. Convertimos
    # a epoch 2000 antes de operar para que la comparacion sea coherente.
    timestamp_mp = timestamp_utc - _EPOCH_OFFSET

    tupla_utc = time.localtime(timestamp_mp)
    ano = tupla_utc[0]

    # Ultimo domingo de marzo -> inicio horario de verano (01:00 UTC, epoch 2000)
    t_marzo31 = time.mktime((ano, 3, 31, 1, 0, 0, 0, 0, 0))
    w_marzo = time.localtime(t_marzo31)
    ultimo_domingo_marzo = 31 - ((w_marzo[6] + 1) % 7)
    limite_verano = time.mktime((ano, 3, ultimo_domingo_marzo, 1, 0, 0, 0, 0, 0))

    # Ultimo domingo de octubre -> fin horario de verano (01:00 UTC, epoch 2000)
    t_octubre31 = time.mktime((ano, 10, 31, 1, 0, 0, 0, 0, 0))
    w_octubre = time.localtime(t_octubre31)
    ultimo_domingo_octubre = 31 - ((w_octubre[6] + 1) % 7)
    limite_invierno = time.mktime((ano, 10, ultimo_domingo_octubre, 1, 0, 0, 0, 0, 0))

    if limite_verano <= timestamp_mp < limite_invierno:
        return _OFFSET_VERANO_S
    return _OFFSET_INVIERNO_S

def _resolver_parametros_lora(info, grupo_data, c):
    def res(clave, defecto):
        return info.get(clave, grupo_data.get(clave, c.get(clave, defecto)))

    return {
        "sf": int(res("lora_sf", 7)),
        "cr": int(res("lora_cr", 5)),
        "bw_khz": float(res("ancho_banda_hz", 125000)) / 1000.0,
        "sw": int(res("lora_sync_word", 18)),
        "pr": int(res("lora_preamble_len", 8)),
    }


def descargar_agenda_completa(fecha_hoy):
    print(f"Descargando pases oficiales para el dia {fecha_hoy}...")
    pases_consolidados = []

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

    satelites_con_pases = 0
    satelites_fallidos = []
    total_satelites = len(satelites_a_rastrear)
    min_pct = int(c.get("min_satelites_porcentaje", 75))
    # Mínimo de satélites con pases necesarios para alcanzar el umbral (redondeo hacia arriba)
    min_satelites_necesarios = (total_satelites * min_pct + 99) // 100

    for i, (nombre_sat, info) in enumerate(satelites_a_rastrear.items()):
        # Early abort — si incluso descargando todos los satélites restantes
        # no podemos alcanzar el umbral, paramos el bucle para ahorrar tiempo
        satelites_restantes = total_satelites - i
        if satelites_con_pases + satelites_restantes < min_satelites_necesarios:
            msg_abort = "Abortando descarga temprana: imposible alcanzar umbral {}% (max posible: {}/{}, con pases: {})".format(
                min_pct, satelites_con_pases + satelites_restantes, total_satelites, satelites_con_pases)
            log_warn("N2YO", msg_abort)
            log_persistente("N2YO", msg_abort, "WARN")
            break

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
                log_persistente("N2YO", f"{nombre_sat}: respuesta sin JSON valido.", "WARN")
                satelites_fallidos.append(nombre_sat)
                continue  # pasa al siguiente satelite directamente

            cuerpo_json = buffer_caracteres[:fin_json + 1]
            json_data   = json.loads(cuerpo_json)

            if "passes" in json_data and json_data["passes"]:
                satelites_con_pases += 1
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
            log_persistente("N2YO", "{}: {}".format(nombre_sat, e), "ERROR")
            satelites_fallidos.append(nombre_sat)

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

    if total_satelites > 0:
        porcentaje = (satelites_con_pases / total_satelites) * 100
        if porcentaje < min_pct:
            log_error("N2YO", "Umbral no alcanzado: {}/{} satelites con pases ({:.0f}%, min {}%)".format(
                satelites_con_pases, total_satelites, porcentaje, min_pct))
            log_persistente("N2YO", "Umbral no alcanzado: {}/{} satelites con pases ({:.0f}%, min {}%)".format(
                satelites_con_pases, total_satelites, porcentaje, min_pct), "ERROR")
            return False
        elif satelites_fallidos:
            log_warn("N2YO", "Agenda parcial: {}/{} satelites descargados ({:.0f}%). Faltaron: {}".format(
                satelites_con_pases, total_satelites, porcentaje, ", ".join(satelites_fallidos)))
            log_persistente("N2YO", "Agenda parcial: {}/{} satelites descargados ({:.0f}%). Faltaron: {}".format(
                satelites_con_pases, total_satelites, porcentaje, ", ".join(satelites_fallidos)), "WARN")

    if len(pases_consolidados) == 0:
        log_error("N2YO", "Agenda vacia: ningun satelite tiene pases programados para hoy")
        return False

    try:
        pases_consolidados.sort(key=lambda x: x["utc_ini_timestamp"])

        email_estado_automatico = c.get("email_estado_automatico", False)
        if email_estado_automatico:
            try:
                from datos_satelites import _calcular_horas_estado_automaticas, _guardar_config_con_horas_estado
                log_info("ESTADO_AUTO", "Modo automatico activado. Calculando horas de estado...")
                desfase_actual = obtener_desfase_espana(int(time.time()))
                horas_estado = _calcular_horas_estado_automaticas(pases_consolidados, desfase_actual)
                _guardar_config_con_horas_estado(horas_estado)
            except Exception as e_alert:
                log_warn("ESTADO_AUTO", "No se pudo calcular horas automaticas: {}".format(e_alert))
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
    # Busca si hay un satelite activo en el cielo basandose estrictamente en marcas de tiempo Unix.
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


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("--- DIAGNOSTICO AUTONOMO DE DATOS_SATELITES ---")
    print("=" * 60)


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
        import network
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
