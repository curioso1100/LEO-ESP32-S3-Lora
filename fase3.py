# =========================================================================
# MÓDULO: fase3.py - SUPERVISOR DE ESCUCHA ACTIVA
# =========================================================================


import machine
import time
import json
import gc
import os

import placa
from config_system import guardar_fase, obtener_config
from logger import (
    log_info, log_debug, log_warn, log_error, log_exception,
    rotar_logs_txt
)
from tiempo_satelites import obtener_unix_utc_real, obtener_tiempo_actual

CONFIG = obtener_config()

from doppler_motor import calcular_parametros_satelite


# =========================================================================
# CONSTANTES
# =========================================================================

_SLEEP_PASE_ACTIVO_S = 5
_SLEEP_ESPERA_S = 30


# =========================================================================
# FUNCIONES AUXILIARES LOCALES (específicas del bucle)
# =========================================================================

def _comprobar_prg(radio, itv):
    """Gestiona el botón PRG. Única función: marcar ITV como realizada.

    Patrón: 3 pulsaciones cortas (<1s cada una) dentro de 5 segundos.
    Si se detecta, marca ITV realizada, parpadea LED 5 veces y continúa.
    """
    if not placa.prg_pulsado():
        return

    pulsaciones = 1  # Ya estamos en la primera pulsación
    t_inicio_ventana = time.ticks_ms()

    # Esperar a que suelten el botón de la primera pulsación
    while placa.prg_pulsado():
        time.sleep_ms(50)
    duracion_primera = time.ticks_diff(time.ticks_ms(), t_inicio_ventana)
    if duracion_primera > 1000:
        # Primera pulsación demasiado larga, no cuenta
        return

    while time.ticks_diff(time.ticks_ms(), t_inicio_ventana) < 5000:
        if placa.prg_pulsado():
            t_pulso = time.ticks_ms()
            while placa.prg_pulsado():
                time.sleep_ms(50)
            dur = time.ticks_diff(time.ticks_ms(), t_pulso)
            if dur > 1000:
                # Pulsación larga detectada, resetear conteo
                pulsaciones = 0
                continue
            pulsaciones += 1
            if pulsaciones >= 3:
                log_warn("PRG", "3 pulsaciones cortas detectadas -> marcando ITV realizada")
                itv.marcar_itv_realizada(obtener_unix_utc_real(), "boton_prg_3pulsos")
                placa.led_blink(5, pausa_ms=100)
                return
        time.sleep_ms(50)

    log_debug("PRG", "Solo {} pulsacion(es) corta(s), ignorando".format(pulsaciones))


def _intentar_transicion_fase4(radio, itv=None):
    """Verifica si se debe transicionar a fase4 y reinicia."""
    try:
        with open("estado.json", "r") as f:
            estado = json.load(f)
            if estado.get("fase", 3) == 4:
                log_info("FASE3", "Transicion a fase4 detectada - reiniciando")
                radio.standby()
                os.sync()
                time.sleep_ms(500)
                placa.reiniciar()
            # ITV: si hay email ITV pendiente, también forzar fase4
            if itv is not None and itv.hay_email_itv_pendiente():
                log_warn("ITV", "Email ITV pendiente detectado -> forzando transicion a fase4")
                guardar_fase(4)
                radio.standby()
                os.sync()
                time.sleep_ms(500)
                placa.reiniciar()
    except Exception as e:
        log_warn("FASE3", "Error verificando fase: {}".format(e))


# =========================================================================
# FUNCIÓN PÚBLICA PRINCIPAL
# =========================================================================

