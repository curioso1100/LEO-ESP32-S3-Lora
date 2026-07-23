# =========================================================================
# MÓDULO: config_system.py
# =========================================================================
# FUSIÓN A.2: configuracion.py + estado.py
# Fecha refactor: 2026-07-23
# =========================================================================
#
# PRINCIPIO: Este módulo NO importa logger.py.
# Errores de I/O se silencian (valores por defecto), no se propagan.
# El llamador puede loguear si lo considera necesario.
# =========================================================================

import json
import os


# -------------------------------------------------------------------------
# CONSTANTES (de configuracion.py)
# -------------------------------------------------------------------------

VERSION = "V8.0"
NOMBRE_PROYECTO = "LEO"

_CONFIG_FILE = "config.json"
_CONFIG_CACHE = None


# -------------------------------------------------------------------------
# CONFIGURACIÓN
# -------------------------------------------------------------------------

def obtener_config():
    """Lee config.json. Si falla, devuelve diccionario vacío."""
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE

    try:
        with open(_CONFIG_FILE, "r") as f:
            _CONFIG_CACHE = json.load(f)
        return _CONFIG_CACHE
    except (OSError, ValueError):
        return {}


def version():
    return VERSION


def nombre_proyecto():
    return NOMBRE_PROYECTO


def firma_proyecto():
    return "{} {}".format(NOMBRE_PROYECTO, VERSION)


# -------------------------------------------------------------------------
# ESTADO (fase)
# -------------------------------------------------------------------------

_ESTADO_FILE = "estado.json"


def guardar_fase(fase):
    """Guarda la fase en estado.json. Si falla, devuelve False."""
    try:
        with open(_ESTADO_FILE, "w") as f:
            json.dump({"fase": int(fase)}, f)
            f.flush()
            os.sync()
        return True
    except (OSError, ValueError):
        return False


def leer_fase():
    """Lee la fase desde estado.json. Si falla, devuelve 1 (fase1 por defecto)."""
    try:
        with open(_ESTADO_FILE, "r") as f:
            estado = json.load(f)
            return int(estado.get("fase", 1))
    except (OSError, ValueError):
        return 1


def borrar_estado():
    """Elimina estado.json. Si falla, silencia el error."""
    try:
        os.remove(_ESTADO_FILE)
    except OSError:
        pass