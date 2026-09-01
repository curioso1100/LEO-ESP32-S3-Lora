# =========================================================================
# fase0.py - Actualizacion remota de config.json y archivos .py desde Gist
# =========================================================================

import json
import os
import socket
import ssl
import gc
import time

from logger import log_warn, log_error, log_debug, log_persistente

URL_CONFIG_JSON = "https://gist.githubusercontent.com/curioso1100/748c744578208005d929a9746f301a5e/raw/config.json"

CONFIG_LOCAL = "config.json"
CONFIG_BACKUP = "config.json.bak"

ARCHIVOS_PROTEGIDOS = {"fase0.py", "main.py", "boot.py"}

CLAVES_CRITICAS = [
    "wifi_ssid",
    "wifi_pass",
    "destinatario",
    "grupo_satelites_actual",
    "perfiles_satelites",
]

TIMEOUT_SEG = 20
MAX_REDIRECTS = 3
CHUNK_SIZE = 256          # Reducido para no reservar buffers grandes
DELAY_ENTRE_DESCARGAS_MS = 3000   # 3 s entre descargas para no saturar GitHub


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


# =========================================================================
# HTTP A RAM (solo para config.json, ~7 KB)
# =========================================================================

def _http_get_single(url, timeout=TIMEOUT_SEG):
    host, port, path, is_https = _parsear_url(url)
    if not host:
        log_warn("FASE0", "URL invalida: {}".format(url))
        return None, None, None
    raw_sock = None
    sock = None
    try:
        res = socket.getaddrinfo(host, port)
        if not res:
            log_persistente("FASE0", "DNS fallo para {}:{}".format(host, port), "WARN")
            return None, None, None
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


# =========================================================================
# HTTP A DISCO (streaming, sin cargar en RAM)
# =========================================================================

def _http_download_single(url, filepath, timeout=TIMEOUT_SEG):
    """Descarga body directamente a filepath. Retorna (status, bytes_written, redirect_url)."""
    host, port, path, is_https = _parsear_url(url)
    if not host:
        log_warn("FASE0", "URL invalida: {}".format(url))
        return None, 0, None

    raw_sock = None
    sock = None
    bytes_written = 0

    try:
        res = socket.getaddrinfo(host, port)
        if not res:
            log_persistente("FASE0", "DNS fallo para {}:{}".format(host, port), "WARN")
            return None, 0, None
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

        # Leer headers byte a byte (pocos bytes, seguro en RAM)
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
            return status, 0, redirect_url

        # Escribir body directo a disco, chunk a chunk
        with open(filepath, "wb") as f:
            while True:
                chunk = sock.read(CHUNK_SIZE)
                if not chunk:
                    break
                f.write(chunk)
                bytes_written += len(chunk)

        return status, bytes_written, None

    except Exception as e:
        log_persistente("FASE0", "Error HTTP download {}: {}".format(url, e), "WARN")
        return None, 0, None
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


def _http_download(url, filepath, timeout=TIMEOUT_SEG):
    """Maneja redirects y descarga a filepath. Retorna (status, bytes_written)."""
    sep = "&" if "?" in url else "?"
    cache_bust_url = url + sep + "t=" + str(int(time.time()))
    current_url = cache_bust_url
    for _ in range(MAX_REDIRECTS):
        gc.collect()
        status, bytes_written, redirect = _http_download_single(current_url, filepath, timeout)
        if status in (301, 302, 307, 308) and redirect:
            log_debug("FASE0", "Redirect {} -> {}".format(status, redirect))
            time.sleep(1)   # Pausa tras redirect para no saturar GitHub
            # El redirect puede ya tener query params
            if "?" in redirect:
                current_url = redirect + "&t=" + str(int(time.time()))
            else:
                current_url = redirect + "?t=" + str(int(time.time()))
            continue
        return status, bytes_written
    log_persistente("FASE0", "Demasiados redirects", "WARN")
    return None, 0


# =========================================================================
# VALIDACIONES
# =========================================================================

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
        if not isinstance(d, dict):
            return None
        copia = dict(d)
        for k in claves_ignorar:
            copia.pop(k, None)
        return copia
    norm_remoto = _normalizar(cfg_remoto_dict)
    norm_local = _normalizar(cfg_local)
    if norm_remoto is None or norm_local is None:
        return False
    return norm_remoto == norm_local


# =========================================================================
# ACTUALIZACION REMOTA DE ARCHIVOS .py
# =========================================================================

