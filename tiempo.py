# =========================================================================
# MÓDULO: tiempo.py
# Gestión centralizada de tiempo UTC/local y correcciones horarias
# =========================================================================


import time
import machine
import os

_EPOCH_OFFSET = 946684800   # Diferencia entre epoch MicroPython (2000) y Unix (1970)


def obtener_unix_utc_real():
    """Devuelve timestamp Unix real (epoch 1970) compensando el offset de MicroPython."""
    return int(time.time()) + _EPOCH_OFFSET


def es_entorno_thonny():
    """Detecta si estamos ejecutando bajo Thonny (soft reset o presencia de directorio)."""
    try:
        return ("thonny" in os.listdir("/")) or (machine.reset_cause() == 5)
    except:
        return False


def corregir_hora_thonny():
    """Corrige la hora cuando el RTC está desfasado por reinicios de Thonny."""
    import datos_satelites
    try:
        t = time.localtime()
        unix_local = int(time.mktime(t)) + _EPOCH_OFFSET
        desfase = datos_satelites.obtener_desfase_espana(unix_local)
        return unix_local - desfase
    except:
        return obtener_unix_utc_real()


def obtener_tiempo_actual():
    """Devuelve (utc_unix, reloj_str, t_local) teniendo en cuenta corrección Thonny."""
    import gc
    import datos_satelites

    if es_entorno_thonny():
        utc_unix = corregir_hora_thonny()
    else:
        utc_unix = obtener_unix_utc_real()

    gc.collect()
    desfase = datos_satelites.obtener_desfase_espana(utc_unix)
    local_unix = utc_unix + desfase
    t_local = time.localtime(local_unix - _EPOCH_OFFSET)
    reloj_pantalla_str = "{:02d}:{:02d}:{:02d}".format(t_local[3], t_local[4], t_local[5])
    return utc_unix, reloj_pantalla_str, t_local


def parsear_timestamp(ts_str):
    """Convierte '2026-07-16T11:49:57' a timestamp Unix (epoch 1970).
    Devuelve None si el formato no es válido.
    """
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
        # time.mktime necesita tupla con epoch MicroPython (2000)
        # pero devuelve segundos desde 2000, así que sumamos offset
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
