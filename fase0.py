# =========================================================================
# fase0.py - Actualizacion remota de configuracion desde GitHub Gist
# =========================================================================

import json
import os
import socket
import ssl
import gc
import time

from logger import log_warn, log_error, log_debug, log_persistente

URL_UPDATE_FLAG = "https://gist.githubusercontent.com/curioso1100/748c744578208005d929a9746f301a5e/raw/update.flag"
URL_CONFIG_JSON = "https://gist.githubusercontent.com/curioso1100/748c744578208005d929a9746f301a5e/raw/config.json"

CONFIG_LOCAL = "config.json"
CONFIG_BACKUP = "config.json.bak"

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


def _parsear_url(url):
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
        headers = b""
        while b"\r\n\r\n" not in headers:
            chunk = sock.read(1)
            if not chunk:
                break
            headers += chunk
        status = None
        if headers:
            first_line = headers.split(b"\r\n")[0].decode("utf-8", "ignore")
            parts = first_line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                status = int(parts[1])
        redirect_url = None
        if status in (301, 302, 307, 308):
            for line in headers.split(b"\r\n"):
                if line.lower().startswith(b"location:"):
                    redirect_url = line.split(b":", 1)[1].strip().decode("utf-8", "ignore")
                    break
        body = b""
        while True:
            chunk = sock.read(CHUNK_SIZE)
            if not chunk:
                break
            body += chunk
        return status, body, redirect_url
    except Exception as e:
        log_persistente("FASE0", "Error HTTP GET {}: {}".format(url, e), "WARN")
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
    log_persistente("FASE0", "Demasiados redirects", "WARN")
    return None, None


def _validar_config(cfg_dict):
    for clave in CLAVES_CRITICAS:
        if clave not in cfg_dict:
            log_warn("FASE0", "Validacion fallida: falta clave '{}'".format(clave))
            return False
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


def _configs_iguales_normalizados(cfg_remoto_dict):
    try:
        with open(CONFIG_LOCAL, "r") as f:
            cfg_local = json.load(f)
    except (OSError, ValueError):
        return False
    claves_ignorar = {"email_estado_horas_fijas"}
    def _normalizar(d):
        copia = dict(d)
        for k in claves_ignorar:
            copia.pop(k, None)
        return copia
    return _normalizar(cfg_remoto_dict) == _normalizar(cfg_local)


def ejecutar():
    log_persistente("FASE0", "Iniciando check de actualizacion remota", "INFO")

    status, body = _http_get(URL_UPDATE_FLAG)
    if status != 200 or not body:
        log_persistente("FASE0", "No hay flag disponible (status={})".format(status), "INFO")
        return False

    flag_content = body.decode("utf-8", "ignore").strip()
    log_persistente("FASE0", "Flag contenido: '{}'".format(flag_content), "INFO")

    if not flag_content:
        log_persistente("FASE0", "Flag vacio. Sin actualizaciones.", "INFO")
        return False

    ficheros_a_actualizar = [l.strip() for l in flag_content.split("\n") if l.strip()]
    if not ficheros_a_actualizar:
        log_persistente("FASE0", "Flag sin contenido util", "INFO")
        return False

    cambios_aplicados = False

    for fichero in ficheros_a_actualizar:
        log_persistente("FASE0", "Procesando actualizacion para: {}".format(fichero), "INFO")

        if fichero.endswith(".py"):
            log_warn("FASE0", "Fichero .py detectado pero no soportado aun: {}".format(fichero))
            continue
        if not fichero.endswith(".json"):
            log_warn("FASE0", "Extension desconocida: {}".format(fichero))
            continue

        if fichero == "config.json":
            url = URL_CONFIG_JSON
        else:
            log_warn("FASE0", "Fichero desconocido en flag: {}".format(fichero))
            continue

        status, data_bytes = _http_get(url)
        if status != 200 or not data_bytes:
            log_persistente("FASE0", "Fallo descarga de {} (status={})".format(fichero, status), "WARN")
            continue

        log_persistente("FASE0", "{} descargado: {} bytes".format(fichero, len(data_bytes)), "INFO")

        try:
            cfg = json.loads(data_bytes.decode("utf-8"))
        except Exception as e:
            log_persistente("FASE0", "JSON invalido en {}: {}".format(fichero, e), "WARN")
            continue

        if _configs_iguales_normalizados(cfg):
            log_persistente("FASE0", "{} remoto es identico al local (ignorando claves volatiles). Sin cambios.".format(fichero), "INFO")
            continue
        if not _validar_config(cfg):
            log_persistente("FASE0", "Validacion rechazo el nuevo {}".format(fichero), "WARN")
            continue

        # Escritura ATOMICA: bytes tal cual del Gist (preserva formato original)
        # NOTA: fase1/actualizar_linea_config reescribira plano al guardar horas.
        #       El Gist es la copia legible; la placa funciona con JSON plano.
        temp_file = CONFIG_LOCAL + ".new"
        try:
            with open(temp_file, "wb") as f:
                f.write(data_bytes)
                f.flush()
                os.sync()

            time.sleep_ms(200)
            with open(temp_file, "rb") as f:
                leido = f.read()
            if leido != data_bytes:
                log_warn("FASE0", "Verificacion fallida: tamano esperado={}, leido={}".format(len(data_bytes), len(leido)))
                try:
                    os.remove(temp_file)
                except OSError:
                    pass
                continue
            try:
                cfg_verif = json.loads(leido.decode("utf-8"))
                if "wifi_ssid" not in cfg_verif:
                    raise ValueError("Falta wifi_ssid")
            except Exception as e:
                log_warn("FASE0", "JSON post-escritura invalido: {}".format(e))
                try:
                    os.remove(temp_file)
                except OSError:
                    pass
                continue

            if CONFIG_LOCAL in os.listdir():
                try:
                    os.rename(CONFIG_LOCAL, CONFIG_BACKUP)
                    log_persistente("FASE0", "Backup creado: {}".format(CONFIG_BACKUP), "INFO")
                except Exception as e:
                    log_warn("FASE0", "No se pudo crear backup: {}".format(e))

            os.rename(temp_file, CONFIG_LOCAL)
            os.sync()

            if temp_file in os.listdir():
                try:
                    os.remove(temp_file)
                except OSError:
                    pass

            import config_system
            config_system._CONFIG_CACHE = None

            log_persistente("FASE0", "{} actualizado. Reinicio requerido.".format(fichero), "INFO")
            cambios_aplicados = True

        except Exception as e:
            log_error("FASE0", "Error escribiendo {}: {}".format(fichero, e))
            log_persistente("FASE0", "Error escribiendo {}: {}".format(fichero, e), "ERROR")
            try:
                if temp_file in os.listdir():
                    os.remove(temp_file)
            except OSError:
                pass
            continue

    if cambios_aplicados:
        log_persistente("FASE0", "Cambios aplicados. Se requiere reinicio.", "INFO")
        return True
    else:
        log_persistente("FASE0", "Sin cambios aplicados.", "INFO")
        return False


def test():
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
        try:
            cfg_test = json.loads(body.decode("utf-8"))
            iguales = _configs_iguales_normalizados(cfg_test)
            print("Remoto == Local:", iguales)
        except Exception as e:
            print("Error comparando:", e)
    print("=" * 60)
    print("TEST finalizado. No se ha escrito nada en el filesystem.")
    print("=" * 60)
