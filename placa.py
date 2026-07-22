# =========================================================================
# MÓDULO: placa.py
# Abstracción de hardware de la placa utilizada
# =========================================================================

import machine
import time
import os


PLACA = "HELTEC_WIFI_LORA_V3"
RADIO_CHIP = "SX1262"

LED_PIN = 35

LORA_SPI_BUS = 1

LORA_CLK  = 9
LORA_MOSI = 10
LORA_MISO = 11

LORA_CS   = 8
LORA_IRQ  = 14
LORA_RST  = 12
LORA_GPIO = 13

# Botón PRG (GPIO0). Strapping pin: LOW durante reset fuerza bootloader.
# En ejecución normal se puede leer como entrada digital con pull-up.
PRG_PIN = 0


# =========================================================================
# IMPLEMENTACION
# =========================================================================

_led = machine.Pin(LED_PIN, machine.Pin.OUT)

def led_on():
    _led.on()

def led_off():
    _led.off()

def led_toggle():
    _led.value(not _led.value())

def led_blink(veces, pausa_ms=300):
    for _ in range(veces):
        _led.on()
        time.sleep_ms(pausa_ms)
        _led.off()
        time.sleep_ms(pausa_ms)

def led_patron_error(ciclos=10, corto_ms=10, largo_ms=500):
    for _ in range(ciclos):
        _led.on()
        time.sleep_ms(corto_ms)
        _led.off()
        time.sleep_ms(largo_ms)

def reiniciar():
    try:
        import os
        os.sync()
        time.sleep_ms(200)
    except Exception:
        pass
    machine.reset()

def nombre_placa():
    return PLACA

def chip_radio():
    return RADIO_CHIP

def info_radio():
    return {
        "placa": PLACA,
        "chip": RADIO_CHIP,
        "spi_bus": LORA_SPI_BUS
    }

def crear_radio():
    import gc
    gc.collect()
    from sx1262 import SX1262
    gc.collect()

    return SX1262(
        spi_bus=LORA_SPI_BUS,
        clk=LORA_CLK,
        mosi=LORA_MOSI,
        miso=LORA_MISO,
        cs=LORA_CS,
        irq=LORA_IRQ,
        rst=LORA_RST,
        gpio=LORA_GPIO
    )


# =========================================================================
# BOTÓN PRG (GPIO0)
# =========================================================================
# NOTA: GPIO0 es un "strapping pin". Si se mantiene pulsado (LOW) durante
# un reset físico (pulsación del botón RST o reconexión de USB), el
# ESP32-S3 entrará en modo bootloader para carga de firmware en lugar de
# ejecutar el programa. Úsalo solo en ejecución normal.
# =========================================================================

_prg = None

def _prg_pin():
    """Devuelve el objeto Pin del botón PRG, inicializado una sola vez."""
    global _prg
    if _prg is None:
        _prg = machine.Pin(PRG_PIN, machine.Pin.IN, machine.Pin.PULL_UP)
    return _prg

def prg_pulsado():
    """True si el botón PRG está pulsado ahora mismo (LOW)."""
    return _prg_pin().value() == 0

def prg_pulsado_al_arrancar(espera_ms=100):
    """Detecta si PRG está pulsado justo tras el arranque.
    Incluye una pequeña espera para estabilizar la lectura.
    Si no se usa, no tiene efecto alguno en el resto del código.
    """
    p = _prg_pin()
    time.sleep_ms(espera_ms)
    return p.value() == 0

def modo_limpio(ficheros=None, parpadeos=3):
    """Elimina los ficheros indicados, parpadea el LED y reinicia la placa.
    Args:
        ficheros: lista de nombres de fichero a borrar. Si es None, no borra nada.
        parpadeos: veces que parpadea el LED antes de reiniciar.
    Si no se llama explícitamente, no tiene efecto alguno.
    """
    import uos  # import diferido para ahorrar memoria si no se usa
    if ficheros is None:
        ficheros = []
    for f in ficheros:
        try:
            uos.remove(f)
        except OSError:
            pass  # no existía o no se pudo borrar
    if parpadeos:
        led_blink(parpadeos, pausa_ms=150)
    reiniciar()


def leer_temperatura_cpu():
    """Lee la temperatura interna del MCU ESP32-S3.
    Retorna float (°C) o None si no está disponible.
    """
    try:
        import esp32
        return esp32.mcu_temperature()
    except Exception:
        return None


def leer_espacio_filesystem():
    """Retorna tupla (libre_kb, total_kb) o (None, None)."""
    try:
        stat = os.statvfs("/")
        frag_size = stat[1]   # f_frsize
        free_blocks = stat[3]  # f_bfree
        total_blocks = stat[2] # f_blocks
        libre_kb = (free_blocks * frag_size) / 1024
        total_kb = (total_blocks * frag_size) / 1024
        return libre_kb, total_kb
    except Exception:
        return None, None


class Ventilador:
    """Control de ventilador por GPIO con histéresis térmica.
    El estado se mantiene internamente; no usa variables globales.
    """

    def __init__(self, gpio, umbral_on_c=55.0, umbral_off_c=45.0):
        self._gpio = gpio
        self._umbral_on = umbral_on_c
        self._umbral_off = umbral_off_c
        self._encendido = False
        self._pin = None
        self._inicializado = False

    def inicializar(self):
        """Inicializa el pin GPIO. Retorna True si tuvo éxito."""
        if self._inicializado:
            return True
        try:
            self._pin = machine.Pin(self._gpio, machine.Pin.OUT)
            self._pin.value(0)  # Apagado inicialmente
            self._encendido = False
            self._inicializado = True
            return True
        except Exception:
            self._pin = None
            self._inicializado = False
            return False

    def controlar(self, temp_actual):
        """Controla el ventilador con histéresis.
        Retorna True si está encendido, False si apagado.
        """
        if not self._inicializado or self._pin is None or temp_actual is None:
            return self._encendido

        if temp_actual >= self._umbral_on and not self._encendido:
            self._pin.value(1)
            self._encendido = True
        elif temp_actual <= self._umbral_off and self._encendido:
            self._pin.value(0)
            self._encendido = False

        return self._encendido

    def estado(self):
        """Retorna True si el ventilador está encendido."""
        return self._encendido

    def encender(self):
        """Fuerza el encendido del ventilador. Retorna True si tuvo éxito."""
        if self._inicializado and self._pin is not None:
            self._pin.value(1)
            self._encendido = True
            return True
        return False  # No se pudo encender

    def apagar(self):
        """Fuerza el apagado del ventilador. Retorna True si tuvo éxito."""
        if self._inicializado and self._pin is not None:
            self._pin.value(0)
            self._encendido = False
            return True
        self._encendido = False  # Aunque falle, queremos apagar lógicamente
        return True  # Apagar es "seguro" incluso si falla
