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

# Archivos que NUNCA se actualizan remotamente (riesgo de brick)
ARCHIVOS_PROTEGIDOS = {"fase0.py", "main.py", "boot.py"}

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


# =========================================================================
# ACTUALIZACION REMOTA DE ARCHIVOS .py (V9.0)
# =========================================================================

def _extraer_nombre_archivo(url):
    """Extrae el nombre del archivo de una URL raw de Gist.
    Ej: https://.../raw/red.py -> red.py
    """
    # Quitar parametros de query
    url_limpia = url.split("?")[0]
    # Quitar trailing slash
    url_limpia = url_limpia.rstrip("/")
    # Extraer ultimo componente
    idx = url_limpia.rfind("/")
    if idx >= 0:
        nombre = url_limpia[idx + 1:]
    else:
        nombre = url_limpia
    # Validar que termine en .py
    if not nombre.endswith(".py"):
        return None
    return nombre




def _normalizar_url_gist(url):
    """Corrige URLs de Gist que incluyen hash de commit, usando la URL 'latest'.
    GitHub conserva archivos borrados si la URL incluye el hash del commit.
    La URL 'latest' (sin hash) devuelve 404 correctamente cuando el archivo no existe.
    NOTA: No usa 're' porque MicroPython no soporta cuantificadores {n} en regex.
    """
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

def _archivos_iguales(path_a, path_b):
    """Compara dos archivos byte a byte."""
    try:
        with open(path_a, "rb") as fa:
            data_a = fa.read()
        with open(path_b, "rb") as fb:
            data_b = fb.read()
        return data_a == data_b
    except OSError:
        return False


def _actualizar_archivo_py(url):
    """Descarga un archivo .py remoto y lo instala si difiere del local.
    Retorna (actualizado: bool, nombre: str)
    """
    nombre = _extraer_nombre_archivo(url)
    if not nombre:
        log_warn("FASE0", "URL sin nombre de archivo .py valido: {}".format(url))
        return False, None

    if nombre in ARCHIVOS_PROTEGIDOS:
        log_warn("FASE0", "Archivo protegido, omitido: {}".format(nombre))
        return False, nombre

    # Normalizar URL (eliminar hash de commit si existe)
    url = _normalizar_url_gist(url)

    log_debug("FASE0", "Descargando {} -> {}".format(url, nombre))

    status, data_bytes = _http_get(url)
    if status != 200 or not data_bytes:
        log_persistente("FASE0", "Fallo descarga de {} (status={})".format(nombre, status), "WARN")
        return False, nombre

    if len(data_bytes) < 50:
        log_warn("FASE0", "{} descargado con solo {} bytes, descartado".format(nombre, len(data_bytes)))
        return False, nombre

    tmp_path = nombre + ".tmp"
    local_existe = nombre in os.listdir()

    try:
        # Escribir a archivo temporal
        with open(tmp_path, "wb") as f:
            f.write(data_bytes)
            f.flush()
            os.sync()

        # Verificar escritura
        time.sleep_ms(100)
        with open(tmp_path, "rb") as f:
            leido = f.read()
        if leido != data_bytes:
            log_warn("FASE0", "Verificacion fallida para {}".format(nombre))
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            return False, nombre

        # Comparar con local (si existe)
        if local_existe and _archivos_iguales(tmp_path, nombre):
            log_persistente("FASE0", "{} remoto identico al local. Sin cambios.".format(nombre), "INFO")
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            return False, nombre

        # Instalar: rename atomico
        os.rename(tmp_path, nombre)
        os.sync()

        # Limpiar tmp residual
        if tmp_path in os.listdir():
            try:
                os.remove(tmp_path)
            except OSError:
                pass

        log_persistente("FASE0", "{} actualizado ({} bytes).".format(nombre, len(data_bytes)), "INFO")
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
    """Actualiza todos los modulos .py listados en config.json[ficheros_a_actualizar].
    Retorna True si al menos uno cambio.
    """
    urls = cfg_dict.get("ficheros_a_actualizar", [])
    if not urls:
        log_debug("FASE0", "No hay ficheros_a_actualizar en config.json")
        return False

    if not isinstance(urls, list):
        log_warn("FASE0", "ficheros_a_actualizar no es una lista")
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
        time.sleep_ms(500)  # Pausa entre descargas

    return alguno_actualizado


# =========================================================================
# FLUJO PRINCIPAL
# =========================================================================

def ejecutar():
    log_persistente("FASE0", "Iniciando check de actualizacion remota", "INFO")

    cambios = False

    # --- 1. Actualizar config.json (existente, sin cambios) ---
    status, data_bytes = _http_get(URL_CONFIG_JSON)
    if status == 200 and data_bytes:
        tam_remoto = len(data_bytes)
        try:
            tam_local = os.stat(CONFIG_LOCAL)[6]
        except OSError:
            tam_local = 0

        log_persistente("FASE0", "config.json descargado: {} bytes (local: {} bytes)".format(tam_remoto, tam_local), "INFO")

        try:
            cfg = json.loads(data_bytes.decode("utf-8"))
        except Exception as e:
            log_persistente("FASE0", "JSON invalido: {}".format(e), "WARN")
            cfg = None

        if cfg is not None:
            if not _configs_iguales_normalizados(cfg):
                if _validar_config(cfg):
                    # Escritura ATOMICA
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
                        else:
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
                            else:
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

                                log_persistente("FASE0", "config.json actualizado. Reinicio requerido.", "INFO")
                                cambios = True
                    except Exception as e:
                        log_error("FASE0", "Error escribiendo config.json: {}".format(e))
                        log_persistente("FASE0", "Error escribiendo config.json: {}".format(e), "ERROR")
                        try:
                            if temp_file in os.listdir():
                                os.remove(temp_file)
                        except OSError:
                            pass
                else:
                    log_persistente("FASE0", "Validacion rechazo el nuevo config.json", "WARN")
            else:
                log_persistente("FASE0", "config.json remoto es identico al local tras normalizar (ignorando claves volatiles). Sin cambios.", "INFO")
    else:
        log_persistente("FASE0", "Fallo descarga de config.json (status={})".format(status), "WARN")

    # --- 2. Actualizar modulos .py (V9.0) ---
    if cfg is not None:
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
            # Test de ficheros_a_actualizar
            urls = cfg.get("ficheros_a_actualizar", [])
            print("ficheros_a_actualizar:", urls)
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