def _extraer_nombre_archivo(url):
    url_limpia = url.split("?")[0]
    url_limpia = url_limpia.rstrip("/")
    idx = url_limpia.rfind("/")
    if idx >= 0:
        nombre = url_limpia[idx + 1:]
    else:
        nombre = url_limpia
    if not nombre.endswith(".py"):
        return None
    return nombre


def _normalizar_url_gist(url):
    parts = url.split('/')
    for i, part in enumerate(parts):
        if part == 'raw' and i + 1 < len(parts):
            candidate = parts[i + 1]
            if len(candidate) == 40:
                is_hex = True
                for c in candidate:
                    if c not in '0123456789abcdefABCDEF':
                        is_hex = False
                        break
                if is_hex:
                    new_parts = parts[:i + 1] + parts[i + 2:]
                    url_corregida = '/'.join(new_parts)
                    log_warn("FASE0", "URL con hash de commit detectada. Corregida: {} -> {}".format(url, url_corregida))
                    return url_corregida
    return url


def _archivos_iguales_por_chunks(path_a, path_b):
    """Compara dos archivos chunk a chunk, sin cargarlos enteros en RAM."""
    try:
        with open(path_a, "rb") as fa, open(path_b, "rb") as fb:
            while True:
                ca = fa.read(CHUNK_SIZE)
                cb = fb.read(CHUNK_SIZE)
                if ca != cb:
                    return False
                if not ca:
                    break
        return True
    except OSError:
        return False


def _actualizar_archivo_py(url):
    nombre = _extraer_nombre_archivo(url)
    if not nombre:
        log_warn("FASE0", "URL sin nombre de archivo .py valido: {}".format(url))
        return False, None

    if nombre in ARCHIVOS_PROTEGIDOS:
        log_warn("FASE0", "Archivo protegido, omitido: {}".format(nombre))
        return False, nombre

    url = _normalizar_url_gist(url)
    log_debug("FASE0", "Descargando {} -> {}".format(url, nombre))

    gc.collect()

    tmp_path = nombre + ".tmp"
    status, bytes_written = _http_download(url, tmp_path)

    if status != 200 or bytes_written == 0:
        log_persistente("FASE0", "Fallo descarga de {} (status={})".format(nombre, status), "WARN")
        try:
            if tmp_path in os.listdir():
                os.remove(tmp_path)
        except OSError:
            pass
        return False, nombre

    if bytes_written < 50:
        log_warn("FASE0", "{} descargado con solo {} bytes, descartado".format(nombre, bytes_written))
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return False, nombre

    # Verificacion ligera: leer primeros y ultimos bytes
    try:
        with open(tmp_path, "rb") as f:
            head = f.read(64)
        if len(head) == 0:
            raise ValueError("Archivo vacio")
    except Exception as e:
        log_warn("FASE0", "Verificacion fallida para {}: {}".format(nombre, e))
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return False, nombre

    # Comparar con local (si existe y mismo tamano)
    local_existe = nombre in os.listdir()
    if local_existe:
        try:
            tam_local = os.stat(nombre)[6]
            if tam_local == bytes_written:
                if _archivos_iguales_por_chunks(tmp_path, nombre):
                    log_persistente("FASE0", "{} remoto identico al local. Sin cambios.".format(nombre), "INFO")
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
                    return False, nombre
        except Exception as e:
            log_warn("FASE0", "Error comparando {}: {}".format(nombre, e))

    # Instalar: rename atomico
    try:
        os.rename(tmp_path, nombre)
        os.sync()
        log_persistente("FASE0", "{} actualizado ({} bytes).".format(nombre, bytes_written), "INFO")
        return True, nombre
    except Exception as e:
        log_persistente("FASE0", "Error actualizando {}: {}".format(nombre, e), "ERROR")
        try:
            if tmp_path in os.listdir():
                os.remove(tmp_path)
        except OSError:
            pass
        return False, nombre


def _actualizar_modulos_py(cfg_dict):
    urls = cfg_dict.get("ficheros_a_actualizar", [])
    if not urls:
        log_debug("FASE0", "No hay ficheros a actualizar en config.json")
        return False

    if not isinstance(urls, list):
        log_warn("FASE0", "ficheros a actualizar no es una lista")
        return False

    log_persistente("FASE0", "Iniciando actualizacion de {} modulo(s) .py".format(len(urls)), "INFO")

    alguno_actualizado = False
    for url in urls:
        if not isinstance(url, str) or not url.strip():
            continue
        gc.collect()
        actualizado, nombre = _actualizar_archivo_py(url.strip())
        if actualizado:
            alguno_actualizado = True
        time.sleep_ms(DELAY_ENTRE_DESCARGAS_MS)

    return alguno_actualizado


