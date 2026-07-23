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
    # Lee config.json. Si falla, devuelve diccionario vacío."""
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
    # Guarda la fase en estado.json. Si falla, devuelve False
    try:
        with open(_ESTADO_FILE, "w") as f:
            json.dump({"fase": int(fase)}, f)
            f.flush()
            os.sync()
        return True
    except (OSError, ValueError):
        return False


def leer_fase():
    # Lee la fase desde estado.json. Si falla, devuelve 1 (fase1 por defecto)
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


# =========================================================================
# CLASES AUXILIARES (fusionadas desde config_fase3.py, email_state.py,
#                    sweep_params.py en A.3)
# =========================================================================

import time  # necesario para EstadoEmail


class ConfigFase3:
    # Cachea parámetros de config relevantes para fase3

    def __init__(self, config):
        self._raw = config

        self.email_cada_min = int(config.get("email_estado_cada_minutos", 5))
        self.email_cada_seg = self.email_cada_min * 60
        self.horas_fijas = self._parsear_horas(config.get('email_estado_horas_fijas', []))

        self.heartbeat_base_min = int(config.get("heartbeat_intervalo_base_min", 15))
        self.heartbeat_pase_min = int(config.get("heartbeat_intervalo_pase_min", 2))
        self.heartbeat_activo = config.get("heartbeat", True)

        self.ventilador_activo = config.get("ventilador_activo", False)
        self.ventilador_gpio = int(config.get("ventilador_gpio", 38))
        self.ventilador_on = float(config.get("ventilador_umbral_on_c", 55.0))
        self.ventilador_off = float(config.get("ventilador_umbral_off_c", 45.0))

        self.doppler_activo = config.get("doppler", True)

        perfil_id = config.get("grupo_satelites_actual", "uhf_433")
        perfil = config.get("perfiles_satelites", {}).get(perfil_id, {})
        sweep_cfg = perfil.get("parametros_sweep", {})
        self.sweep_combinaciones = sweep_cfg.get("combinaciones", [
            {"rx_iq": False, "crc_on": False, "implicit_header": False, "sync_word": 18},
            {"rx_iq": False, "crc_on": True,  "implicit_header": False, "sync_word": 18},
            {"rx_iq": True,  "crc_on": False, "implicit_header": False, "sync_word": 18},
            {"rx_iq": True,  "crc_on": True,  "implicit_header": False, "sync_word": 18},
        ])
        self.sweep_intervalo = int(sweep_cfg.get("intervalo_seg", 5))
        self.sweep_activo_global = config.get("buscar_parametros", False)

        self.min_ram = int(config["seguridad_hardware"]["minima_ram_alerta_bytes"])
        self.max_wifi_intentos = int(config["seguridad_hardware"]["max_intentos_wifi"])

        self.max_logs_txt_kb = int(config.get("max_logs_txt_kb", 50))
        self.max_logs_txt_lineas = int(config.get("max_logs_txt_lineas", 200))

        self.debug = config.get("debug_consola", True)
        self.perfiles = config.get("perfiles_satelites", {})

    @staticmethod
    def _parsear_horas(horas_raw):
        if not horas_raw:
            return []
        resultado = []
        for h in horas_raw:
            try:
                partes = str(h).strip().split(":")
                if len(partes) == 2:
                    hh = int(partes[0])
                    mm = int(partes[1])
                    if 0 <= hh < 24 and 0 <= mm < 60:
                        resultado.append(hh * 3600 + mm * 60)
            except Exception:
                pass
        return sorted(set(resultado))


