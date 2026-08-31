# ==========================================================================
# MÓDULO: fase3_utils.py - Funciones auxiliares del bucle principal
# ==========================================================================

import time
import json
import gc
import os

from logger import (
    log_info, log_debug, log_warn, log_exception, log_persistente,
    escribir_captura, leer_errores_para_email
)
from tiempo_satelites import obtener_tiempo_actual

# AGENDA
def agenda_caducada(t_local):
    # Devuelve True si la agenda no corresponde al dia actual
    try:
        fecha_actual_str = "{:04d}-{:02d}-{:02d}".format(t_local[0], t_local[1], t_local[2])
        with open("agenda.json", "r") as aj_check:
            agenda = json.load(aj_check)
        gc.collect()
        if agenda.get("fecha_creacion", "") != fecha_actual_str:
            log_warn("AGENDA", "Fecha caducada ({}) -> regenerando".format(fecha_actual_str))
            return True
        return False
    except Exception as e:
        log_exception("AGENDA", e)
        return False

def mostrar_proximos_pases(utc_actual, reloj_str):
    try:
        with open("agenda.json", "r") as f:
            agenda = json.load(f)
        pases = agenda.get("pases", [])
    except json.JSONDecodeError:
        log_warn("AGENDA", "agenda.json corrupto")
        print("[AGENDA] {} | Error: agenda.json corrupto".format(reloj_str))
        return
    except:
        print("[AGENDA] Sin agenda.json disponible")
        return

    futuros = []
    for p in pases:
        ts_inicio = int(p["tiempo"]["utc_ini_timestamp"])
        ts_fin = ts_inicio + int(p["tiempo"]["duracion_min"]) * 60
        if utc_actual > ts_fin:
            continue
        futuros.append(p)

    if not futuros:
        print("[AGENDA] {} | Sin pases programados".format(reloj_str))
        return

    print("[AGENDA] {} | Proximos pases:".format(reloj_str))
    for i, p in enumerate(futuros[:2]):
        ini = p["tiempo"]["inicio"]
        fin = p["tiempo"]["fin"]
        nom = p["satelite"]["nombre"]
        el = p["satelite"]["max_elevacion"]
        ts_ini = int(p["tiempo"]["utc_ini_timestamp"])
        if utc_actual >= ts_ini:
            marca = " <<< ACTIVO AHORA"
        else:
            mins = (ts_ini - utc_actual) // 60
            marca = " (en {} min)".format(mins) if mins > 0 else " (en <1 min)"
        print("  #{} {}-{} {:12} (Elev:{}°){}".format(i+1, ini, fin, nom, el, marca))
    print("")

