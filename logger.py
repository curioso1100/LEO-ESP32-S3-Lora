# =========================================================================
# MÓDULO: logger.py
# =========================================================================

import os
import json

from config_system import obtener_config
from tiempo import obtener_tiempo_actual


def _debug_activo():
    try:
        return obtener_config().get("debug_consola", True)
    except:
        return True


def _max_log_lineas():
    try:
        return int(obtener_config().get("max_log_lineas", 50))
    except:
        return 50


def _rx_diag_activo():
    try:
        return obtener_config().get("activar_rx_diag", True)
    except:
        return True


def _max_errores_email_chars():
    try:
        return int(obtener_config().get("max_errores_email_chars", 2000))
    except:
        return 2000


def _timestamp_iso():
    try:
        utc_unix, reloj_str, t_local = obtener_tiempo_actual()
        return "{:04d}-{:02d}-{:02d}T{}".format(
            t_local[0], t_local[1], t_local[2], reloj_str)
    except Exception:
        return "????-??-??T??:??:??"


def log_info(modulo, mensaje):
    if _debug_activo():
        print("[INFO][{}] {}".format(modulo, mensaje))


def log_debug(modulo, mensaje):
    if _debug_activo():
        print("[DEBUG][{}] {}".format(modulo, mensaje))


def log_warn(modulo, mensaje):
    print("[WARN][{}] {}".format(modulo, mensaje))


def log_error(modulo, mensaje):
    print("[ERROR][{}] {}".format(modulo, mensaje))


def log_exception(modulo, exc):
    print("[ERROR][{}] {}".format(modulo, str(exc)))


def _contar_lineas_fichero(fichero):
    try:
        with open(fichero, "r") as f:
            count = 0
            for _ in f:
                count += 1
            return count
    except OSError:
        return 0


def _rotar_si_necesario(fichero, max_lineas):
    try:
        lineas = _contar_lineas_fichero(fichero)
        if lineas >= max_lineas:
            import uos
            try:
                uos.remove(fichero + ".old")
            except OSError:
                pass
            uos.rename(fichero, fichero + ".old")
            return True
    except Exception:
        pass
    return False


def log_persistente(modulo, mensaje, nivel="ERROR", fichero="errores.log"):
    ts = _timestamp_iso()
    linea = "{}|{}|{}|{}\n".format(ts, modulo, nivel, mensaje)
    print(linea, end="")

    try:
        max_lineas = _max_log_lineas()
        _rotar_si_necesario(fichero, max_lineas)

        with open(fichero, "a") as f:
            f.write(linea)
            f.flush()
            os.sync()
    except Exception:
        pass


def log_rx_diag(irq, estado, longitud, datos=None):
    if not _rx_diag_activo():
        return

    msg = "IRQ=0x{:04X} ESTADO={} LEN={}".format(irq, estado, longitud)
    if datos is not None:
        msg += " DATA={}".format(datos[:32] if len(datos) > 32 else datos)
    log_persistente("RX", msg, nivel="INFO")


def rotar_logs_txt(max_kb, max_lineas, fichero="logs.txt"):
    try:
        try:
            size = os.stat(fichero)[6]
        except OSError:
            return

        size_kb = size / 1024
        if size_kb < max_kb:
            return

        with open(fichero, "r") as f:
            lineas = f.readlines()

        if len(lineas) > max_lineas:
            lineas = lineas[-max_lineas:]

        with open(fichero, "w") as f:
            for linea in lineas:
                f.write(linea)
            f.flush()
            os.sync()

        log_info("LOGS", "Rotado {} ({}KB -> {} lineas)".format(
            fichero, int(size_kb), len(lineas)))
    except Exception as e:
        log_warn("LOGS", "Error rotando {}: {}".format(fichero, e))


def leer_archivo_texto(path, max_lineas=500):
    try:
        with open(path, "r") as f:
            lineas = f.readlines()
        if len(lineas) > max_lineas:
            lineas = lineas[-max_lineas:]
        return "".join(lineas)
    except OSError:
        return "({} no disponible)".format(path)
    except Exception as e:
        return "(Error leyendo {}: {})".format(path, e)


def leer_errores_para_email(fichero="errores.log", max_chars=None):
    if max_chars is None:
        max_chars = _max_errores_email_chars()

    try:
        tam = os.stat(fichero)[6]
        if tam == 0:
            return ''

        with open(fichero, 'r') as f:
            if tam > max_chars + 500:
                f.seek(tam - max_chars - 500)
            datos = f.read()

        lineas = datos.split('\n')
        filtradas = []

        for ln in lineas:
            ln_stripped = ln.strip()
            if not ln_stripped:
                continue

            incluir = False

            if 'EXCEPTION' in ln_stripped:
                incluir = True
            elif 'ERROR' in ln_stripped and 'RX INFO' not in ln_stripped:
                incluir = True
            elif 'WARN' in ln_stripped and 'RX INFO' not in ln_stripped:
                incluir = True
            elif 'INFO' in ln_stripped and any(k in ln_stripped for k in [
                'FASE', 'PASE', 'AGENDA', 'EMAIL', 'NTP', 'REINICIAR',
                'THRESHOLD', 'VENT', 'MEM', 'CAPTURA', 'RADIO'
            ]):
                incluir = True
            elif 'RX INFO' in ln_stripped and 'DATA=' not in ln_stripped:
                incluir = True

            if incluir:
                filtradas.append(ln)

        resultado = '\n'.join(filtradas)

        if len(resultado) > max_chars:
            resultado = resultado[-max_chars:]
            nl_idx = resultado.find('\n')
            if nl_idx >= 0:
                resultado = resultado[nl_idx + 1:]

        return resultado

    except OSError:
        return ''
    except Exception as e:
        return "(Error leyendo {}: {})".format(fichero, e)