class EstadoEmail:
    # Gestiona horas fijas de envío y persistencia

    def __init__(self, horas_fijas_seg, email_cada_seg):
        self._horas_fijas = horas_fijas_seg
        self._email_cada_seg = email_cada_seg
        self._ultimo_email_ts = time.time() if email_cada_seg > 0 else 0
        self._ultima_hora_enviada = self._cargar_ultima_hora()

    def _cargar_ultima_hora(self):
        try:
            with open("ultima_hora_email.txt", "r") as f:
                return int(f.read().strip())
        except Exception:
            return None

    def _guardar_ultima_hora(self, hora_seg):
        try:
            with open("ultima_hora_email.txt", "w") as f:
                f.write(str(hora_seg))
                f.flush()
                os.sync()
        except Exception:
            pass

    def toca_enviar(self, t_local_tuple, forzado=False):
        if forzado:
            return True
        if self._horas_fijas:
            hh, mm, ss = t_local_tuple[3], t_local_tuple[4], t_local_tuple[5]
            actual_seg = hh * 3600 + mm * 60 + ss
            for hf_seg in self._horas_fijas:
                diff = actual_seg - hf_seg
                if 0 <= diff <= 30:
                    if self._ultima_hora_enviada != hf_seg:
                        self._ultima_hora_enviada = hf_seg
                        self._guardar_ultima_hora(hf_seg)
                        return True
            return False
        elif self._email_cada_seg > 0:
            if (time.time() - self._ultimo_email_ts) >= self._email_cada_seg:
                self._ultimo_email_ts = time.time()
                return True
        return False

    def marcar_enviado(self):
        self._ultimo_email_ts = time.time()

    def proxima_hora_str(self, t_local_tuple):
        if not self._horas_fijas:
            return "---"
        hh, mm, ss = t_local_tuple[3], t_local_tuple[4], t_local_tuple[5]
        actual_seg = hh * 3600 + mm * 60 + ss
        for hf_seg in self._horas_fijas:
            if hf_seg > actual_seg:
                return '{:02d}:{:02d}'.format(hf_seg // 3600, (hf_seg % 3600) // 60)
        hf_seg = self._horas_fijas[0]
        return '{:02d}:{:02d}'.format(hf_seg // 3600, (hf_seg % 3600) // 60)

    def info_str(self, t_local_tuple):
        if self._horas_fijas:
            return "EMAIL:FIJO->{}".format(self.proxima_hora_str(t_local_tuple))
        elif self._email_cada_seg > 0:
            return "EMAIL:{}s/{}s".format(int(time.time() - self._ultimo_email_ts), self._email_cada_seg)
        return "EMAIL:OFF"



_MAX_PAY_LEN = 255

class SweepParametros:
    def __init__(self, combinaciones, intervalo_seg, activo_global, perfiles=None):
        self._combinaciones = combinaciones
        self._intervalo = intervalo_seg
        self._activo_global = activo_global
        self._perfiles = perfiles or {}
        self._idx = 0
        self._locked = False
        self._last_change = 0

    def calcular(self, sat_objeto, utc_unix):
        buscar = self._debe_buscar(sat_objeto)
        if buscar and sat_objeto is not None and not self._locked:
            if (utc_unix - self._last_change) >= self._intervalo:
                self._idx = (self._idx + 1) % len(self._combinaciones)
                self._last_change = utc_unix

        if buscar and sat_objeto is not None:
            cfg = self._combinaciones[self._idx]
        else:
            cfg = {}

        lora = sat_objeto.get("lora", {}) if sat_objeto else {}
        cab_imp = cfg.get("implicit_header", lora.get("implicit_header", False))
        pay_len = int(lora.get("payload_len", _MAX_PAY_LEN))
        crc_on = cfg.get("crc_on", bool(lora.get("crc_on", False)))
        rx_iq = cfg.get("rx_iq", bool(lora.get("rx_iq", False)))
        sync_word = cfg.get("sync_word", int(lora.get("sync_word", 18)))

        return cfg, cab_imp, pay_len, crc_on, rx_iq, sync_word

    def lock(self):
        self._locked = True

    def reset(self):
        self._idx = 0
        self._locked = False
        self._last_change = 0

    @property
    def locked(self):
        return self._locked

    @locked.setter
    def locked(self, valor):
        self._locked = bool(valor)

    def _debe_buscar(self, sat_objeto):
        if sat_objeto is None:
            return self._activo_global
        nombre_sat = sat_objeto.get("satelite", {}).get("nombre", None)
        if nombre_sat:
            for perfil_id, perfil in self._perfiles.items():
                satelites = perfil.get("satelites", {})
                if nombre_sat in satelites:
                    buscar = satelites[nombre_sat].get("buscar_parametros", None)
                    if buscar is not None:
                        return bool(buscar)
        return self._activo_global