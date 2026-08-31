# ==========================================================================
# MÓDULO: fase3.py - SUPERVISOR DE ESCUCHA ACTIVA
# ==========================================================================

import machine
import time
import json
import gc
import os

import placa
from config_system import guardar_fase, obtener_config, leer_reinicios, incrementar_reinicios, limpiar_backups_residuales, set_estado_enviado, get_horas_reinicio
from logger import (
    log_info, log_debug, log_warn, log_error, log_exception,
    rotar_logs_txt, escribir_heartbeat, log_persistente
)
from tiempo_satelites import obtener_unix_utc_real, obtener_tiempo_actual

CONFIG = obtener_config()

from doppler_motor import calcular_parametros_satelite


# =========================================================================
# CONSTANTES
# =========================================================================

_SLEEP_PASE_ACTIVO_S = 5
_SLEEP_ESPERA_S = 30

_ESTADO_PENDIENTE_FILE = "estado_pendiente.json"

# =========================================================================
# FUNCIONES AUXILIARES LOCALES (específicas del bucle)
# =========================================================================

def _estado_pendiente_existe():
    # Devuelve True si ya hay un estado pendiente guardado
    try:
        os.stat(_ESTADO_PENDIENTE_FILE)
        return True
    except OSError:
        return False


def _comprobar_prg(radio, itv):
    # Gestiona el botón PRG. Única función: marcar ITV como realizada
    if placa.detectar_pulsacion_prg():
        log_warn("PRG", "Pulsacion PRG detectada -> marcando ITV realizada")
        itv.marcar_itv_realizada(obtener_unix_utc_real(), "Boton PRG pulsado")
        placa.led_blink(5, pausa_ms=100)


def _intentar_transicion_fase4(radio):
    # Completa la transicion a fase4 reiniciando SIEMPRE.
    # El llamador ya debe haber guardado fase=4 si es necesario.
    # No verificamos estado.json para evitar fallos por buffer de flash no sincronizado.
    try:
        os.sync()
        log_info("FASE3", "Transicion a fase4 - reiniciando")
        radio.standby()
        time.sleep_ms(500)
        placa.reiniciar()
    except Exception as e:
        log_warn("FASE3", "Error en transicion a fase4: {}".format(e))
        log_persistente("FASE3", "Error en transicion a fase4: {}".format(e), "WARN")


# =========================================================================
# REINICIO PROGRAMADO (horas_de_reinicio en config.json)
# =========================================================================

_REINICIO_PROG_FLAG = "reinicio_prog.flag"


def _comprobar_reinicio_programado(radio, t_local, estado_actual, hay_estado_pendiente):
    fecha_actual = "{:04d}-{:02d}-{:02d}".format(t_local[0], t_local[1], t_local[2])
    hora_actual = "{:02d}:{:02d}".format(t_local[3], t_local[4])

    # 1. ¿Hay un reinicio pendiente de un pase anterior?
    if estado_actual == "BASE" and not hay_estado_pendiente:
        try:
            with open("reinicio_prog.pendiente", "r") as f:
                pendiente = f.read().strip()
            if pendiente:
                hora_pend = pendiente.split(" ")[1]
                log_info("REINICIO_PROG", "Recuperando reinicio pendiente de las {}".format(hora_pend))
                log_persistente("REINICIO_PROG", "Recuperando reinicio pendiente de las {}".format(hora_pend), "INFO")
                radio.standby()
                time.sleep_ms(500)
                os.sync()
                try:
                    os.remove("reinicio_prog.pendiente")
                except Exception:
                    pass
                try:
                    with open("reinicio_prog.flag", "w") as f:
                        f.write(pendiente)
                        f.flush()
                        os.sync()
                except Exception:
                    pass
                guardar_fase(1)
                time.sleep_ms(200)
                placa.reiniciar()
        except Exception:
            pass

    # 2. ¿Es hora de reinicio ahora?
    horas = get_horas_reinicio()
    if not horas:
        return
    if hora_actual not in horas:
        return
    clave = fecha_actual + " " + hora_actual
    try:
        with open("reinicio_prog.flag", "r") as f:
            if f.read().strip() == clave:
                return
    except Exception:
        pass

    # 3. ¿Podemos reiniciar ahora?
    if estado_actual == "BASE" and not hay_estado_pendiente:
        log_info("REINICIO_PROG", "Hora programada {} alcanzada. Reiniciando...".format(hora_actual))
        log_persistente("REINICIO_PROG", "Reinicio programado a las {}".format(hora_actual), "INFO")
        radio.standby()
        time.sleep_ms(500)
        os.sync()
        try:
            with open("reinicio_prog.flag", "w") as f:
                f.write(clave)
                f.flush()
                os.sync()
        except Exception:
            pass
        guardar_fase(1)
        time.sleep_ms(200)
        placa.reiniciar()
    else:
        # Estamos ocupados (PASE o estado_pendiente). Marcar como pendiente.
        try:
            with open("reinicio_prog.pendiente", "w") as f:
                f.write(clave)
                f.flush()
                os.sync()
            log_info("REINICIO_PROG", "Hora {} alcanzada pero ocupado ({}). Pendiente.".format(hora_actual, estado_actual))
        except Exception:
            pass