def ejecutar():
    placa.led_blink(3)
    placa.led_off()

    # --- Lazy imports con gc.collect() para evitar fragmentación de heap ---
    gc.collect() 
    from config_system import ConfigFase3, EstadoEmail, SweepParametros
    gc.collect()
    from radio_manager import RadioManager
    gc.collect()
    from sat_identifier import IdentificadorSat
    gc.collect()
    from fase3_utils import (
        verificar_agenda_o_reiniciar,
        mostrar_proximos_pases,
        mostrar_estado_pase,
        procesar_recepcion,
        enviar_email_estado,
        escribir_heartbeat_fase3,
        sincronizar_ntp_si_necesario,
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
            ventilador = None

    # NTP
    sincronizar_ntp_si_necesario(cfg)

    # Radio
    params_ini = calcular_parametros_satelite(obtener_unix_utc_real())
    radio = RadioManager()
    radio.inicializar(params_ini)
    log_info("FASE3_INIT", "DOPPLER={} | Freq={:.3f}MHz | SF={} | BW={} | CR={} | SW={} | LNA=0x{:02X} | HB={} | ESTADO={}min".format(
        cfg.doppler_activo, radio.frecuencia, radio.sf, radio.bw, radio.cr,
        radio.sync_word, radio.ganancia, cfg.heartbeat_activo,
        cfg.email_cada_min if cfg.email_cada_seg > 0 else "FIJO"))

    # Sweep e identificación
    sweep = SweepParametros(cfg.sweep_combinaciones, cfg.sweep_intervalo,
                             cfg.sweep_activo_global, cfg.perfiles)
    ident = IdentificadorSat(cfg.perfiles, debug=cfg.debug)

    # Email
    email = EstadoEmail(cfg.horas_fijas, cfg.email_cada_seg)

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

    escribir_heartbeat_fase3(reloj_str, modo_hb, radio, 0, 0,
                              sat_hb, temp, vent_on, fs_libre,
                              heartbeat_activo=cfg.heartbeat_activo)
    if cfg.debug:
        print("[HEARTBEAT] Inicial guardado (IRQ:{})".format(0))

    # --- BUCLE PRINCIPAL ---
    heartbeat_ciclos = 0
    reinicios = 0
    ultimo_satelite_en_cielo = None
    thonny_info_mostrada = False

    # ITV: flags para tracking de eventos por ciclo
    heartbeat_escrito_este_ciclo = False
    email_enviado_este_ciclo = False

    while True:
        # Seguridad RAM
        if gc.mem_free() < cfg.min_ram:
            log_warn("MEM", "RAM baja ({} < {} bytes), reiniciando".format(
                gc.mem_free(), cfg.min_ram))
            radio.standby()
            reinicios += 1
            placa.reiniciar()

        # Botón PRG (solo marcar ITV)
        _comprobar_prg(radio, itv)

        # Tiempo
        utc, reloj_str, t_local = obtener_tiempo_actual()
        if (not thonny_info_mostrada) and ("thonny" in os.listdir("/") or machine.reset_cause() == 5):
            log_info("THONNY", "Detectado arranque desde Thonny")
            thonny_info_mostrada = True

        # Agenda
        verificar_agenda_o_reiniciar(radio, t_local)

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
                if email.toca_enviar(t_local):
                    log_info("EMAIL", "DISPARANDO email de estado (fin de pase)!")
                    print("[EMAIL-DEBUG] DISPARANDO email de estado (fin de pase)!")
                    if enviar_email_estado(email, temp, vent_on, fs_libre,
                                            paquetes_capturados, paquetes_descartados):
                        email_enviado_este_ciclo = True
                        _intentar_transicion_fase4(radio, itv)

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

        # --- Email periódico ---
        if email.toca_enviar(t_local):
            log_info("EMAIL", "DISPARANDO email de estado!")
            if enviar_email_estado(email, temp, vent_on, fs_libre,
                                    paquetes_capturados, paquetes_descartados):
                email_enviado_este_ciclo = True
                _intentar_transicion_fase4(radio, itv)

        # --- Heartbeat ---
        heartbeat_ciclos += 1
        if heartbeat_ciclos >= heartbeat_intervalo:
            heartbeat_ciclos = 0
            sat_hb = sat_obj["satelite"]["nombre"] if sat_obj else "-"
            modo_hb = "PASE" if sat_obj else "BASE"
            irq_delta = radio.irq_count - irq_count_inicial
            escribir_heartbeat_fase3(reloj_str, modo_hb, radio, irq_delta, reinicios,
                                      sat_hb, temp, vent_on, fs_libre,
                                      heartbeat_activo=cfg.heartbeat_activo)
            heartbeat_escrito_este_ciclo = True
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
            heartbeat_enviado=heartbeat_escrito_este_ciclo,
            email_enviado=email_enviado_este_ciclo,
            capturas_count=paquetes_capturados[0],
            utc_actual=utc,
            t_local_tuple=t_local
        )
        itv_necesaria, motivos_itv = itv.evaluar(utc, t_local)
        if itv_necesaria and not itv.hay_email_itv_pendiente():
            guardar_fase(4)
            _intentar_transicion_fase4(radio, itv)

        # ITV: reset flags para siguiente ciclo
        heartbeat_escrito_este_ciclo = False
        email_enviado_este_ciclo = False

        # --- Sleep ---
        sleep_s = _SLEEP_PASE_ACTIVO_S if sat_obj is not None else _SLEEP_ESPERA_S
        for _ in range(sleep_s):
            if placa.prg_pulsado():
                _comprobar_prg(radio, itv)
            time.sleep(1)


if __name__ == "__main__":
    ejecutar()