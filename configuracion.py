# =========================================================================
# MÓDULO: configuracion.py
# =========================================================================
import json

VERSION = "V8.0"
NOMBRE_PROYECTO = "LEO"

_CONFIG_FILE = "config.json"
CONFIG = None


def obtener_config():
    global CONFIG

    if CONFIG is None:
        try:
            with open(_CONFIG_FILE, "r") as f:
                datos = json.load(f)
        except OSError:
            raise RuntimeError(
                "No se encontro el fichero de configuracion: '{}'".format(_CONFIG_FILE)
            )
        except ValueError:
            raise RuntimeError(
                "El fichero '{}' no contiene JSON valido".format(_CONFIG_FILE)
            )

        if not isinstance(datos, dict):
            raise RuntimeError(
                "Se esperaba un objeto JSON (dict) en '{}'".format(_CONFIG_FILE)
            )

        CONFIG = datos

    return CONFIG


def version():
    # Devuelve la version actual del proyecto
    return VERSION


def nombre_proyecto():
    # Devuelve el nombre del proyecto
    return NOMBRE_PROYECTO


def firma_proyecto():
    # Devuelve la firma completa del proyecto (nombre + version)
    return "{} {}".format(NOMBRE_PROYECTO, VERSION)
