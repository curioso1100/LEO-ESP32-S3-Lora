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
#    >>> from tiempo_satelites import obtener_unix_utc_real
#    >>> itv = ITVManager()
#    >>> print(itv.resumen_compacto())   # Ej: "ITV:OK 3/90 -"
#    >>> print(itv.info_debug())        # Dict con metricas completas
#
# 3. MARCAR ITV REALIZADA (boton PRG o consola):
#    >>> itv.marcar_itv_realizada(obtener_unix_utc_real(), "revision_ok")
#    # O en fase3: pulsa PRG 1 vez
#
# 4. ITV REMOTA (sin bajar la placa):
#    Crea itv_remota.json en la flash (via Git o Thonny):
#    {
#        "realizar_itv": true,
#        "timestamp": 1234567890,
#        "motivo": "remota_post_reinicio",
#        "notas": "Actualizacion config.json - todo OK"
#    }
#    La placa lo procesara automaticamente al crear ITVManager.
#
# 5. FICHEROS ITV EN FLASH:
#    - itv_estado.json          : Metricas acumuladas (NO borrar)
#    - itv_email_pendiente.json : Email ITV preparado (se borra tras envio)
#    - itv_remota.json          : Orden de ITV remota (se borra tras procesar)
#
# 6. CRITERIOS ITV (configurables en config.json -> "itv"):
#    - dias_maximos: 90 dias sin revision
#    - ventilador_activaciones_7d: 3+ activaciones (polvo/obstruccion)
#    - delta_temp_maxima_c: +5C vs mes anterior (degradacion termica)
#    - delta_rssi_db: -10dB vs historico (problema antena)
#    - dias_sin_capturas: 7 dias sin capturas (antena desconectada)
#
# 7. RESUMEN EN HEARTBEAT:
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
ITV_REMOTA_FICHERO = "itv_remota.json"

DEFAULT_UMBRALES = {
    "dias_maximos": 90,
    "ventilador_activaciones_7d": 3,
    "delta_temp_maxima_c": 5,
    "delta_rssi_db": 10,
    "dias_sin_capturas": 7,
}


# =========================================================================
# CLASE PRINCIPAL
# =========================================================================

