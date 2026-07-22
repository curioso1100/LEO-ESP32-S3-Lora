# =====================================================
# MÓDULO: itv_manager.py - Gestión de ITV de la placa
# =====================================================
#
# INSTRUCCIONES DE USO:
# ------------------------------------------------
#
# 1. FORZAR ITV DE PRUEBA (desde consola Thonny):
#    >>> import itv_manager          # Ejecuta main() automaticamente
#    # o pulsa F5 con este fichero abierto
#    # Luego reinicia: import machine; machine.reset()
#
# 2. VER ESTADO ITV:
#    >>> from itv_manager import ITVManager
#    >>> from tiempo import obtener_unix_utc_real
#    >>> itv = ITVManager()
#    >>> print(itv.resumen_compacto())   # Ej: "ITV:OK 3/90 -"
#    >>> print(itv.info_debug())        # Dict con metricas completas
#
# 3. MARCAR ITV REALIZADA (boton PRG o consola):
#    >>> itv.marcar_itv_realizada(obtener_unix_utc_real(), "revision_ok")
#    # O en fase3: pulsa PRG 3 veces cortas (<1s cada una, en 5s)
#
# 4. FICHEROS ITV EN FLASH:
#    - itv_estado.json          : Metricas acumuladas (NO borrar)
#    - itv_email_pendiente.json : Email ITV preparado (se borra tras envio)
#
# 5. CRITERIOS ITV (configurables en config.json -> "itv"):
#    - dias_maximos: 90 dias sin revision
#    - ventilador_activaciones_7d: 3+ activaciones (polvo/obstruccion)
#    - delta_temp_maxima_c: +5C vs mes anterior (degradacion termica)
#    - delta_rssi_db: -10dB vs historico (problema antena)
#    - reinicios_7d: 2+ reinicios (humedad/PSU inestable)
#    - dias_sin_capturas: 7 dias sin capturas (antena desconectada)
#    - dias_sin_heartbeat_enviado: 3 dias sin email (fallo red/energia)
#
# 6. RESUMEN EN HEARTBEAT:
#    ITV:OK 1/90 -     -> Dia 1 de 90, todo OK
#    ITV:PENDIENTE 91/90 ITV_RUTINARIA: 91 dias -> ITV vencida
#

import json
import os
import time
import gc

from logger import log_info, log_warn, log_debug, log_persistente


# =========================================================================
# CONSTANTES
# =========================================================================

ITV_FICHERO = "itv_estado.json"
ITV_EMAIL_FICHERO = "itv_email_pendiente.json"

DEFAULT_UMBRALES = {
    "dias_maximos": 90,
    "ventilador_activaciones_7d": 3,
    "delta_temp_maxima_c": 5,
    "delta_rssi_db": 10,
    "reinicios_7d": 2,
    "dias_sin_capturas": 7,
    "dias_sin_heartbeat_enviado": 3,
}


# =========================================================================
# CLASE PRINCIPAL
# =========================================================================

