# =========================================================================
# SUBMÓDULO: doppler_motor.py - MOTOR DE CÁLCULO DE PASES Y AGENDA
# =========================================================================

import json
import datos_satelites

from logger import log_debug
from config_system import obtener_config

__all__ = ["calcular_parametros_satelite", "auditar_agenda_consola"]

CONFIG = obtener_config()

DEBUG_MODO = CONFIG.get("debug_consola", True)

# _perfil_activo: constante interna; no modificar desde fuera del módulo
_perfil_activo = CONFIG["perfiles_satelites"][CONFIG["grupo_satelites_actual"]]

FREQ_BASE_HZ  = float(_perfil_activo["frecuencia_base_espera_hz"])
BW_BASE_KHZ   = float(_perfil_activo.get("ancho_banda_hz", CONFIG.get("ancho_banda_hz",   125000))) / 1000.0
SF_BASE       = int(_perfil_activo.get("lora_sf",          CONFIG.get("lora_sf",          11)))
CR_BASE       = int(_perfil_activo.get("lora_cr",          CONFIG.get("lora_cr",          8)))
SW_BASE       = int(_perfil_activo.get("lora_sync_word",   CONFIG.get("lora_sync_word",   52)))
PREAMBLE_BASE = int(_perfil_activo.get("lora_preamble_len",CONFIG.get("lora_preamble_len",8)))

# Valores de ganancia del receptor (registros del chip de radio)
_GANANCIA_ALTA = 0x96   # modo alta ganancia: subida/bajada del pase
# _GANANCIA_BAJA = 0x18   # modo baja ganancia: satélite en cénit (original)
_GANANCIA_BAJA = 0x96   # <-- Forzado al máximo siempre hasta recibir el primer satélite


def calcular_parametros_satelite(utc_actual_segundos):
    sat_objeto = datos_satelites.obtener_objeto_satelite(utc_actual_segundos)

    if sat_objeto is None:
        # Sin pase activo: devolver parámetros base en MHz (misma unidad que
        # la rama principal para que el llamador no tenga que distinguir casos)
        freq_base_mhz = FREQ_BASE_HZ / 1000000.0
        return {
            "sat_objeto":   None,
            "freq_obj":     freq_base_mhz,
            "sf_obj":       SF_BASE,
            "cr_obj":       CR_BASE,
            "bw_obj":       BW_BASE_KHZ,
            "sw_obj":       SW_BASE,
            "preamble_obj": PREAMBLE_BASE,
            "ganancia_obj": _GANANCIA_BAJA,
            "crc_on":       False,
            "rx_iq":        False,
        }

    frecuencia_nominal_hz = float(sat_objeto["lora"]["frecuencia_hz"])
    sf_obj       = int(  sat_objeto["lora"]["sf"])
    cr_obj       = int(  sat_objeto["lora"]["cr"])
    bw_obj       = float(sat_objeto["lora"]["bw_khz"])
    sw_obj       = int(  sat_objeto["lora"].get("sync_word",    SW_BASE))
    preamble_obj = int(  sat_objeto["lora"].get("preamble_len", PREAMBLE_BASE))

    segundos_inicio_pase    = int(sat_objeto["tiempo"]["utc_ini_timestamp"])
    duracion_total_segundos = int(sat_objeto["tiempo"]["duracion_min"]) * 60
    segundos_transcurridos  = utc_actual_segundos - segundos_inicio_pase
    tercio_segundos         = duracion_total_segundos // 3
    delta_hz                = int(sat_objeto["satelite"]["delta_doppler_hz"])

    if segundos_transcurridos < tercio_segundos:
        # Primer tercio: satélite aproximándose → frecuencia alta
        frecuencia_calculada_hz = frecuencia_nominal_hz + delta_hz
        ganancia_obj = _GANANCIA_ALTA
    elif segundos_transcurridos < (tercio_segundos * 2):
        # Segundo tercio: satélite en cénit → frecuencia nominal
        frecuencia_calculada_hz = frecuencia_nominal_hz
        ganancia_obj = _GANANCIA_BAJA
    else:
        # Tercer tercio: satélite alejándose → frecuencia baja
        frecuencia_calculada_hz = frecuencia_nominal_hz - delta_hz
        ganancia_obj = _GANANCIA_ALTA

    return {
        "sat_objeto":   sat_objeto,
        "freq_obj":     frecuencia_calculada_hz / 1000000.0,
        "sf_obj":       sf_obj,
        "cr_obj":       cr_obj,
        "bw_obj":       bw_obj,
        "sw_obj":       sw_obj,
        "preamble_obj": preamble_obj,
        "ganancia_obj": ganancia_obj,
    }


def auditar_agenda_consola(reloj_pantalla_str, utc_actual_segundos):
    if not DEBUG_MODO:
        return

    print("\n" + "=" * 88)
    print(" [HORA LOCAL -> {}] Escrutinio de agenda y configuracion de radio".format(reloj_pantalla_str))
    print("=" * 88)

    try:
        with open("agenda.json", "r") as archivo_print:
            pases_locales = json.load(archivo_print).get("pases", [])
    except FileNotFoundError:
        log_debug("AGENDA", "agenda.json no encontrada")
        return
    except json.JSONDecodeError as e:
        log_debug("AGENDA", "agenda.json mal formada: {}".format(e))
        return

    for p in pases_locales:
        ts_inicio = int(p["tiempo"]["utc_ini_timestamp"])
        ts_fin    = ts_inicio + int(p["tiempo"]["duracion_min"]) * 60

        if utc_actual_segundos > ts_fin:
            continue    # pase ya finalizado, ignorar

        ini    = p["tiempo"]["inicio"]
        fin    = p["tiempo"]["fin"]
        nom    = p["satelite"]["nombre"]
        sw_p   = p["lora"]["sync_word"]
        el_p   = p["satelite"]["max_elevacion"]

        v_status = "->[EN CIELO]" if (ts_inicio <= utc_actual_segundos <= ts_fin) else "[ESPERA]"
        print(" {:12} | Sat: {:15} | Ventana: [{} a {}] | SW: {:2} | MaxElev: {:2}°".format(
            v_status, nom, ini, fin, sw_p, el_p))