def mostrar_estado_pase(sat_objeto, params, sweep_cfg, doppler_activo):
    if sat_objeto is None:
        return
    duracion_total = int(sat_objeto["tiempo"]["duracion_min"] * 60)
    transcurrido = max(0, params["utc_unix"] - sat_objeto["tiempo"]["utc_ini_timestamp"])
    tercio = max(1, duracion_total // 3)
    if transcurrido < tercio:
        tramo_txt = "AOS"
    elif transcurrido < (tercio * 2):
        tramo_txt = "TCA"
    else:
        tramo_txt = "LOS"
    print("[PASE] {} | ElevMax:{}° | Dur:{}s | Trans:{}s | Tramo:{} | Doppler:{}".format(
        sat_objeto["satelite"]["nombre"],
        sat_objeto["satelite"]["max_elevacion"],
        duracion_total, transcurrido, tramo_txt,
        "ON" if doppler_activo else "OFF"))

# RECEPCIÓN
def procesar_recepcion(radio, sat_objeto, sweep, identificador,
                        paquetes_capturados, paquetes_descartados, debug=False):
    datos_raw, estado_rx, rssi, snr = radio.leer_paquete()

    paquete_valido = (
        datos_raw is not None and
        len(datos_raw) > 0 and
        (estado_rx == 0 or estado_rx == -7 or sat_objeto is not None)
    )

    if paquete_valido:
        log_info("RX", "[!] PAQUETE CAZADO! estado={} len={} RSSI={} SNR={}".format(
            estado_rx, len(datos_raw), rssi, snr))
        try:
            paquete_hex = datos_raw.hex() if hasattr(datos_raw, "hex") else str(datos_raw)
            _, reloj_pantalla_str, _ = obtener_tiempo_actual()

            sat_nombre_detectado = identificador.identificar(datos_raw)

            if sat_objeto is not None and sat_nombre_detectado is not None:
                sat_activo = sat_objeto["satelite"]["nombre"]
                if identificador.misma_familia(sat_nombre_detectado, sat_activo):
                    sat_nombre_detectado = sat_activo

            # Fallback por frecuencia si no hay match por header
            if sat_nombre_detectado is None and sat_objeto is not None:
                try:
                    lora_cfg = sat_objeto.get("lora")
                    if lora_cfg is not None and "frecuencia_hz" in lora_cfg:
                        frec_nominal = lora_cfg["frecuencia_hz"] / 1000000.0
                        diff_khz = abs(radio.frecuencia - frec_nominal) * 1000
                        if diff_khz <= 100:   # 100 kHz de margen
                            sat_nombre_detectado = sat_objeto["satelite"]["nombre"]
                            log_info("ID_FALLBACK",
                                "Header no reconocido, pero frecuencia coincide con pase activo: "
                                "{} @ {:.3f}MHz (diff {:.1f}kHz)".format(
                                sat_nombre_detectado, radio.frecuencia, diff_khz))
                    else:
                        # Usa log_persistente para diagnóstico remoto en el techo
                        log_persistente("ID_FALLBACK",
                            "Satelite activo {} no tiene config 'lora' valida para fallback".format(
                            sat_objeto["satelite"].get("nombre", "???")),
                            nivel="WARN")
                except Exception as e:
                    # Persistir para diagnóstico remoto
                    log_persistente("ID_FALLBACK", "Error en fallback por frecuencia: {}".format(e), nivel="WARN")

            if sat_nombre_detectado is not None:
                sat_nombre = sat_nombre_detectado
                try:
                    frec_esperada = identificador.frecuencia_nominal(sat_nombre)
                except Exception as e:
                    # Persistir para diagnóstico remoto
                    log_persistente("ID", "Error obteniendo frecuencia nominal de {}: {}".format(sat_nombre, e), nivel="WARN")
                    frec_esperada = None
                if frec_esperada is not None:
                    diff_khz = abs(radio.frecuencia - frec_esperada) * 1000
                    if diff_khz > 10:
                        log_warn("RX", "Header dice {} pero frec={:.3f}MHz (esperada {:.3f}MHz, diff={:.1f}kHz)".format(
                            sat_nombre, radio.frecuencia, frec_esperada, diff_khz))
                log_info("RX", "Satelite identificado: {} | Pase activo: {} | Frec: {:.3f}MHz".format(
                    sat_nombre,
                    sat_objeto["satelite"]["nombre"] if sat_objeto else "NINGUNO",
                    radio.frecuencia))
                if sat_objeto is None:
                    try:
                        frec_nom = identificador.frecuencia_nominal(sat_nombre)
                        if frec_nom is not None:
                            radio.forzar_frecuencia(frec_nom)
                    except Exception as e:
                        # Persistir para diagnóstico remoto
                        log_persistente("ID", "Error forzando frecuencia para {}: {}".format(sat_nombre, e), nivel="WARN")
            else:
                sat_nombre = "DESCONOCIDO"

            buscar_activo = sweep._debe_buscar(sat_objeto)
            modo = "BQ" if buscar_activo else "N"
            estado_rx_str = "OK" if estado_rx == 0 else ("CRC_ERR" if estado_rx == -7 else str(estado_rx))

            escribir_captura("satelites_cazados.txt", sat_nombre, reloj_pantalla_str,
                             radio.frecuencia, radio.sf, radio.bw, radio.cr, radio.sync_word,
                             radio.rx_iq, radio.crc_on, False, 255, paquete_hex, modo, rssi, snr)

            log_info("CAPTURA", "SAT={} | HEX={} | ESTADO_RX={} | LEN={} | RSSI={} | SNR={} | FREC_RX={:.3f}MHz".format(
                sat_nombre, paquete_hex[:40] + "..." if len(paquete_hex) > 40 else paquete_hex,
                estado_rx_str, len(datos_raw), rssi, snr, radio.frecuencia))

            if debug:
                print("*** PAQUETE RECIBIDO DE {} ***".format(sat_nombre))
                print("  HEX: {}".format(paquete_hex))
                print("  LEN: {} bytes".format(len(datos_raw)))
                print("  RSSI: {} dBm".format(rssi))
                print("  SNR: {} dB".format(snr))
                print("*** ACUMULADO - CONTINUANDO ESCUCHA ***")

            paquetes_capturados[0] += 1
            if buscar_activo:
                sweep.lock()
            os.sync()
        except Exception as e:
            # Persistir el error en errores.log para diagnóstico remoto en el techo
            log_exception("CAPTURA", e)
            import sys
            sys.print_exception(e)
            log_persistente("CAPTURA", "Excepcion en procesar_recepcion: {}".format(e), nivel="ERROR")

    # Loguear como WARNING los paquetes descartados para diagnóstico
    elif datos_raw is not None and len(datos_raw) > 0:
        paquetes_descartados[0] += 1
        hex_preview = datos_raw.hex()[:20] if hasattr(datos_raw, "hex") else str(datos_raw)[:20]
        log_warn("RX", "[DESCARTADO] estado={} len={} hex={} sat={}".format(
            estado_rx, len(datos_raw), hex_preview,
            sat_objeto["satelite"]["nombre"] if sat_objeto else "BASE"))

    gc.collect()
    return sweep.locked

# EMAIL / ESTADO
def leer_ultimos_heartbeats(max_lineas=None):
    # Lee las últimas líneas de heartbeat.log para incluir en el email. Si max_lineas es None, usa el valor de config.json (max_hb_acumulados)
    if max_lineas is None:
        try:
            from config_system import obtener_config
            max_lineas = int(obtener_config().get("max_hb_acumulados", 200))
        except Exception:
            max_lineas = 200
    try:
        with open("heartbeat.log", "r") as f:
            todas = [l.strip() for l in f.readlines() if l.strip()]
        return todas[-max_lineas:] if len(todas) > max_lineas else todas
    except OSError:
        return []

def contar_capturas_pendientes(fichero="satelites_cazados.txt"):
    try:
        with open(fichero, "r") as f:
            return len([l for l in f.readlines() if l.strip()])
    except:
        return 0

def preparar_estado_pendiente(temp_cpu, ventilador_on, fs_libre_kb,
                               paquetes_capturados, paquetes_descartados,
                               max_hb_lineas=None):
    # Construye estado_pendiente.json. NO llama guardar_fase()
    log_debug('EMAIL', 'Preparando estado pendiente para fase4...')
    hb_lines = leer_ultimos_heartbeats(max_lineas=max_hb_lineas)
    hb_count = len(hb_lines)
    print("[EMAIL-DEBUG] === preparar_estado_pendiente() === HB={} CAP={}".format(
        hb_count, contar_capturas_pendientes()))

    capturas = []
    try:
        with open("satelites_cazados.txt", "r") as f:
            for line in f:
                if line.strip() and not line.strip().startswith("#"):
                    capturas.append(line.strip())
                    if len(capturas) >= 50:
                        break
    except OSError:
        pass
    gc.collect()

    # Leer flag de reinicio programado para incluirlo en el email de estado (una sola vez)
    reinicio_prog_info = ""
    try:
        with open("reinicio_prog.flag", "r") as f:
            reinicio_prog_info = f.read().strip()
        # Borrar tras leer para no repetir la nota en emails futuros
        try:
            os.remove("reinicio_prog.flag")
        except Exception:
            pass
    except Exception:
        pass

    try:
        estado_pendiente = {
            "tipo": "estado",
            "timestamp": time.time(),
            "heartbeats": hb_lines,
            "capturas_count": len(capturas),
            "capturas": capturas[-50:] if capturas else [],
            "temp_cpu": temp_cpu,
            "ventilador_on": ventilador_on,
            "fs_libre_kb": fs_libre_kb,
            "paquetes_capturados": paquetes_capturados[0],
            "paquetes_descartados": paquetes_descartados[0],
            "estado_enviado": False,
            'errores': ''
        }
        errores_texto = leer_errores_para_email('errores.log')
        if reinicio_prog_info:
            estado_pendiente['errores'] = "[NOTA] Reinicio programado: {}\n\n{}".format(reinicio_prog_info, errores_texto)
        else:
            estado_pendiente['errores'] = errores_texto
        gc.collect()
        with open("estado_pendiente.json", "w") as f:
            json.dump(estado_pendiente, f)
            f.flush()
            os.sync()
        gc.collect()
        log_info("EMAIL", "Estado pendiente guardado ({} HB, {} CAP)".format(
            hb_count, len(capturas)))
        if capturas:
            try:
                os.remove("satelites_cazados.txt")
            except Exception:
                pass
        gc.collect()
        return estado_pendiente
    except Exception as e:
        log_exception("EMAIL_ESTADO", e)
        return None

# NTP
def ntp_requiere_sync():
    # Devuelve True si el RTC indica ano < 2026 (corrupto)
    if time.localtime()[0] >= 2026:
        return False
    log_warn("RTC", "RTC corrupto - se requiere sincronizacion NTP")
    return False
  