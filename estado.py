# =========================================================================
# MÓDULO: estado.py  -  Gestión centralizada de estado.json
# =========================================================================
import json
import errno
from logger import log_error

RUTA_ESTADO = "estado.json"
_MODULO = "estado"


def guardar_fase(fase):
    """Serializa ``fase`` en estado.json. Devuelve True si tiene éxito."""
    try:
        with open(RUTA_ESTADO, "w") as f:
            json.dump({"fase": fase}, f)
        return True
    except OSError as e:
        log_error(_MODULO, "No se pudo escribir {}: {}".format(RUTA_ESTADO, e))
        return False


def leer_fase():
    """Lee y devuelve la fase almacenada en estado.json (por defecto 1)."""
    try:
        with open(RUTA_ESTADO, "r") as f:
            datos = json.load(f)
    except OSError as e:
        if e.args[0] == errno.ENOENT:
            return 1  # Fichero no existe todavía, situación normal
        log_error(_MODULO, "No se pudo leer {}: {}".format(RUTA_ESTADO, e))
        return 1
    except ValueError as e:
        # json.JSONDecodeError no existe en MicroPython, usa ValueError
        log_error(_MODULO, "JSON inválido en {}: {}".format(RUTA_ESTADO, e))
        return 1

    try:
        return int(datos.get("fase", 1))
    except (ValueError, TypeError) as e:
        log_error(_MODULO, "Valor de fase inválido en {}: {}".format(RUTA_ESTADO, e))
        return 1


def borrar_estado():
    """Elimina estado.json si existe; no hace nada en caso contrario."""
    import os
    try:
        os.remove(RUTA_ESTADO)
    except OSError as e:
        if e.args[0] != errno.ENOENT:
            log_error(_MODULO, "No se pudo eliminar {}: {}".format(RUTA_ESTADO, e))
