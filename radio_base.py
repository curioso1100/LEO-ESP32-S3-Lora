# =========================================================================
# MÓDULO: radio_base.py
# =========================================================================
# NOTA:
# Toda nueva implementación de radio debe heredar de RadioBase
# para disponer del estado común:
#   crc_actual
#   rx_iq_actual
#
# Ver:
#   sx1262.py
#   sx127x_wrapper.py


# Interfaz mínima requerida por fase3.py
#
# begin(...)
# recv()
# setBlockingCallback()
# setFrequency()
# setRxIq()
# setSyncWord()
# standby()
# writeRegister()
#
# Estado requerido:
# crc_actual
# rx_iq_actual


class RadioBase:

    def __init__(self):

        self.crc_actual = False
        self.rx_iq_actual = False

        self.frecuencia_actual = None
        self.sf_actual = None
        self.bw_actual = None
        self.cr_actual = None
        self.syncword_actual = None
        self.preamble_actual = None