def escribir_captura(path, sat_nombre, marca_tiempo, frecuencia_actual,
                     sf_actual, bw_actual, cr_actual, sw_actual,
                     rx_iq, crc_on, implicit_header, pay_len,
                     paquete_texto, modo, rssi=None, snr=None):
    fh = None
    try:
        # Construir linea base
        linea = "SAT={}|HORA={}|RSSI={}|SNR={}|FREC={:.3f}MHz|SF={}|BW={}|CR={}|SW={}|RXIQ={}|CRC={}|IMPL={}|PLEN={}|MODO={}".format(
            sat_nombre, marca_tiempo,
            "{:.1f}".format(rssi) if rssi is not None else "N/A",
            "{:.2f}".format(snr) if snr is not None else "N/A",
            frecuencia_actual, sf_actual, bw_actual, cr_actual, sw_actual,
            rx_iq, crc_on, implicit_header, pay_len, modo)

        # Añadir DATA
        linea += "|DATA={}\n".format(paquete_texto)

        fh = open(path, "a")
        fh.write(linea)
        fh.flush()
        os.sync()
    except Exception as e:
        log_persistente("CAPTURA", "Fallo al escribir en {}: {}".format(path, e), "ERROR")
    finally:
        if fh:
            fh.close()


def leer_todas_las_capturas(fichero="satelites_cazados.txt"):
    try:
        with open(fichero, "r") as h:
            lineas = h.readlines()
        capturas = [l.strip() for l in lineas if l.strip()]
        if capturas:
            log_info("CAPTURAS", "Leidas {} capturas de {}".format(len(capturas), fichero))
        return capturas
    except OSError:
        log_warn("CAPTURAS", "No se encontro {}".format(fichero))
        return []
    except Exception as e:
        log_warn("CAPTURAS", "Error leyendo {}: {}".format(fichero, e))
        return []


def borrar_capturas(fichero="satelites_cazados.txt"):
    try:
        os.remove(fichero)
        log_info("CAPTURAS", "{} eliminado correctamente".format(fichero))
        return True
    except OSError:
        return True
    except Exception as e:
        log_warn("CAPTURAS", "No se pudo eliminar {}: {}".format(fichero, e))
        return False


def hay_capturas_pendientes(fichero="satelites_cazados.txt"):
    try:
        with open(fichero, "r") as f:
            return len(f.readlines()) > 0
    except OSError:
        return False


def leer_logs_pendientes(fichero="logs.txt"):
    try:
        with open(fichero, "r") as f:
            return f.readlines()
    except OSError:
        return []
    except Exception as e:
        log_warn("LOG", "Error leyendo {}: {}".format(fichero, e))
        return []


def guardar_logs(logs, fichero="logs.txt"):
    if not logs:
        return True
    try:
        with open(fichero, "w") as f:
            for linea in logs:
                f.write(linea)
            f.flush()
            os.sync()
        return True
    except Exception as e:
        log_error("LOG", "Error escribiendo {}: {}".format(fichero, e))
        return False


def guardar_para_reintento(resumen, capturas, fichero="logs.txt"):
    try:
        with open(fichero, "a") as f:
            if resumen:
                f.write("=== ESTADO FALLIDO ===\n")
                f.write(resumen)
                f.write("\n")
            for cap in capturas:
                f.write(cap + "\n")
            f.flush()
            os.sync()
        log_info("EMAIL", "Datos guardados en {} para reintento".format(fichero))
    except Exception as e:
        log_error("EMAIL", "No se pudieron guardar datos para reintento: {}".format(e))


def leer_estado_pendiente(fichero="estado_pendiente.json"):
    try:
        with open(fichero, "r") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def borrar_estado_pendiente(fichero="estado_pendiente.json"):
    try:
        os.remove(fichero)
        log_info("ESTADO", "{} eliminado".format(fichero))
    except OSError:
        pass


def escribir_heartbeat(path, reloj_str, modo, frecuencia_actual, sf_actual,
                       bw_actual, cr_actual, sw_actual, crc_on, rx_iq,
                       ganancia_actual, ram_libre, irq_count_total, reinicios,
                       sat_nombre=" ", temp_cpu=None, ventilador_on=False,
                       fs_libre_kb=None, heartbeat_activo=True):
    if not heartbeat_activo:
        return None

    temp_str = "{:.1f}".format(temp_cpu) if temp_cpu is not None else "N/A"
    vent_str = "ON" if ventilador_on else "OFF"
    fs_str = "{:.0f}".format(fs_libre_kb) if fs_libre_kb is not None else "N/A"
    rst_str = "" if reinicios == 0 else " RST={}".format(reinicios)

    sat_field = "{:<15}".format(sat_nombre[:15])

    linea = "HB {} {} {} {:.3f} SF{} BW{} CR{} SW{} C{} I{} RAM={} IRQ={}{} T={} V={} FS={}\n".format(
        reloj_str, modo, sat_field, frecuencia_actual, sf_actual, bw_actual, cr_actual, sw_actual,
        "1" if crc_on else "0", "1" if rx_iq else "0", ram_libre,
        irq_count_total, rst_str, temp_str, vent_str, fs_str)

    fh = None
    try:
        fh = open(path, "a")
        fh.write(linea)
        fh.flush()
        os.sync()
    except Exception as e:
        log_warn("HEARTBEAT", "No se pudo escribir heartbeat: {}".format(e))
    finally:
        if fh:
            fh.close()

    return linea.strip()