# =========================================================================
# FLUJO PRINCIPAL
# =========================================================================

def ejecutar():
    log_persistente("FASE0", "Iniciando check de actualizacion remota", "INFO")
    gc.collect()

    cambios = False
    cfg = None

    # --- 1. Actualizar config.json (descarga directa a archivo) ---
    tmp_cfg = CONFIG_LOCAL + ".new"
    status, bytes_cfg = _http_download(URL_CONFIG_JSON, tmp_cfg)

    if status == 200 and bytes_cfg > 0:
        try:
            tam_local = os.stat(CONFIG_LOCAL)[6]
        except OSError:
            tam_local = 0

        log_persistente("FASE0", "config.json descargado: {} bytes (local: {} bytes)".format(bytes_cfg, tam_local), "INFO")

        try:
            with open(tmp_cfg, "rb") as f:
                data_bytes = f.read()
            cfg = json.loads(data_bytes.decode("utf-8"))
        except Exception as e:
            log_persistente("FASE0", "JSON invalido: {}".format(e), "WARN")
            try:
                os.remove(tmp_cfg)
            except OSError:
                pass
            cfg = None

        if cfg is not None:
            if not _configs_iguales_normalizados(cfg):
                if _validar_config(cfg):
                    # Verificacion post-escritura
                    try:
                        with open(tmp_cfg, "rb") as f:
                            leido = f.read()
                        if leido != data_bytes:
                            raise ValueError("Tamano esperado={}, leido={}".format(len(data_bytes), len(leido)))
                        cfg_verif = json.loads(leido.decode("utf-8"))
                        if "wifi_ssid" not in cfg_verif:
                            raise ValueError("Falta wifi_ssid")
                    except Exception as e:
                        log_warn("FASE0", "JSON post-escritura invalido: {}".format(e))
                        try:
                            os.remove(tmp_cfg)
                        except OSError:
                            pass
                    else:
                        # Backup atomico
                        if CONFIG_LOCAL in os.listdir():
                            try:
                                if CONFIG_BACKUP in os.listdir():
                                    os.remove(CONFIG_BACKUP)
                                os.rename(CONFIG_LOCAL, CONFIG_BACKUP)
                                log_persistente("FASE0", "Backup creado: {}".format(CONFIG_BACKUP), "INFO")
                            except Exception as e:
                                log_warn("FASE0", "No se pudo crear backup: {}".format(e))

                        os.rename(tmp_cfg, CONFIG_LOCAL)
                        os.sync()

                        try:
                            import config_system
                            config_system._CONFIG_CACHE = None
                        except Exception as e:
                            log_warn("FASE0", "No se pudo invalidar cache de config_system: {}".format(e))

                        log_persistente("FASE0", "config.json actualizado. Reinicio requerido.", "INFO")
                        cambios = True
                else:
                    log_persistente("FASE0", "Validacion rechazo el nuevo config.json", "WARN")
                    try:
                        os.remove(tmp_cfg)
                    except OSError:
                        pass
            else:
                log_persistente("FASE0", "config.json remoto es identico al local tras normalizar. Sin cambios.", "INFO")
                try:
                    os.remove(tmp_cfg)
                except OSError:
                    pass
    else:
        log_persistente("FASE0", "Fallo descarga de config.json (status={})".format(status), "WARN")
        try:
            if tmp_cfg in os.listdir():
                os.remove(tmp_cfg)
        except OSError:
            pass

    # --- 2. Actualizar modulos .py ---
    if cfg is not None:
        time.sleep(2)   # Pausa entre fases para no saturar GitHub
        if _actualizar_modulos_py(cfg):
            cambios = True

    return cambios


def test():
    print("=" * 60)
    print("FASE0 - MODO TEST (solo lectura, sin escritura)")
    print("=" * 60)
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
            urls = cfg.get("ficheros_a_actualizar", [])
            print("ficheros a actualizar:", urls)
            for url in urls:
                nombre = _extraer_nombre_archivo(url)
                print("  URL: {} -> nombre: {}".format(url, nombre))
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


if __name__ == "__main__":
    try:
        from red import conectar_wifi
        if conectar_wifi():
            res = ejecutar()
            print("FASE0 resultado:", res)
        else:
            print("FASE0: No se pudo conectar WiFi")
    except Exception as e:
        print("FASE0: Error en ejecucion directa:", e)