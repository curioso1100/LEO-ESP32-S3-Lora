# =========================================================================
# MODULO: sat_identifier.py - Identificacion de satelites por header
# =========================================================================

from logger import log_debug, log_warn

class IdentificadorSat:
    def __init__(self, perfiles, debug=False):
        self._debug = debug
        self._perfiles = perfiles  # <- NUEVO: guardar perfiles para consulta posterior
        self._reglas = self._cargar_reglas(perfiles)

    def _cargar_reglas(self, perfiles):
        reglas = []
        try:
            for perfil_id, perfil in perfiles.items():
                satelites = perfil.get("satelites", {})
                for nombre, cfg in satelites.items():
                    id_cfg = cfg.get("identificacion_header")
                    if id_cfg:
                        num_reglas = len(id_cfg.get("reglas", []))
                        long_min = id_cfg.get("longitud_minima", 1)
                        # Los satelites CON longitud_exacta son mas especificos
                        # y deben evaluarse PRIMERO (prioridad alta = 1000)
                        long_exacta = 0 if id_cfg.get("longitud_exacta") is None else 1000
                        reglas.append((nombre, id_cfg, num_reglas, long_min, long_exacta))
            reglas.sort(key=lambda x: (x[2], x[3], -x[4]), reverse=True)
            if self._debug:
                orden_str = ", ".join(["{}({}r)".format(r[0], r[2]) for r in reglas])
                log_debug("ID_HEADER", "Orden evaluacion: " + orden_str)
        except Exception as e:
            log_warn("ID_HEADER", "Error cargando reglas: {}".format(e))
        return [(r[0], r[1]) for r in reglas]

    def identificar(self, datos_raw):
        if datos_raw is None:
            return None
        for nombre_sat, cfg in self._reglas:
            long_min = cfg.get("longitud_minima", 1)
            if len(datos_raw) < long_min:
                continue
            long_exacta = cfg.get("longitud_exacta")
            if long_exacta is not None and len(datos_raw) != long_exacta:
                continue
            long_max = cfg.get("longitud_maxima")
            if long_max is not None and len(datos_raw) > long_max:
                continue

            coincidencia = True
            for regla in cfg.get("reglas", []):
                offset = regla["offset"]
                if offset >= len(datos_raw):
                    coincidencia = False
                    break
                valor_esperado = int(regla["valor"], 0)
                valor_real = datos_raw[offset]
                mask = regla.get("mask")
                if mask is not None:
                    valor_real = valor_real & int(mask, 0)
                if valor_real != valor_esperado:
                    coincidencia = False
                    break
            if coincidencia:
                return nombre_sat
        return None

    # =========================================================================
    # NUEVO: metodos para desambiguacion por familia de header
    # =========================================================================

    def _familia_header(self, nombre_sat):
        """Devuelve la familia_header de un satelite, o None si no tiene."""
        try:
            for perfil_id, perfil in self._perfiles.items():
                satelites = perfil.get("satelites", {})
                if nombre_sat in satelites:
                    id_cfg = satelites[nombre_sat].get("identificacion_header", {})
                    return id_cfg.get("familia_header", None)
        except Exception:
            pass
        return None

    def misma_familia(self, nombre_a, nombre_b):
        """Devuelve True si ambos satelites pertenecen a la misma familia_header."""
        if nombre_a is None or nombre_b is None:
            return False
        if nombre_a == nombre_b:
            return True
        fam_a = self._familia_header(nombre_a)
        fam_b = self._familia_header(nombre_b)
        return fam_a is not None and fam_a == fam_b

    def frecuencia_nominal(self, nombre_sat):
        try:
            for perfil_id, perfil in self._perfiles.items():
                satelites = perfil.get("satelites", {})
                if nombre_sat in satelites:
                    frec_hz = satelites[nombre_sat].get("frec", None)
                    if frec_hz is not None:
                        return frec_hz / 1000000.0
        except Exception:
            pass
        return None
