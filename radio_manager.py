# =========================================================================
# MÓDULO: radio_manager.py - Manager completo del radio SX1262 V7.2
# =========================================================================
# FUSIÓN A.1: RadioBase integrado en sx1262.py (antes en radio_base.py)
# Fecha refactor: 2026-07-23
# =========================================================================


import gc
import time

from sx1262 import RadioBase
from logger import log_info, log_warn, log_persistente, log_rx_diag, log_error


class RadioManager:
    """Manager completo del radio SX1262."""

    _REG_LNA_GAIN = 0x08AC
    _TX_POWER = 14
    _MAX_PAY_LEN = 255

    def __init__(self):
        self._sx = None
        self._estado = RadioBase()
        self._irq_count = 0
        self._paquetes_en_cola = 0
        self._ultimo_evento_irq = 0
        self._rx_continuo_activo = False
        self._ganancia_actual = 0x96

    def _on_rx_irq(self, events):
        self._ultimo_evento_irq = events
        if events & 0x0002:  # RX_DONE
            self._paquetes_en_cola += 1
            self._irq_count += 1

    def inicializar(self, params):
        """params: dict de doppler_motor.calcular_parametros_satelite()"""
        # Lazy import de placa con gc.collect() para evitar fragmentación
        gc.collect()
        from placa import crear_radio
        gc.collect()
        self._sx = crear_radio()
        gc.collect()
        self._aplicar_config_completa(params)
        self._iniciar_rx_continuo()
        log_info("FASE3", "Supervisor de escucha activa iniciado")
        gc.collect()
        return self._sx

    def _aplicar_config_completa(self, params):
        crc = bool(params.get("crc_on", False))
        rx_iq = bool(params.get("rx_iq", False))
        self._sx.begin(
            freq=params["freq_obj"], bw=params["bw_obj"], sf=params["sf_obj"],
            cr=params["cr_obj"], syncWord=params["sw_obj"],
            power=self._TX_POWER, preambleLength=params["preamble_obj"],
            implicit=False, implicitLen=self._MAX_PAY_LEN,
            crcOn=crc, txIq=False, rxIq=rx_iq
        )
        self._estado.crc_actual = crc
        self._estado.rx_iq_actual = rx_iq
        self._estado.frecuencia_actual = params["freq_obj"]
        self._estado.sf_actual = params["sf_obj"]
        self._estado.bw_actual = params["bw_obj"]
        self._estado.cr_actual = params["cr_obj"]
        self._estado.syncword_actual = params["sw_obj"]
        self._estado.preamble_actual = params["preamble_obj"]
        self._ganancia_actual = params["ganancia_obj"]
        self._sx.writeRegister(self._REG_LNA_GAIN, [params["ganancia_obj"]], 1)

    def _iniciar_rx_continuo(self):
        try:
            self._sx.setBlockingCallback(False, self._on_rx_irq)
            if not self._rx_continuo_activo:
                log_info("RX", "Modo RX continuo (non-blocking) activado")
                self._rx_continuo_activo = True
        except Exception as e:
            log_error("RX", "Fallo al activar RX continuo: {}".format(e))
            self._rx_continuo_activo = False

    def reconfigurar(self, params, sweep_params):
        """sweep_params: dict con cab_imp, pay_len, crc_on, rx_iq, sync_word"""
        cab_imp = sweep_params.get("cab_imp", False)
        pay_len = sweep_params.get("pay_len", self._MAX_PAY_LEN)
        crc_on = sweep_params.get("crc_on", False)
        rx_iq = sweep_params.get("rx_iq", False)
        sync_word = sweep_params.get("sync_word", self._estado.syncword_actual or 18)

        reconfig_completa = (
            self._estado.sf_actual != params["sf_obj"] or
            self._estado.cr_actual != params["cr_obj"] or
            self._estado.bw_actual != params["bw_obj"] or
            self._estado.preamble_actual != params["preamble_obj"] or
            self._estado.crc_actual != crc_on or
            self._estado.rx_iq_actual != rx_iq or
            self._estado.syncword_actual != sync_word or
            getattr(self._sx, 'implicit_actual', False) != cab_imp or
            getattr(self._sx, 'pay_len_actual', self._MAX_PAY_LEN) != pay_len
        )

        try:
            if reconfig_completa:
                self._sx.begin(
                    freq=params["freq_obj"], bw=params["bw_obj"], sf=params["sf_obj"],
                    cr=params["cr_obj"], syncWord=sync_word, power=self._TX_POWER,
                    preambleLength=params["preamble_obj"], implicit=cab_imp,
                    implicitLen=pay_len, crcOn=crc_on, txIq=False, rxIq=rx_iq
                )
                self._estado.crc_actual = crc_on
                self._estado.rx_iq_actual = rx_iq
                self._estado.sf_actual = params["sf_obj"]
                self._estado.cr_actual = params["cr_obj"]
                self._estado.bw_actual = params["bw_obj"]
                self._estado.preamble_actual = params["preamble_obj"]
                self._estado.syncword_actual = sync_word
                self._estado.frecuencia_actual = params["freq_obj"]
                self._sx.implicit_actual = cab_imp
                self._sx.pay_len_actual = pay_len
                self._sx.writeRegister(self._REG_LNA_GAIN, [params["ganancia_obj"]], 1)
                self._ganancia_actual = params["ganancia_obj"]
                self._iniciar_rx_continuo()
                return True
            else:
                hubo_cambio = False
                if self._estado.frecuencia_actual != params["freq_obj"]:
                    self._sx.setFrequency(params["freq_obj"])
                    self._estado.frecuencia_actual = params["freq_obj"]
                    hubo_cambio = True
                if self._estado.syncword_actual != sync_word:
                    self._sx.setSyncWord(sync_word)
                    self._estado.syncword_actual = sync_word
                    hubo_cambio = True
                if self._ganancia_actual != params["ganancia_obj"]:
                    self._sx.writeRegister(self._REG_LNA_GAIN, [params["ganancia_obj"]], 1)
                    self._ganancia_actual = params["ganancia_obj"]
                    hubo_cambio = True
                if self._estado.rx_iq_actual != rx_iq:
                    try:
                        self._sx.setRxIq(rx_iq)
                    except Exception as e:
                        log_persistente("RADIO", "setRxIq fallo ({}), forzando reinicio RX".format(e), "WARN")
                        self._iniciar_rx_continuo()
                    self._estado.rx_iq_actual = rx_iq
                    hubo_cambio = True
                if hubo_cambio:
                    self._iniciar_rx_continuo()
                return hubo_cambio
        except Exception as e:
            log_persistente("RADIO", "Reconfiguracion fallida: {} - intentando recuperar".format(e), "ERROR")
            try:
                self._sx.standby()
                time.sleep_ms(100)
                self._iniciar_rx_continuo()
            except Exception as e2:
                log_persistente("RADIO", "Recuperacion fallida: {}".format(e2), "ERROR")
        gc.collect()
        return False

    def leer_paquete(self):
        if self._paquetes_en_cola <= 0:
            return None, None, None, None
        self._paquetes_en_cola -= 1
        try:
            datos, estado_rx_local = self._sx.recv()
            rssi = None
            snr = None
            for intento in range(3):
                try:
                    rssi = self._sx.getRSSI()
                    snr = self._sx.getSNR()
                    if rssi is not None and snr is not None:
                        break
                except Exception as e_intento:
                    if intento == 2:
                        log_warn("RX", "Fallo leyendo RSSI/SNR tras 3 intentos: {}".format(e_intento))
                    time.sleep_ms(5)

            log_rx_diag(self._ultimo_evento_irq, estado_rx_local, len(datos), datos)
            return datos, estado_rx_local, rssi, snr
        except Exception as e:
            log_persistente("RX", "recv() fallo: {}".format(e), "ERROR")
            return None, "ERR_RECV_ASSERT", None, None

    def standby(self):
        if self._sx:
            try:
                self._sx.standby()
            except Exception as e:
                log_warn("RADIO", "standby() fallo: {}".format(e))

    @property
    def frecuencia(self): return self._estado.frecuencia_actual
    @property
    def sf(self): return self._estado.sf_actual
    @property
    def bw(self): return self._estado.bw_actual
    @property
    def cr(self): return self._estado.cr_actual
    @property
    def sync_word(self): return self._estado.syncword_actual
    @property
    def preamble(self): return self._estado.preamble_actual
    @property
    def ganancia(self): return self._ganancia_actual
    @property
    def rx_iq(self): return self._estado.rx_iq_actual
    @property
    def crc_on(self): return self._estado.crc_actual
    @property
    def irq_count(self): return self._irq_count

    # Método para ajustar frecuencia desde fuera (usado en identificación por header)
    def forzar_frecuencia(self, freq_mhz):
        """Fuerza la frecuencia actual sin reconfigurar todo el radio."""
        self._estado.frecuencia_actual = freq_mhz
