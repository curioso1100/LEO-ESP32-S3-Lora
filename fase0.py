# =========================================================================
# fase0.py - Actualizacion remota de configuracion desde GitHub Gist
# =========================================================================
# Se ejecuta:
#   1. Al arranque (desde fase1.py), justo despues de WiFi up.
#   2. (futuro) Despues de cada email de estado en fase4.
#
# Si detecta cambio en config.json remoto:
#   - Descarga, valida, backup, escribe.
#   - Retorna True -> el llamador debe hacer reiniciar().
# Si no hay cambio o falla:
#   - Retorna False -> continuar normalmente.
# =========================================================================

import json
import os
import socket
import ssl
import gc

from logger import log_info, log_warn, log_error, log_debug, log_persistente

# -------------------------------------------------------------------------
# CONFIGURACION DE URLs (raw de GitHub Gist, SIN hash de commit)
# GitHub redirige 302 automaticamente a la ultima version.
# -------------------------------------------------------------------------
URL_UPDATE_FLAG = "https://gist.githubusercontent.com/curioso1100/748c744578208005d929a9746f301a5e/raw/update.flag"
URL_CONFIG_JSON = "https://gist.githubusercontent.com/curioso1100/748c744578208005d929a9746f301a5e/raw/config.json"

CONFIG_LOCAL = "config.json"
CONFIG_BACKUP = "config.json.bak"

# Claves criticas que debe tener un config.json valido
CLAVES_CRITICAS = [
    "wifi_ssid",
    "wifi_pass",
    "destinatario",
    "grupo_satelites_actual",
    "perfiles_satelites",
]

TIMEOUT_SEG = 10
MAX_REDIRECTS = 3
CHUNK_SIZE = 512


# -------------------------------------------------------------------------
# HTTP/HTTPS nativo (sin urequests)
# -------------------------------------------------------------------------

def _parsear_url(url):
    # Extrae host, port, path, is_https de una URL
    if url.startswith("https://"):
        port = 443
        rest = url[8:]
        is_https = True
    elif url.startswith("http://"):
        port = 80
        rest = url[7:]
        is_https = False
    else:
        return None, None, None, False

    idx = rest.find("/")
    if idx < 0:
        host = rest
        path = "/"
    else:
        host = rest[:idx]
        path = rest[idx:]

    return host, port, path, is_https


def _http_get_single(url, timeout=TIMEOUT_SEG):
    # Una sola peticion HTTP sin seguir redirects. Retorna (status_code, body_bytes, redirect_url o None)
    host, port, path, is_https = _parsear_url(url)
    if not host:
        log_warn("FASE0", "URL invalida: {}".format(url))
        return None, None, None

    raw_sock = None
    sock = None
    try:
        res = socket.getaddrinfo(host, port)
        addr = res[0][4]

        raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw_sock.settimeout(timeout)
        raw_sock.connect(addr)

        if is_https:
            try:
                sock = ssl.wrap_socket(raw_sock, server_hostname=host)
            except TypeError:
                sock = ssl.wrap_socket(raw_sock)
        else:
            sock = raw_sock

        req = "GET {} HTTP/1.1\r\nHost: {}\r\nConnection: close\r\nUser-Agent: ESP32-LEO/1.0\r\n\r\n".format(path, host)
        sock.write(req.encode())

        # Leer headers hasta \r\n\r\n
        headers = b""
        while b"\r\n\r\n" not in headers:
            chunk = sock.read(1)
            if not chunk:
                break
            headers += chunk

        # Extraer status
        status = None
        if headers:
            first_line = headers.split(b"\r\n")[0].decode("utf-8", "ignore")
            parts = first_line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                status = int(parts[1])

        # Extraer Location si hay redirect
        redirect_url = None
        if status in (301, 302, 307, 308):
            for line in headers.split(b"\r\n"):
                if line.lower().startswith(b"location:"):
                    redirect_url = line.split(b":", 1)[1].strip().decode("utf-8", "ignore")
                    break

        # Leer body
        body = b""
        while True:
            chunk = sock.read(CHUNK_SIZE)
            if not chunk:
                break
            body += chunk

        return status, body, redirect_url

    except Exception as e:
        log_warn("FASE0", "Error HTTP GET {}: {}".format(url, e))
        return None, None, None

    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass
        if raw_sock:
            try:
                raw_sock.close()
            except Exception:
                pass
        gc.collect()