class ITVManager:
    # Gestiona la lógica de mantenimiento preventivo de la estación LEO
    def __init__(self, config=None):
        self._cfg = config or {}
        self._umbrales = self._cargar_umbrales()
        self._estado = self._cargar_estado()
        # FIX: cargar flags de persistencia para que sobrevivan a reinicios
        self._itv_pendiente = self._estado.get("itv_pendiente", False)
        self._motivo_itv = self._estado.get("motivo_itv", [])
        self._inicializar_metricas()
        self._procesar_itv_remota()

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
            "ventilador_activaciones_7d": 0,
            "ventilador_activaciones_historico": [],
            "temperaturas_max_semanal": [],
            "capturas_ultimos_7d": 0,
            "capturas_historico": [],
            "rssi_por_satelite": {},
            "ultimo_dia_calculado": 0,
            "ultimo_timestamp_diario": 0,
            "version_estado": 3,
            "heartbeats_acumulados": 0,
            "emails_enviados_ultimos_7d": 0,
            "emails_enviados_historico": [],
            "reinicios_7d": 0,
            "reinicios_ultima_semana": 0,
            "_temp_max_hoy": None,
            "_capturas_previas": 0,
            "_ventilador_estaba_on": False,
            # FIX: persistir flags de alerta entre reinicios
            "itv_pendiente": False,
            "motivo_itv": [],
        }

    def _guardar_estado(self):
        # FIX: sincronizar flags de instancia al estado antes de guardar
        self._estado["itv_pendiente"] = self._itv_pendiente
        self._estado["motivo_itv"] = self._motivo_itv
        try:
            with open(ITV_FICHERO, "w") as f:
                json.dump(self._estado, f)
                f.flush()
                os.sync()
        except Exception as e:
            log_warn("ITV", "No se pudo guardar estado: {}".format(e))

    # ------------------------------------------------------------------
    # ITV remota (sin bajar la placa)
    # ------------------------------------------------------------------

    def _procesar_itv_remota(self):
        """Procesa itv_remota.json si existe. Permite marcar ITV realizada
        remotamente subiendo un archivo via Git sin bajar la placa."""
        try:
            if ITV_REMOTA_FICHERO not in os.listdir():
                return
            with open(ITV_REMOTA_FICHERO, "r") as f:
                data = json.load(f)
            if not data.get("realizar_itv", False):
                return
            timestamp = data.get("timestamp", 0)
            motivo = data.get("motivo", "remota")
            notas = data.get("notas", "")
            if timestamp == 0:
                try:
                    from tiempo_satelites import obtener_unix_utc_real
                    timestamp = obtener_unix_utc_real()
                except Exception:
                    timestamp = int(time.time())
            self.marcar_itv_realizada(timestamp, motivo)
            try:
                os.remove(ITV_REMOTA_FICHERO)
            except OSError:
                pass
            log_info("ITV", "ITV remota procesada: {} | {}".format(motivo, notas))
        except (OSError, ValueError) as e:
            log_debug("ITV", "Error procesando ITV remota: {}".format(e))

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
        Calcula dias_acumulados basado en tiempo transcurrido real,
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
            from tiempo_satelites import obtener_unix_utc_real
            utc_actual = obtener_unix_utc_real()
        except Exception:
            utc_actual = int(time.time())

        try:
            from tiempo_satelites import obtener_tiempo_actual
            _, _, t_local = obtener_tiempo_actual()
            dia_actual = t_local[7]
        except Exception:
            import time
            dia_actual = time.localtime()[7]

        # Calcular dias_acumulados desde el primer heartbeat
        dias_estimados = self._calcular_dias_desde_primer_hb(lineas, utc_actual)
        if dias_estimados < 1:
            hb_count = len([l for l in lineas if l.strip().startswith("HB ")])
            dias_estimados = max(1, hb_count // 96)

        self._estado["dias_acumulados"] = dias_estimados
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
        log_info("ITV", "Reconstruido: ~{} dias, {} CAP (dia={})".format(
            dias_estimados, self._estado['capturas_ultimos_7d'], dia_actual))

    def _calcular_dias_desde_primer_hb(self, lineas, utc_actual):
        # Extrae timestamp del primer heartbeat y calcula días transcurridos
        try:
            for linea in lineas:
                if linea.strip().startswith("HB "):

                    partes = linea.strip().split()
                    if len(partes) >= 2:
                        ts_str = partes[1]
                        from tiempo_satelites import parsear_timestamp
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
                   reinicios, capturas_count,
                   utc_actual, t_local_tuple):
        dia_actual = t_local_tuple[7]

        # Calcular días transcurridos desde último timestamp diario
        dias_transcurridos = self._calcular_dias_transcurridos(utc_actual)

        # FIX: procesar TODOS los días pendientes, no solo 1.
        # Si la placa estuvo apagada varios días, hace falta ejecutar
        # el reset diario por cada día transcurrido para no perderlos.
        if dias_transcurridos >= 1 and self._estado["ultimo_timestamp_diario"] > 0:
            log_info("ITV", "Reset diario pendiente: {} dia(s) calculado(s), procesando todos".format(
                dias_transcurridos))
            for i in range(dias_transcurridos):
                ts_dia = self._estado["ultimo_timestamp_diario"] + (i + 1) * 86400
                import time
                dia_dia = time.localtime(ts_dia)[7]
                self._reset_diario(dia_dia, ts_dia)
        elif self._estado["ultimo_timestamp_diario"] == 0:
            log_debug("ITV", "Inicializando ultimo_timestamp_diario = {}".format(utc_actual))
            self._estado['ultimo_timestamp_diario'] = utc_actual
            self._estado['ultimo_dia_calculado'] = dia_actual

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
            self._estado["_reinicios_previos"] = reinicios

        capturas_prev = self._estado.get('_capturas_previas', 0)
        if capturas_count < capturas_prev:
            # Reinicio detectado: contador reseteado
            self._estado["capturas_ultimos_7d"] += capturas_count
        elif capturas_count > capturas_prev:
            self._estado["capturas_ultimos_7d"] += (capturas_count - capturas_prev)
        self._estado["_capturas_previas"] = capturas_count

        if self._estado["heartbeats_acumulados"] % 10 == 0:
            self._guardar_estado()

    def _calcular_dias_transcurridos(self, utc_actual):
        # Calcula días reales transcurridos desde último reset diario
        ultimo_ts = self._estado.get("ultimo_timestamp_diario", 0)
        if ultimo_ts == 0:
            return 0
        segundos = utc_actual - ultimo_ts
        dias = segundos // 86400
        if dias > 30:
            log_warn("ITV", "dias_transcurridos anormalmente alto: {} (utc={}, ultimo_ts={})".format(
                dias, utc_actual, ultimo_ts))
        return dias

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
        self._estado["dias_acumulados"] += 1

        self._limpiar_historico_antiguo(utc_actual)
        self._estado["ultimo_dia_calculado"] = dia_actual
        self._estado["ultimo_timestamp_diario"] = utc_actual
        self._guardar_estado()

        log_info("ITV", "Reset diario: dia={}, dias_acum={}, temp_max_hoy={}".format(
            dia_actual, self._estado['dias_acumulados'], temp_max_hoy))

    def _limpiar_historico_antiguo(self, utc_actual):
        limite = utc_actual - (7 * 86400)
        # FIX: limpiar tambien capturas_historico, no solo ventilador
        for clave in ["ventilador_activaciones_historico", "capturas_historico"]:
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

        # FIX: calcular capturas de 7d correctamente sumando historial + actual
        capturas_7d = self._calcular_capturas_7d(utc_actual)
        if capturas_7d == 0 and dias_acum > self._umbrales["dias_sin_capturas"]:
            try:
                with open("agenda.json", "r") as f:
                    if len(json.load(f).get("pases", [])) > 0:
                        motivos.append("SIN_CAPTURAS: 0 en 7d")
            except (OSError, ValueError):
                pass

        itv_necesaria = len(motivos) > 0
        # FIX: self._itv_pendiente ahora se carga del estado, sobrevive a reinicios
        if itv_necesaria and not self._itv_pendiente:
            self._itv_pendiente = True
            self._motivo_itv = motivos
            self._estado["itv_pendiente"] = True
            self._estado["motivo_itv"] = motivos
            self._preparar_email_itv(utc_actual, motivos, dias_desde_ultima_itv)
            log_warn("ITV", "ALERTA: {}".format("; ".join(motivos)))
            self._guardar_estado()  # persistir inmediatamente

        return itv_necesaria, motivos

    # FIX: nuevo metodo para calcular capturas reales de 7 dias
    def _calcular_capturas_7d(self, utc_actual):
        """Suma capturas del dia en curso + historial de los ultimos 7 dias."""
        total = self._estado.get("capturas_ultimos_7d", 0)
        limite = utc_actual - (7 * 86400)
        for entry in self._estado.get("capturas_historico", []):
            if isinstance(entry, list) and len(entry) >= 2:
                ts = entry[0]
                if ts > limite:
                    total += entry[1]
        return total

    def _evaluar_temperatura(self):
        # Leer reinicios totales del contador persistente del sistema
        try:
            from config_system import leer_reinicios
            reinicios_totales = leer_reinicios()
        except Exception:
            reinicios_totales = 0

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
        # Leer reinicios totales del contador persistente del sistema
        try:
            from config_system import leer_reinicios
            reinicios_totales = leer_reinicios()
        except Exception:
            reinicios_totales = 0

        temps = self._estado["temperaturas_max_semanal"]

        # fallback a _temp_max_hoy si no hay historial semanal aun
        temp_max_7d = None
        if temps:
            temp_max_7d = max([t[1] for t in temps[-7:]], default=None)
        if temp_max_7d is None:
            temp_max_7d = self._estado.get("_temp_max_hoy", None)

        temp_max_30d = None
        if temps:
            temp_max_30d = max([t[1] for t in temps[-30:]], default=None)

        # Formatear temperaturas; evitar mostrar 'None C'
        def _fmt_temp(t):
            if t is None:
                return "N/D (sin datos)"
            try:
                return "{:.1f}C".format(t)
            except Exception:
                return str(t)

        temp_max_7d_str = _fmt_temp(temp_max_7d)
        temp_max_30d_str = _fmt_temp(temp_max_30d)

        # FIX: incluir capturas del dia actual en el total estimado
        capturas_total = self._estado.get("capturas_ultimos_7d", 0)
        if self._estado.get("capturas_historico"):
            capturas_total += sum(c[1] for c in self._estado["capturas_historico"])

        # FIX: usar calculo real de 7 dias en lugar de solo el dia en curso
        capturas_7d = self._calcular_capturas_7d(utc_actual)

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
                "reinicios_total": reinicios_totales,
                "ventilador_activaciones_7d": self._estado["ventilador_activaciones_7d"],
                "temp_max_7d": temp_max_7d_str,
                "temp_max_30d": temp_max_30d_str,
                "capturas_total_estimado": capturas_total,
                "capturas_7d": capturas_7d,
                "rssi_por_satelite": rssi_resumen,
            },

            "checklist": [
                "Caja estanca (sellos, condensacion)",
                "Antena (firme, oxido SMA, cable)",
                "PCB (sulfatacion, insectos, moho)",
                "Ventilador (gira libre, polvo)",
                "Pre-LNA (conector, calor)",
                "Alimentacion (cable, conector)",
                "PSRAM (sin inicializar - esperado)",
            ],
            "acciones": [
                "OK -> subir, resetear ITV (pulsa PRG en fase3)",
                "Menor -> reparar, subir, ITV en 30 dias",
                "Grave -> bajar, diagnosticar en mesa",
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
        # Lee y BORRA el email ITV pendiente. Cuidado: si falla el envío se pierde
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

    def obtener_email_itv_pendiente(self):
        # Lee el email ITV pendiente SIN borrarlo. Usar en fase2 para envío seguro
        try:
            with open(ITV_EMAIL_FICHERO, "r") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def borrar_email_itv_pendiente(self):
        # Borra el archivo de email ITV pendiente tras envío exitoso
        try:
            os.remove(ITV_EMAIL_FICHERO)
            log_debug("ITV", "Email ITV pendiente borrado tras envío exitoso")
        except OSError:
            pass

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
        self._estado["ventilador_activaciones_7d"] = 0
        self._estado["ventilador_activaciones_historico"] = []
        self._estado["temperaturas_max_semanal"] = []
        self._estado["capturas_ultimos_7d"] = 0
        self._estado["capturas_historico"] = []
        self._estado["rssi_por_satelite"] = {}
        self._estado["_reinicios_previos"] = 0
        self._estado["_capturas_previas"] = 0
        self._estado["_ventilador_estaba_on"] = False
        self._estado["ultimo_timestamp_diario"] = utc_actual
        self._itv_pendiente = False
        self._motivo_itv = []
        self._estado["itv_pendiente"] = False
        self._estado["motivo_itv"] = []
        # borrar email ITV pendiente si existe
        try:
            if ITV_EMAIL_FICHERO in os.listdir():
                os.remove(ITV_EMAIL_FICHERO)
                log_debug("ITV", "Email ITV pendiente borrado tras marcar ITV realizada")
        except OSError:
            pass
        self._guardar_estado()
        log_info("ITV", "ITV realizada. Motivo: {}".format(motivo))

    def forzar_itv(self, utc_actual, motivo='forzado_manual'):
        # Prepara email ITV pendiente. NO orquesta transición de fase
        self._itv_pendiente = True
        self._motivo_itv = [motivo]
        self._estado["itv_pendiente"] = True
        self._estado["motivo_itv"] = [motivo]
        self._preparar_email_itv(utc_actual, [motivo], 0)
        log_warn("ITV", "ITV forzada: {} -> email ITV preparado".format(motivo))
        self._guardar_estado()

    # ------------------------------------------------------------------
    # Estado para debug / heartbeat
    # ------------------------------------------------------------------

    def resumen_compacto(self):
        # Muestra dias_acum/dias_max compacto
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
            "capturas_7d": self._estado["capturas_ultimos_7d"],
            "temps_registradas": len(self._estado["temperaturas_max_semanal"]),
        }


# =========================================================================
# EJECUCIÓN DIRECTA: Forzar ITV desde consola Thonny
# Uso: import itv_manager
#      # o desde shell: mpremote run itv_manager.py
# =========================================================================

def main():
    # Forzar ITV manualmente desde consola
    print("=" * 50)
    print("FORZAR ITV - ITVManager")
    print("=" * 50)

    try:
        from tiempo_satelites import obtener_unix_utc_real
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
    print("Para que la placa envie el email ITV:")
    print("  1. Desde fase3: espera a que evaluar() detecte la ITV,")
    print("     o pulsa PRG 1 vez para marcarla realizada.")
    print("  2. Desde consola: ejecuta:")
    print("     >>> from config_system import guardar_fase")
    print("     >>> guardar_fase(2)")
    print("     >>> import machine; machine.reset()")
    print("=" * 50)


if __name__ == "__main__":
    main()
