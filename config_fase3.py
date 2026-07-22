# =========================================================================
# MÓDULO: config_fase3.py - Configuración cacheada para fase3
# =========================================================================


import os


class ConfigFase3:
    """Cachea parámetros de config relevantes para fase3."""

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