def _http_get(url, timeout=TIMEOUT_SEG):
    # Petición HTTP con seguimiento de redirects y anti-cache
    import time
    # Añadir cache-bust para evitar que GitHub sirva version antigua
    sep = "&" if "?" in url else "?"
    cache_bust_url = url + sep + "t=" + str(int(time.time()))
    
    current_url = cache_bust_url
    for _ in range(MAX_REDIRECTS):
        status, body, redirect = _http_get_single(current_url, timeout)
        if status in (301, 302, 307, 308) and redirect:
            log_debug("FASE0", "Redirect {} -> {}".format(status, redirect))
            current_url = redirect
            continue
        return status, body
    log_warn("FASE0", "Demasiados redirects")
    return None, None


# -------------------------------------------------------------------------
# Validacion y persistencia
# -------------------------------------------------------------------------

def _validar_config(cfg_dict):
    # Valida que el dict tenga estructura minima viable
    for clave in CLAVES_CRITICAS:
        if clave not in cfg_dict:
            log_warn("FASE0", "Validacion fallida: falta clave '{}'".format(clave))
            return False

    # Al menos 1 satelite en el perfil activo
    try:
        grupo = cfg_dict.get("grupo_satelites_actual", "")
        perfiles = cfg_dict.get("perfiles_satelites", {})
        satelites = perfiles.get(grupo, {}).get("satelites", {})
        if not satelites or len(satelites) < 1:
            log_warn("FASE0", "Validacion fallida: no hay satelites en perfil '{}'".format(grupo))
            return False
    except Exception as e:
        log_warn("FASE0", "Validacion fallida en satelites: {}".format(e))
        return False

    # Frecuencias en rango UHF razonable (430-440 MHz)
    try:
        for nombre, sat in satelites.items():
            frec = sat.get("frec", 0)
            if not (430000000 <= frec <= 440000000):
                log_warn("FASE0", "Validacion fallida: frecuencia invalida para {}: {} Hz".format(nombre, frec))
                return False
    except Exception as e:
        log_warn("FASE0", "Validacion fallida en frecuencias: {}".format(e))
        return False

    return True


def _backup_config():
    # Copia config.json a config.json.bak
    try:
        with open(CONFIG_LOCAL, "rb") as f:
            data = f.read()
        with open(CONFIG_BACKUP, "wb") as f:
            f.write(data)
        log_debug("FASE0", "Backup creado: {}".format(CONFIG_BACKUP))
        return True
    except Exception as e:
        log_warn("FASE0", "No se pudo crear backup: {}".format(e))
        return False


def _configs_iguales(data_nueva_bytes):
    # Compara byte a byte con config.json local
    try:
        with open(CONFIG_LOCAL, "rb") as f:
            local = f.read()
        return local == data_nueva_bytes
    except OSError:
        return False


# -------------------------------------------------------------------------
# Punto de entrada principal
# -------------------------------------------------------------------------

