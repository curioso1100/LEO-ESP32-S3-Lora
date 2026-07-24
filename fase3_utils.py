# =========================================================================
# MÓDULO: fase3_utils.py - Funciones auxiliares del bucle principal
# =========================================================================


import time
import json
import gc
import os

from logger import (
    log_info, log_debug, log_warn, log_exception, log_persistente,
    escribir_captura, escribir_heartbeat, leer_errores_para_email
)
from tiempo_satelites import obtener_tiempo_actual
from config_system import guardar_fase


# =========================================================================
# AGENDA
# =========================================================================

def verificar_agenda_o_reiniciar(radio, t_local):
    try:
        fecha_actual_str = "{:04d}-{:02d}-{:02d}".format(t_local[0], t_local[1], t_local[2])
        with open("agenda.json", "r") as aj_check:
            agenda = json.load(aj_check)
        gc.collect()
        if agenda.get("fecha_creacion", "") != fecha_actual_str:
            log_warn("AGENDA", "Fecha caducada ({}) -> regenerando".format(fecha_actual_str))
            guardar_fase(1)
            radio.standby()
            time.sleep_ms(500)
            from placa import reiniciar
            reiniciar()
    except Exception as e:
        log_exception("AGENDA", e)


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


# =========================================================================
# RECEPCIÓN
# =========================================================================

def procesar_recepcion(radio, sat_objeto, sweep, identificador,
                        paquetes_capturados, paquetes_descartados, debug=False):
    datos_raw, estado_rx, rssi, snr = radio.leer_paquete()
    if datos_raw is not None and len(datos_raw) > 0 and (estado_rx == 0 or estado_rx == -7):
        log_info("RX", "[!] PAQUETE CAZADO! estado={} len={} RSSI={} SNR={}".format(
            estado_rx, len(datos_raw), rssi, snr))
        try:
            paquete_hex = datos_raw.hex() if hasattr(datos_raw, "hex") else str(datos_raw)
            _, reloj_pantalla_str, _ = obtener_tiempo_actual()

            sat_nombre_detectado = identificador.identificar(datos_raw)

            # Desambiguación por familia de header + pase activo
            if sat_objeto is not None and sat_nombre_detectado is not None:
                sat_activo = sat_objeto["satelite"]["nombre"]
                if identificador.misma_familia(sat_nombre_detectado, sat_activo):
                    sat_nombre_detectado = sat_activo

            if sat_nombre_detectado is not None:
                sat_nombre = sat_nombre_detectado
                frec_esperada = identificador.frecuencia_nominal(sat_nombre)
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
                    frec_nom = identificador.frecuencia_nominal(sat_nombre)
                    if frec_nom is not None:
                        radio.forzar_frecuencia(frec_nom)
            else:
                # FIX: No atribuir al satélite del pase cuando no hay coincidencia
                sat_nombre = "DESCONOCIDO"

            buscar_activo = sweep._debe_buscar(sat_objeto)
            modo = "BUSQUEDA" if buscar_activo else "NORMAL"
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
            log_exception("CAPTURA", e)
    elif datos_raw is not None and len(datos_raw) > 0 and estado_rx != 0 and estado_rx != -7:
        paquetes_descartados[0] += 1
        log_info("RX", "[DESCARTADO] Paquete con estado={}, len={}".format(estado_rx, len(datos_raw)))
    return sweep.locked


# =========================================================================
# EMAIL / ESTADO
# =========================================================================

def leer_ultimos_heartbeats(max_lineas=200):
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


def enviar_email_estado(email, temp_cpu, ventilador_on, fs_libre_kb,
                         paquetes_capturados, paquetes_descartados):
    log_debug('EMAIL', 'Preparando estado pendiente para fase4...')
    hb_lines = leer_ultimos_heartbeats()
    hb_count = len(hb_lines)
    print("[EMAIL-DEBUG] === enviar_email_estado() === HB={} CAP={}".format(
        hb_count, contar_capturas_pendientes()))

    if not email._horas_fijas and email._email_cada_seg <= 0:
        print("[EMAIL-DEBUG] EMAIL:OFF - ambos mecanismos desactivados")
        return True

    email.marcar_enviado()
    capturas = []
    try:
        with open("satelites_cazados.txt", "r") as f:
            for line in f:
                if line.strip():
                    capturas.append(line.strip())
                    if len(capturas) >= 50:
                        break
    except OSError:
        pass
    gc.collect()

    try:
        try:
            with open("satelites_cazados.txt", "r") as f:
                contenido_actual = f.read()
            resumen = "# RESUMEN_RX: capturados={} descartados={} total={}\n".format(
                paquetes_capturados[0], paquetes_descartados[0],
                paquetes_capturados[0] + paquetes_descartados[0])
            with open("satelites_cazados.txt", "w") as f:
                f.write(resumen + contenido_actual)
                f.flush()
                os.sync()
        except Exception:
            pass

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
            'errores': ''
        }
        estado_pendiente['errores'] = leer_errores_para_email('errores.log')
        gc.collect()
        with open("estado_pendiente.json", "w") as f:
            json.dump(estado_pendiente, f)
            f.flush()
            os.sync()
        gc.collect()
        log_info("EMAIL", "Estado pendiente guardado ({} HB, {} CAP) -> transicionando a fase4".format(
            hb_count, len(capturas)))
        if capturas:
            try:
                os.remove("satelites_cazados.txt")
            except Exception:
                pass
        guardar_fase(4)
        return True
    except Exception as e:
        log_exception("EMAIL_ESTADO", e)
        return False


# =========================================================================
# HEARTBEAT
# =========================================================================

def escribir_heartbeat_fase3(reloj_str, modo, radio, irq_delta, reinicios,
                              sat_nombre, temp_cpu, ventilador_on, fs_libre_kb, heartbeat_activo=True):
    escribir_heartbeat(
        "heartbeat.log", reloj_str, modo, radio.frecuencia, radio.sf,
        radio.bw, radio.cr, radio.sync_word, radio.crc_on, radio.rx_iq,
        radio.ganancia, gc.mem_free(), irq_delta, reinicios,
        sat_nombre, temp_cpu, ventilador_on, fs_libre_kb,
        heartbeat_activo=heartbeat_activo
    )


# =========================================================================
# NTP
# =========================================================================

def sincronizar_ntp_si_necesario(cfg):
    import time
    if time.localtime()[0] >= 2026:
        return
    log_warn("RTC", "RTC corrupto - transicionando a fase1 para sincronizar")
    guardar_fase(1)
    time.sleep_ms(500)
    from placa import reiniciar
    reiniciar()