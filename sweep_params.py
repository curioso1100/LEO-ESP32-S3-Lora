# =========================================================================
# MÓDULO: sweep_params.py - Gestión de parámetros de búsqueda (sweep)
# =========================================================================
# Extraído de fase3.py V7.0.0
# Recibe perfiles como parámetro, no lee CONFIG global.
# =========================================================================

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