def ejecutar():
    # Retorna True si se aplico un cambio y se requiere reinicio. Retorna False si no hay cambio, fallo, o validacion rechazada

    log_persistente("FASE0", "Iniciando check de actualizacion remota", "INFO")

    # 1. Descargar update.flag
    status, body = _http_get(URL_UPDATE_FLAG)
    if status != 200 or not body:
        log_debug("FASE0", "No hay flag disponible (status={})".format(status))
        return False

    flag_content = body.decode("utf-8", "ignore").strip()
    log_debug("FASE0", "Flag contenido: '{}'".format(flag_content))

    if not flag_content:
        log_persistente("FASE0", "Flag vacio. Sin actualizaciones", "INFO")
        return False

    # Procesar cada linea del flag
    ficheros_a_actualizar = [l.strip() for l in flag_content.split("\n") if l.strip()]
    if not ficheros_a_actualizar:
        log_persistente("FASE0", "Flag sin contenido util", "INFO")
        return False

    cambios_aplicados = False

    for fichero in ficheros_a_actualizar:
        log_persistente("FASE0", "Procesando actualizacion para: {}".format(fichero), "INFO")

        # Extensiones soportadas: .json (ahora), .py (preparado para futuro)
        if fichero.endswith(".py"):
            log_warn("FASE0", "Fichero .py detectado pero no soportado aun: {}".format(fichero))
            continue

        if not fichero.endswith(".json"):
            log_warn("FASE0", "Extension desconocida: {}".format(fichero))
            continue

        # Mapear fichero a URL
        if fichero == "config.json":
            url = URL_CONFIG_JSON
        else:
            log_warn("FASE0", "Fichero desconocido en flag: {}".format(fichero))
            continue

        # Descargar
        status, data_bytes = _http_get(url)
        if status != 200 or not data_bytes:
            log_warn("FASE0", "Fallo descarga de {} (status={})".format(fichero, status))
            continue

        log_debug("FASE0", "{} descargado: {} bytes".format(fichero, len(data_bytes)))

        # Comparar con local (byte a byte)
        if _configs_iguales(data_bytes):
            log_persistente("FASE0", "{} remoto es identico al local. Sin cambios.".format(fichero), "INFO")
            continue

        # Validar JSON
        try:
            cfg = json.loads(data_bytes.decode("utf-8"))
        except Exception as e:
            log_warn("FASE0", "JSON invalido en {}: {}".format(fichero, e))
            continue

        if not _validar_config(cfg):
            log_warn("FASE0", "Validacion rechazo el nuevo {}".format(fichero))
            continue

        # Backup
        if not _backup_config():
            log_warn("FASE0", "No se pudo hacer backup. Abortando actualizacion.")
            continue

        # Escribir nuevo config
        try:
            with open(CONFIG_LOCAL, "wb") as f:
                f.write(data_bytes)
                f.flush()
                os.sync()

            # Invalidar cache de config_system.py para que la proxima
            # llamada a obtener_config() lea el fichero fresco
            import config_system
            config_system._CONFIG_CACHE = None

            log_info("FASE0", "{} actualizado correctamente. Backup en {}".format(fichero, CONFIG_BACKUP))
            log_persistente("FASE0", "Update remoto aplicado: {}".format(fichero), "INFO")
            cambios_aplicados = True

        except Exception as e:
            log_error("FASE0", "Error escribiendo {}: {}".format(fichero, e))
            log_persistente("FASE0", "Error escribiendo {}: {}".format(fichero, e), "ERROR")
            continue

    if cambios_aplicados:
        log_persistente("FASE0", "Cambios aplicados. Se requiere reinicio", "INFO")
        return True
    else:
        log_persistente("FASE0", "Sin cambios aplicados", "INFO")
        return False


# -------------------------------------------------------------------------
# Modo test: ejecutar desde consola Thonny sin escribir nada
# -------------------------------------------------------------------------

def test():
    # Solo descarga y muestra por print, sin tocar el filesystem
    print("=" * 60)
    print("FASE0 - MODO TEST (solo lectura, sin escritura)")
    print("=" * 60)

    print("\n[TEST] Descargando update.flag...")
    status, body = _http_get(URL_UPDATE_FLAG)
    print("Status:", status)
    if body:
        print("Contenido:", repr(body.decode("utf-8", "ignore").strip()))
    else:
        print("Sin body")

    print("\n[TEST] Descargando config.json...")
    status, body = _http_get(URL_CONFIG_JSON)
    print("Status:", status)
    if body:
        print("Size:", len(body), "bytes")
        try:
            cfg = json.loads(body.decode("utf-8"))
            print("JSON valido: SI")
            grupo = cfg.get("grupo_satelites_actual", "?")
            sats = list(cfg.get("perfiles_satelites", {}).get(grupo, {}).get("satelites", {}).keys())
            print("Satelites en perfil '{}': {}".format(grupo, sats))
            print("Validacion estructural:", "OK" if _validar_config(cfg) else "FALLIDA")
        except Exception as e:
            print("JSON valido: NO ->", e)
    else:
        print("Sin body")

    print("\n[TEST] Comparacion con local:")
    if body:
        iguales = _configs_iguales(body)
        print("Remoto == Local:", iguales)

    print("=" * 60)
    print("TEST finalizado. No se ha escrito nada en el filesystem.")
    print("=" * 60)