class ITVManager:
    """Gestiona la lógica de mantenimiento preventivo de la estación LEO."""

    def __init__(self, config=None):
        self._cfg = config or {}
        self._umbrales = self._cargar_umbrales()
        self._estado = self._cargar_estado()
        self._itv_pendiente = False
        self._motivo_itv = []
        self._inicializar_metricas()

    # ------------------------------------------------------------------
    # Carga / persistencia
    # ------------------------------------------------------------------

    def _cargar_umbrales(self):
        cfg_itv = self._cfg.get("itv", {})
        return {k: cfg_itv.get(k, v) for k, v in DEFAULT_UMBRALES.items()}

    def _cargar_estado(self):
        try:
            with open(ITV_FICHERO, "r") as f:
                return json.load(f)
        except (OSError, ValueError):
            return self._estado_inicial()

    def _estado_inicial(self):
        return {
            "ultima_itv_timestamp": 0,
            "ultima_itv_motivo": "inicial",
            "dias_acumulados": 0,
            "heartbeats_acumulados": 0,
            "reinicios_7d": 0,
            "reinicios_ultima_semana": 0,
            "ventilador_activaciones_7d": 0,
            "ventilador_activaciones_historico": [],
            "temperaturas_max_semanal": [],
            "capturas_ultimos_7d": 0,
            "capturas_historico": [],
            "rssi_por_satelite": {},
            "emails_enviados_ultimos_7d": 0,
            "emails_enviados_historico": [],
            "ultimo_dia_calculado": 0,
            "ultimo_timestamp_diario": 0,
            "version_estado": 3,
        }

    def _guardar_estado(self):
        try:
            with open(ITV_FICHERO, "w") as f:
                json.dump(self._estado, f)
                f.flush()
                os.sync()
        except Exception as e:
            log_warn("ITV", "No se pudo guardar estado: {}".format(e))

    # ------------------------------------------------------------------
    # Inicialización de métricas desde logs existentes
    # ------------------------------------------------------------------

    def _inicializar_metricas(self):
        if self._estado["ultima_itv_timestamp"] != 0:
            return
        try:
            self._reconstruir_desde_heartbeat_log()
        except Exception as e:
            log_debug("ITV", "No se pudo reconstruir: {}".format(e))

    def _reconstruir_desde_heartbeat_log(self):
        """Reconstruye métricas desde heartbeat.log.

        FIX v7.3.0: Calcula dias_acumulados basado en tiempo transcurrido real,
        no solo en conteo de heartbeats. Esto evita que siempre muestre dias=1.
        """
        try:
            with open("heartbeat.log", "r") as f:
                lineas = f.readlines()
        except OSError:
            return

        if not lineas:
            return

        try:
            from tiempo import obtener_unix_utc_real
            utc_actual = obtener_unix_utc_real()
        except Exception:
            utc_actual = int(time.time())

        try:
            from tiempo import obtener_tiempo_actual
            _, _, t_local = obtener_tiempo_actual()
            dia_actual = t_local[7]
        except Exception:
            import time
            dia_actual = time.localtime()[7]

        hb_count = len([l for l in lineas if l.strip().startswith("HB ")])

        # FIX v7.3.0: Calcular dias_acumulados desde el primer heartbeat
        dias_estimados = self._calcular_dias_desde_primer_hb(lineas, utc_actual)
        if dias_estimados < 1:
            dias_estimados = max(1, hb_count // 96)

        self._estado["dias_acumulados"] = dias_estimados
        self._estado["heartbeats_acumulados"] = hb_count
        self._estado["ultimo_dia_calculado"] = dia_actual
        self._estado["ultimo_timestamp_diario"] = utc_actual

        try:
            with open("satelites_cazados.txt", "r") as f:
                capturas = len([l for l in f.readlines() if l.strip()])
            self._estado["capturas_ultimos_7d"] = capturas
            self._estado["capturas_historico"].append([utc_actual, capturas])
        except OSError:
            pass

        self._guardar_estado()
        log_info("ITV", "Reconstruido: ~{} dias, {} HB, {} CAP (dia={})".format(
            dias_estimados, hb_count, self._estado['capturas_ultimos_7d'], dia_actual))

    def _calcular_dias_desde_primer_hb(self, lineas, utc_actual):
        """NUEVO v7.3: Extrae timestamp del primer heartbeat y calcula días transcurridos."""
        try:
            for linea in lineas:
                if linea.strip().startswith("HB "):

                    partes = linea.strip().split()
                    if len(partes) >= 2:
                        ts_str = partes[1]
                        from tiempo import parsear_timestamp
                        ts_unix = parsear_timestamp(ts_str)
                        if ts_unix and ts_unix > 0:
                            segundos = utc_actual - ts_unix
                            return max(1, segundos // 86400)
        except Exception:
            pass
        return 0

    # ------------------------------------------------------------------
    # Actualización de métricas
    # ------------------------------------------------------------------

    def actualizar(self, temp_cpu, ventilador_on, rssi_satelite, sat_nombre,
                   reinicios, heartbeat_enviado, email_enviado, capturas_count,
                   utc_actual, t_local_tuple):
        dia_actual = t_local_tuple[7]

        # FIX v7.3.0: Calcular días transcurridos desde último timestamp diario
        dias_transcurridos = self._calcular_dias_transcurridos(utc_actual)

        # Reset diario: si ha pasado al menos un día real desde último reset
        if dias_transcurridos >= 1 and self._estado["ultimo_timestamp_diario"] > 0:
            for _ in range(dias_transcurridos):
                self._reset_diario(dia_actual, utc_actual)
        elif self._estado["ultimo_timestamp_diario"] == 0:
            self._estado['ultimo_timestamp_diario'] = utc_actual
            self._estado['ultimo_dia_calculado'] = dia_actual

        if heartbeat_enviado:
            self._estado["heartbeats_acumulados"] += 1

        if email_enviado:
            self._estado["emails_enviados_ultimos_7d"] += 1
            self._estado["emails_enviados_historico"].append(utc_actual)

        if temp_cpu is not None:
            temp_hoy = self._estado.get("_temp_max_hoy", None)
            if temp_hoy is None or temp_cpu > temp_hoy:
                self._estado["_temp_max_hoy"] = temp_cpu

        if ventilador_on:
            if not self._estado.get("_ventilador_estaba_on", False):
                self._estado["ventilador_activaciones_7d"] += 1
                self._estado["ventilador_activaciones_historico"].append([utc_actual, temp_cpu])
                self._estado["_ventilador_estaba_on"] = True
        else:
            self._estado["_ventilador_estaba_on"] = False

        if rssi_satelite is not None and sat_nombre:
            if sat_nombre not in self._estado["rssi_por_satelite"]:
                self._estado["rssi_por_satelite"][sat_nombre] = []
            self._estado["rssi_por_satelite"][sat_nombre].append([utc_actual, rssi_satelite])
            if len(self._estado["rssi_por_satelite"][sat_nombre]) > 50:
                self._estado["rssi_por_satelite"][sat_nombre] = (
                    self._estado['rssi_por_satelite'][sat_nombre][-50:])

        reinicios_prev = self._estado.get('_reinicios_previos', 0)
        if reinicios > reinicios_prev:
            delta = reinicios - reinicios_prev
            self._estado["reinicios_7d"] += delta
            self._estado["reinicios_ultima_semana"] += delta
            self._estado["_reinicios_previos"] = reinicios

        capturas_prev = self._estado.get('_capturas_previas', 0)
        if capturas_count > capturas_prev:
            self._estado["capturas_ultimos_7d"] += (capturas_count - capturas_prev)
            self._estado["_capturas_previas"] = capturas_count

        if self._estado["heartbeats_acumulados"] % 10 == 0:
            self._guardar_estado()

    def _calcular_dias_transcurridos(self, utc_actual):
        """NUEVO v7.3: Calcula días reales transcurridos desde último reset diario."""
        ultimo_ts = self._estado.get("ultimo_timestamp_diario", 0)
        if ultimo_ts == 0:
            return 0
        segundos = utc_actual - ultimo_ts
        return segundos // 86400

    def _reset_diario(self, dia_actual, utc_actual):
        temp_max_hoy = self._estado.pop('_temp_max_hoy', None)
        if temp_max_hoy is not None:
            self._estado["temperaturas_max_semanal"].append([utc_actual, temp_max_hoy])
            if len(self._estado["temperaturas_max_semanal"]) > 28:
                self._estado["temperaturas_max_semanal"] = (
                    self._estado['temperaturas_max_semanal'][-28:])

        capturas_hoy = self._estado.get('capturas_ultimos_7d', 0)
        self._estado["capturas_historico"].append([utc_actual, capturas_hoy])
        if len(self._estado["capturas_historico"]) > 30:
            self._estado["capturas_historico"] = (
                self._estado['capturas_historico'][-30:])

        self._estado["capturas_ultimos_7d"] = 0
        self._estado["ventilador_activaciones_7d"] = 0
        self._estado["reinicios_7d"] = 0
        self._estado["emails_enviados_ultimos_7d"] = 0
        self._estado["dias_acumulados"] += 1

        self._limpiar_historico_antiguo(utc_actual)
        self._estado["ultimo_dia_calculado"] = dia_actual
        self._estado["ultimo_timestamp_diario"] = utc_actual
        self._guardar_estado()

        log_info("ITV", "Reset diario: dia={}, dias_acum={}, temp_max_hoy={}".format(
            dia_actual, self._estado['dias_acumulados'], temp_max_hoy))

    def _limpiar_historico_antiguo(self, utc_actual):
        limite = utc_actual - (7 * 86400)
        for clave in ["ventilador_activaciones_historico", "emails_enviados_historico"]:
            self._estado[clave] = [e for e in self._estado[clave]
                                    if (e[0] if isinstance(e, list) else e) > limite]

    # ------------------------------------------------------------------
    # Evaluación de triggers ITV
    # ------------------------------------------------------------------

    def evaluar(self, utc_actual, t_local_tuple):
        motivos = []
        dias_acum = self._estado["dias_acumulados"]
        dias_desde_ultima_itv = 0

        if self._estado["ultima_itv_timestamp"] > 0:
            dias_desde_ultima_itv = (utc_actual - self._estado["ultima_itv_timestamp"]) // 86400
        else:
            dias_desde_ultima_itv = dias_acum

        if dias_desde_ultima_itv >= self._umbrales["dias_maximos"]:
            motivos.append("ITV_RUTINARIA: {} dias".format(dias_desde_ultima_itv))

        if self._estado["ventilador_activaciones_7d"] >= self._umbrales["ventilador_activaciones_7d"]:
            motivos.append("DEGRADACION_TERMICA: {} activaciones".format(
                self._estado["ventilador_activaciones_7d"]))

        temp_alert = self._evaluar_temperatura()
        if temp_alert:
            motivos.append(temp_alert)

        rssi_alert = self._evaluar_rssi()
        if rssi_alert:
            motivos.append(rssi_alert)

        if self._estado["reinicios_7d"] >= self._umbrales["reinicios_7d"]:
            motivos.append("REINICIOS: {} en 7d".format(self._estado["reinicios_7d"]))

        capturas_7d = self._estado["capturas_ultimos_7d"]
        if capturas_7d == 0 and dias_acum > self._umbrales["dias_sin_capturas"]:
            try:
                with open("agenda.json", "r") as f:
                    if len(json.load(f).get("pases", [])) > 0:
                        motivos.append("SIN_CAPTURAS: 0 en 7d")
            except (OSError, ValueError):
                pass

        if self._estado["emails_enviados_ultimos_7d"] == 0 and dias_acum > self._umbrales["dias_sin_heartbeat_enviado"]:
            motivos.append("SIN_COMUNICACION: 0 emails en 7d")

        itv_necesaria = len(motivos) > 0
        if itv_necesaria and not self._itv_pendiente:
            self._itv_pendiente = True
            self._motivo_itv = motivos
            self._preparar_email_itv(utc_actual, motivos, dias_desde_ultima_itv)
            log_warn("ITV", "ALERTA: {}".format("; ".join(motivos)))

        return itv_necesaria, motivos

    def _evaluar_temperatura(self):
        temps = self._estado["temperaturas_max_semanal"]
        if len(temps) < 14:
            return None
        recientes = [t[1] for t in temps[-7:]]
        anteriores = [t[1] for t in temps[-14:-7]]
        if not recientes or not anteriores:
            return None
        delta = max(recientes) - max(anteriores)
        if delta >= self._umbrales["delta_temp_maxima_c"]:
            return "DEGRADACION_TERMICA: +{:.1f}C".format(delta)
        return None

    def _evaluar_rssi(self):
        alertas = []
        for sat, puntos in self._estado["rssi_por_satelite"].items():
            if len(puntos) < 10:
                continue
            mitad = len(puntos) // 2
            rssi_reciente = sum(p[1] for p in puntos[-mitad:]) / mitad
            rssi_anterior = sum(p[1] for p in puntos[:mitad]) / mitad
            delta = rssi_anterior - rssi_reciente
            if delta >= self._umbrales["delta_rssi_db"]:
                alertas.append("{}: -{:.1f}dB".format(sat, delta))
        return "; ".join(alertas) if alertas else None

    # ------------------------------------------------------------------
    # Email ITV
    # ------------------------------------------------------------------

    def _preparar_email_itv(self, utc_actual, motivos, dias_desde_ultima_itv):
        temps = self._estado["temperaturas_max_semanal"]
        temp_max_7d = max([t[1] for t in temps[-7:]], default=None) if temps else None
        temp_max_30d = max([t[1] for t in temps[-30:]], default=None) if temps else None
        capturas_total = sum(c[1] for c in self._estado["capturas_historico"]) if self._estado["capturas_historico"] else 0

        rssi_resumen = {}
        for sat, puntos in self._estado["rssi_por_satelite"].items():
            if puntos:
                rssi_medio = sum(p[1] for p in puntos) / len(puntos)
                rssi_resumen[sat] = "{:.1f}dBm ({} muestras)".format(rssi_medio, len(puntos))

        email_data = {
            "tipo": "itv",
            "timestamp": utc_actual,
            "dias_desde_ultima_itv": dias_desde_ultima_itv,
            "motivos": motivos,
            "metricas": {
                "dias_acumulados": self._estado["dias_acumulados"],
                "heartbeats_acumulados": self._estado["heartbeats_acumulados"],
                "reinicios_7d": self._estado["reinicios_7d"],
                "reinicios_total": self._estado["reinicios_ultima_semana"],
                "ventilador_activaciones_7d": self._estado["ventilador_activaciones_7d"],
                "temp_max_7d": temp_max_7d,
                "temp_max_30d": temp_max_30d,
                "capturas_total_estimado": capturas_total,
                "capturas_7d": self._estado["capturas_ultimos_7d"],
                "emails_7d": self._estado["emails_enviados_ultimos_7d"],
                "rssi_por_satelite": rssi_resumen,
            },
            "checklist": [
                "Caja estanca: sellos de silicona intactos? condensacion interior?",
                "Antena: firme? oxido en conector SMA? cable coaxial sin dobleces?",
                "PCB: puntos de soldadura verdes (sulfatacion)? insectos/moho?",
                "Ventilador: gira libre? ruido anomalo? obstruido por polen/polvo?",
                "Pre-LNA: conector firme? calor excesivo?",
                "Alimentacion: cable USB/C sin peladuras? conector barrel jack firme?",
                "PSRAM: sigue sin inicializar? (esperado, anotar)",
            ],
            "acciones": [
                "Todo OK -> volver a subir, resetear contador ITV",
                "Problema menor -> reparar, subir, programar ITV en 30 dias",
                "Problema grave -> bajar permanentemente, diagnosticar en mesa",
            ]
        }

        try:
            with open(ITV_EMAIL_FICHERO, "w") as f:
                json.dump(email_data, f)
                f.flush()
                os.sync()
        except Exception as e:
            log_warn("ITV", "No se pudo guardar email ITV: {}".format(e))

    def leer_email_itv_pendiente(self):
        try:
            with open(ITV_EMAIL_FICHERO, "r") as f:
                data = json.load(f)
            try:
                os.remove(ITV_EMAIL_FICHERO)
            except OSError:
                pass
            return data
        except (OSError, ValueError):
            return None

    def hay_email_itv_pendiente(self):
        try:
            os.stat(ITV_EMAIL_FICHERO)
            return True
        except OSError:
            return False

    # ------------------------------------------------------------------
    # Acciones post-ITV
    # ------------------------------------------------------------------

    def marcar_itv_realizada(self, utc_actual, motivo='manual'):
        self._estado["ultima_itv_timestamp"] = utc_actual
        self._estado["ultima_itv_motivo"] = motivo
        self._estado["dias_acumulados"] = 0
        self._estado["heartbeats_acumulados"] = 0
        self._estado["reinicios_7d"] = 0
        self._estado["reinicios_ultima_semana"] = 0
        self._estado["ventilador_activaciones_7d"] = 0
        self._estado["ventilador_activaciones_historico"] = []
        self._estado["temperaturas_max_semanal"] = []
        self._estado["capturas_ultimos_7d"] = 0
        self._estado["capturas_historico"] = []
        self._estado["rssi_por_satelite"] = {}
        self._estado["emails_enviados_ultimos_7d"] = 0
        self._estado["emails_enviados_historico"] = []
        self._estado["_reinicios_previos"] = 0
        self._estado["_capturas_previas"] = 0
        self._estado["_ventilador_estaba_on"] = False
        self._estado["ultimo_timestamp_diario"] = utc_actual
        self._itv_pendiente = False
        self._motivo_itv = []
        self._guardar_estado()
        log_info("ITV", "ITV realizada. Motivo: {}".format(motivo))

    def forzar_itv(self, utc_actual, motivo='forzado_manual'):
        self._itv_pendiente = True
        self._motivo_itv = [motivo]
        self._preparar_email_itv(utc_actual, [motivo], 0)
        # FIX v7.3.1: Crear estado_pendiente.json para forzar transicion a fase4
        try:
            estado_minimo = {
                "tipo": "estado",
                "timestamp": utc_actual,
                "heartbeats": [],
                "capturas_count": 0,
                "capturas": [],
                "temp_cpu": None,
                "ventilador_on": False,
                "fs_libre_kb": None,
                "paquetes_capturados": 0,
                "paquetes_descartados": 0,
                "errores": ""
            }
            with open("estado_pendiente.json", "w") as f:
                json.dump(estado_minimo, f)
                f.flush()
                os.sync()
            from estado import guardar_fase
            guardar_fase(4)
            log_warn("ITV", "ITV forzada: {} -> estado_pendiente + fase4 preparados".format(motivo))
        except Exception as e:
            log_warn("ITV", "ITV forzada: {} (no se pudo preparar fase4: {})".format(motivo, e))

    # ------------------------------------------------------------------
    # Estado para debug / heartbeat
    # ------------------------------------------------------------------

    def resumen_compacto(self):
        """NUEVO v7.3: Muestra dias_acum/dias_max compacto."""
        dias = self._estado["dias_acumulados"]
        dias_max = self._umbrales["dias_maximos"]
        pendiente = "PENDIENTE" if self._itv_pendiente else "OK"
        motivo = self._motivo_itv[0] if self._motivo_itv else '-'
        return "ITV:{} {}/{} {}".format(
            pendiente, dias, dias_max, motivo[:20])

    def info_debug(self):
        dias = self._estado["dias_acumulados"]
        dias_max = self._umbrales["dias_maximos"]
        return {
            "dias_acumulados": dias,
            "dias_maximos": dias_max,
            "dias_restantes": max(0, dias_max - dias),
            "itv_pendiente": self._itv_pendiente,
            "ventilador_7d": self._estado["ventilador_activaciones_7d"],
            "reinicios_7d": self._estado["reinicios_7d"],
            "capturas_7d": self._estado["capturas_ultimos_7d"],
            "emails_7d": self._estado["emails_enviados_ultimos_7d"],
            "temps_registradas": len(self._estado["temperaturas_max_semanal"]),
        }


# =========================================================================
# EJECUCIÓN DIRECTA: Forzar ITV desde consola Thonny
# Uso: import itv_manager
#      # o desde shell: mpremote run itv_manager.py
# =========================================================================

def main():
    """Forzar ITV manualmente desde consola."""
    print("=" * 50)
    print("FORZAR ITV - ITVManager")
    print("=" * 50)

    try:
        from tiempo import obtener_unix_utc_real
        utc = obtener_unix_utc_real()
    except Exception:
        import time
        utc = int(time.time())

    itv = ITVManager()
    print("Estado actual:", itv.resumen_compacto())
    print("Email ITV pendiente:", itv.hay_email_itv_pendiente())
    print("")

    # Forzar ITV
    itv.forzar_itv(utc, "test_manual")

    print("")
    print("ITV forzada correctamente.")
    print("Estado:", itv.resumen_compacto())
    print("Email ITV pendiente:", itv.hay_email_itv_pendiente())
    print("")
    print("La placa enviara el email ITV en el proximo ciclo de fase4.")
    print("Para enviar inmediatamente, reinicia la placa (machine.reset())")
    print("=" * 50)


if __name__ == "__main__":
    main()