# =========================================================================
# FUNCIÓN PÚBLICA PRINCIPAL
# =========================================================================

def ejecutar():
    placa.led_blink(3)
    placa.led_off()
    limpiar_backups_residuales()

    # --- Lazy imports con gc.collect() para evitar fragmentación de heap ---
    gc.collect() 
    from config_system import ConfigFase3, EstadoEmail, SweepParametros
    gc.collect()
    from radio_manager import RadioManager
    gc.collect()
    from sat_identifier import IdentificadorSat
    gc.collect()
    from fase3_utils import (
        agenda_caducada,
        mostrar_proximos_pases,
        mostrar_estado_pase,
        procesar_recepcion,
        preparar_estado_pendiente,
        ntp_requiere_sync,
    )
    gc.collect()
    from itv_manager import ITVManager
    gc.collect()

    # --- Inicialización ---
    cfg = ConfigFase3(CONFIG)

    # ITV
    itv = ITVManager(CONFIG)
    log_info("ITV_INIT", itv.resumen_compacto())

    # Ventilador
    ventilador = None
    if cfg.ventilador_activo:
        ventilador = placa.Ventilador(cfg.ventilador_gpio, cfg.ventilador_on, cfg.ventilador_off)
        if ventilador.inicializar():
            log_info("VENT", "Ventilador inicializado en GPIO{}".format(cfg.ventilador_gpio))
        else:
            log_warn("VENT", "No se pudo inicializar ventilador GPIO{}".format(cfg.ventilador_gpio))
            log_persistente("VENT", "No se pudo inicializar ventilador GPIO{}".format(cfg.ventilador_gpio), "WARN")
            ventilador = None
    # NTP
    if ntp_requiere_sync():
        log_warn("RTC", "RTC corrupto - transicionando a fase1 para sincronizar")
        log_persistente("RTC", "RTC corrupto - transicionando a fase1 para sincronizar", "WARN")
        guardar_fase(1)
        time.sleep_ms(500)
        incrementar_reinicios()
        placa.reiniciar()

    # Radio
    params_ini = calcular_parametros_satelite(obtener_unix_utc_real())
    radio = RadioManager()
    radio.inicializar(params_ini)
    # Log simplificado (sin email_cada_min)
    log_info("FASE3_INIT", "DOPPLER={} | Freq={:.3f}MHz | SF={} | BW={} | CR={} | SW={} | LNA=0x{:02X} | HB={} | EMAIL=FIJO".format(
        cfg.doppler_activo, radio.frecuencia, radio.sf, radio.bw, radio.cr,
        radio.sync_word, radio.ganancia, cfg.heartbeat_activo))

    # Sweep e identificación
    sweep = SweepParametros(cfg.sweep_combinaciones, cfg.sweep_intervalo,
                             cfg.sweep_activo_global, cfg.perfiles)
    ident = IdentificadorSat(cfg.perfiles, debug=cfg.debug)

    # Email solo con horas fijas (eliminado timer periodico)
    email = EstadoEmail(cfg.horas_fijas)

    # Contadores (listas de 1 elemento para mutabilidad en funciones)
    paquetes_capturados = [0]
    paquetes_descartados = [0]

    # --- Estado inicial ---
    utc, reloj_str, t_local = obtener_tiempo_actual()
    params = calcular_parametros_satelite(utc)
    sat_obj = params.get("sat_objeto")

    if sat_obj is None:
        print(">>> MODO BASE (inicio) <<<")
        mostrar_proximos_pases(utc, reloj_str)
        ultimo_estado = "BASE"
        heartbeat_intervalo = cfg.heartbeat_base_min * 2
    else:
        print(">>> INICIO DE PASE (arranque durante pase) <<<")
        mostrar_proximos_pases(utc, reloj_str)
        ultimo_estado = "PASE"
        heartbeat_intervalo = cfg.heartbeat_pase_min * 12

    # Heartbeat inicial
    temp = placa.leer_temperatura_cpu()
    vent_on = ventilador.controlar(temp) if ventilador else False
    fs_libre, _ = placa.leer_espacio_filesystem()
    sat_hb = sat_obj["satelite"]["nombre"] if sat_obj else "-"
    modo_hb = "PASE" if sat_obj else "BASE"
    irq_count_inicial = radio.irq_count
    escribir_heartbeat(
        "heartbeat.log", reloj_str, modo_hb, radio.frecuencia, radio.sf,
        radio.bw, radio.cr, radio.sync_word, radio.crc_on, radio.rx_iq,
        radio.ganancia, gc.mem_free(), 0, 0,
        sat_hb, temp, vent_on, fs_libre,
        heartbeat_activo=cfg.heartbeat_activo,
            elevacion=sat_obj["satelite"]["max_elevacion"] if sat_obj else None
    )
    if cfg.debug:
        print("[HEARTBEAT] Inicial guardado (IRQ:{})".format(0))

    # --- BUCLE PRINCIPAL ---
    heartbeat_ciclos = 0
    reinicios = leer_reinicios()
    ultimo_satelite_en_cielo = None
    thonny_info_mostrada = False


    while True:
        # Seguridad RAM
        if gc.mem_free() < cfg.min_ram:
            log_warn("MEM", "RAM baja ({} < {} bytes), reiniciando".format(
                gc.mem_free(), cfg.min_ram))
            log_persistente("MEM", "RAM baja ({} < {} bytes), reiniciando".format(
                gc.mem_free(), cfg.min_ram), "WARN")
            radio.standby()
            reinicios += 1
            incrementar_reinicios()
            placa.reiniciar()

        # Botón PRG (solo marcar ITV)
        _comprobar_prg(radio, itv)

        # Tiempo
        utc, reloj_str, t_local = obtener_tiempo_actual()
        if (not thonny_info_mostrada) and ("thonny" in os.listdir("/") or machine.reset_cause() == 5):
            log_info("THONNY", "Detectado arranque desde Thonny")
            thonny_info_mostrada = True

        # Agenda
        if agenda_caducada(t_local):
            guardar_fase(1)
            radio.standby()
            time.sleep_ms(500)
            incrementar_reinicios()
            placa.reiniciar()

        # Temperatura + ventilador
        temp = placa.leer_temperatura_cpu()
        vent_on = ventilador.controlar(temp) if ventilador else False
        fs_libre, _ = placa.leer_espacio_filesystem()

        # Parámetros satélite
        params = calcular_parametros_satelite(utc)
        params["utc_unix"] = utc
        sat_obj = params.get("sat_objeto")

        # Doppler desactivado -> forzar nominal
        if not cfg.doppler_activo and sat_obj is not None:
            params["freq_obj"] = float(sat_obj["lora"]["frecuencia_hz"]) / 1000000.0

        # Sweep
        sweep_cfg, cab_imp, pay_len, crc_on, rx_iq, sync_word = sweep.calcular(sat_obj, utc)

        # Reconfigurar radio
        radio.reconfigurar(params, {
            "cab_imp": cab_imp, "pay_len": pay_len,
            "crc_on": crc_on, "rx_iq": rx_iq, "sync_word": sync_word
        })

        # --- Transición PASE/BASE ---
        estado_actual = "PASE" if sat_obj is not None else "BASE"

        if estado_actual != ultimo_estado:
            if estado_actual == "PASE":
                print(">>> INICIO DE PASE <<<")
                mostrar_proximos_pases(utc, reloj_str)
                mostrar_estado_pase(sat_obj, params, sweep_cfg, cfg.doppler_activo)
                heartbeat_intervalo = cfg.heartbeat_pase_min * 12
                heartbeat_ciclos = 0
            else:
                print(">>> FIN DE PASE - MODO BASE <<<")

                # solo transicionar si realmente hay estado que enviar
                if email.toca_enviar(t_local):
                    log_info("EMAIL", "DISPARANDO email de estado (fin de pase)!")
                    print("[EMAIL-DEBUG] DISPARANDO email de estado (fin de pase)!")

                    hay_estado = False
                    if not _estado_pendiente_existe():
                        if preparar_estado_pendiente(temp, vent_on, fs_libre, paquetes_capturados, paquetes_descartados, max_hb_lineas=cfg.max_hb_acumulados) is not None:
                            hay_estado = True
                    else:
                        log_debug("EMAIL", "Estado pendiente ya existe, saltando preparacion")
                        hay_estado = True

                    if hay_estado:
                        # resetear contadores tras preparar estado
                        paquetes_capturados[0] = 0
                        paquetes_descartados[0] = 0
                        set_estado_enviado(False)
                        guardar_fase(4)
                        _intentar_transicion_fase4(radio)

                mostrar_proximos_pases(utc, reloj_str)
                heartbeat_intervalo = cfg.heartbeat_base_min * 2
                heartbeat_ciclos = 0
                sweep.reset()

            ultimo_estado = estado_actual

        elif sat_obj is not None and cfg.debug:
            transcurrido = max(0, params["utc_unix"] - sat_obj["tiempo"]["utc_ini_timestamp"])
            if transcurrido % 15 < _SLEEP_PASE_ACTIVO_S:
                mostrar_estado_pase(sat_obj, params, sweep_cfg, cfg.doppler_activo)

        # Resetear sweep al cambiar de satélite
        if sat_obj is None and ultimo_satelite_en_cielo is not None:
            sweep.reset()
            ultimo_satelite_en_cielo = None
        if sat_obj is not None:
            ultimo_satelite_en_cielo = sat_obj["satelite"]["nombre"]

        # --- Debug info ---
        if cfg.debug:
            email_info = email.info_str(t_local)
            temp_str = "{:.1f}C".format(temp) if temp is not None else "N/A"
            vent_str = "ON" if vent_on else "OFF"
            fs_str = "{:.0f}KB".format(fs_libre) if fs_libre is not None else "N/A"
            itv_info = itv.resumen_compacto()

            if sat_obj:
                trans = max(0, utc - sat_obj["tiempo"]["utc_ini_timestamp"])
                print("[RX] {} {:3.0f}s | {:.3f}MHz SF{} BW{} CR{} SW{} CRC{} IQ{} LNA=0x{:02X} | RAM:{} | TEMP:{} VENT:{} FS:{} | {} | {}".format(
                    reloj_str, trans,
                    radio.frecuencia, radio.sf, radio.bw, radio.cr, radio.sync_word,
                    "Y" if radio.crc_on else "N", "Y" if radio.rx_iq else "N",
                    radio.ganancia, gc.mem_free(),
                    temp_str, vent_str, fs_str, email_info, itv_info))
            else:
                print("[RX] {} BASE  | {:.3f}MHz SF{} BW{} CR{} SW{} CRC{} IQ{} LNA=0x{:02X} | RAM:{} | TEMP:{} VENT:{} FS:{} | {} | {}".format(
                    reloj_str,
                    radio.frecuencia, radio.sf, radio.bw, radio.cr, radio.sync_word,
                    "Y" if radio.crc_on else "N", "Y" if radio.rx_iq else "N",
                    radio.ganancia, gc.mem_free(),
                    temp_str, vent_str, fs_str, email_info, itv_info))

        # --- Email periódico (horas fijas) --- eliminado timer periodico, solo horas fijas
        if email.toca_enviar(t_local):
            log_info("EMAIL", "DISPARANDO email de estado!")
            print("[EMAIL-DEBUG] DISPARANDO email de estado!")

            hay_estado = False
            if not _estado_pendiente_existe():
                if preparar_estado_pendiente(temp, vent_on, fs_libre, paquetes_capturados, paquetes_descartados, max_hb_lineas=cfg.max_hb_acumulados) is not None:
                    email_enviado_este_ciclo = True
                    hay_estado = True
            else:
                log_debug("EMAIL", "Estado pendiente ya existe, saltando preparacion")
                hay_estado = True

            if hay_estado:
                # resetear contadores tras preparar estado
                paquetes_capturados[0] = 0
                paquetes_descartados[0] = 0
                set_estado_enviado(False)
                guardar_fase(4)
                _intentar_transicion_fase4(radio)

        # --- Heartbeat ---
        heartbeat_ciclos += 1
        if heartbeat_ciclos >= heartbeat_intervalo:
            heartbeat_ciclos = 0
            sat_hb = sat_obj["satelite"]["nombre"] if sat_obj else "-"
            modo_hb = "PASE" if sat_obj else "BASE"
            irq_delta = radio.irq_count - irq_count_inicial

            escribir_heartbeat(
                "heartbeat.log", reloj_str, modo_hb, radio.frecuencia, radio.sf,
                radio.bw, radio.cr, radio.sync_word, radio.crc_on, radio.rx_iq,
                radio.ganancia, gc.mem_free(), irq_delta, reinicios,
                sat_hb, temp, vent_on, fs_libre,
                heartbeat_activo=cfg.heartbeat_activo,
                elevacion=sat_obj["satelite"]["max_elevacion"] if sat_obj else None
            )

            if cfg.debug:
                print("[HEARTBEAT] Guardado en heartbeat.log (IRQ:{})".format(irq_delta))
            irq_count_inicial = radio.irq_count

        # --- Recepción ---
        sweep.locked = procesar_recepcion(
            radio, sat_obj, sweep, ident,
            paquetes_capturados, paquetes_descartados, debug=cfg.debug)

        # --- ITV: actualizar métricas y evaluar ---
        itv.actualizar(
            temp_cpu=temp,
            ventilador_on=vent_on,
            rssi_satelite=None,
            sat_nombre=None,
            reinicios=reinicios,
            capturas_count=paquetes_capturados[0],
            utc_actual=utc,
            t_local_tuple=t_local
        )
        # No transiciona a fase4 por ITV. El email ITV se envía
        # desde fase2 una vez al día. Aquí solo se prepara el archivo pendiente.
        itv_necesaria, motivos_itv = itv.evaluar(utc, t_local)
        if itv_necesaria:
            msg_itv = "ALERTA ITV detectada: {}. Email preparado para fase2.".format(
                "; ".join(motivos_itv))
            log_warn("ITV", msg_itv)
            log_persistente("ITV", msg_itv, "WARN")


        # --- Comprobar reinicio programado (una vez por ciclo, fuera del sleep) ---
        hay_estado_pend = _estado_pendiente_existe()
        _, _, t_local_reinicio = obtener_tiempo_actual()
        _comprobar_reinicio_programado(radio, t_local_reinicio, estado_actual, hay_estado_pend)

        # --- Sleep ---
        sleep_s = _SLEEP_PASE_ACTIVO_S if sat_obj is not None else _SLEEP_ESPERA_S
        for _ in range(sleep_s):
            _comprobar_prg(radio, itv)
            time.sleep(1)

