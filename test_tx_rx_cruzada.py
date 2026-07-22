# =========================================================================
# test_tx_rx_cruzada.py - PRUEBA DE CAMPO
# Heltec WiFi LoRa 32 V3 (SX1262)
# =========================================================================

import time
import machine
from machine import Pin, SoftI2C, ADC
from placa import crear_radio, prg_pulsado, led_on, led_off
import ssd1306

# --- Intentar importar esp32 para wake_on_ext0 (deep sleep pin) ---
try:
    import esp32
    _HAS_ESP32 = True
except ImportError:
    _HAS_ESP32 = False
    print("[WARN] Modulo esp32 no disponible; deep sleep por pin no funcionara")

# --- CONFIGURACION OLED (Heltec V3) ---
_OLED_RST = 21
_VEXT     = 36
_I2C_SCL  = 18
_I2C_SDA  = 17
_OLED_ADDR = 0x3C

# --- CONFIGURACION RADIO
FREQ            = 436.700 # 868.0   
BW              = 250     # 125
SF              = 10      # 12
CR              = 5       # 8
SW              = 0x12    # Sync word privado estandar = 18 decimal
PR              = 8       # 12
POWER           = 16      # 21 <-- Es la máxima potencia
TX_INTERVALO_MS = 2000

# --- TIMEOUTS Y PROTECCIONES ---
_TX_TIMEOUT_MS = 3000       # Timeout maximo para send() (ms)
_RX_TIMEOUT_MS = 500        # Timeout para recv() (ms)
_RADIO_RESET_INTERVAL = 30000  # Reiniciar radio cada 30s sin paquetes

# --- UMBRAL BATERIA BAJA (valor REAL) ---
_BAT_UMBRAL = 3.500

# --- TABLA CALIBRACION: (raw_ADC, voltaje_real_multimetro) ---
_TABLA_CALIBRACION = [
    (993, 4.205), (982, 4.155), (970, 4.103), (957, 4.055), (943, 4.004),
    (931, 3.955), (856, 3.904), (533, 3.855), (228, 3.806), (86, 3.755),
    (29, 3.703), (9, 3.645), (3, 3.605), (1, 3.556), (0, 3.505),
]

# --- FLAG GLOBAL PARA IRQ ---
_rx_flag = False

# --- ESTADO DEL BOTON PRG CON DEBOUNCE ---
_PRG_ESTADO_IDLE = 0
_PRG_ESTADO_PRESIONANDO = 1
_PRG_ESTADO_PRESIONADO = 2
_PRG_ESTADO_SOLTANDO = 3
_PRG_ESTADO_ESPERA = 4

_prg_estado = _PRG_ESTADO_IDLE
_prg_t0 = None
_prg_t_debounce = None
_prg_resultado = None

_PRG_TOGGLEM_MIN = 80
_PRG_TOGGLEM_MAX = 2500
_PRG_SHUTDOWN_MS = 3000
_PRG_DEBOUNCE_MS = 30

# --- PROTECCION ANTI-BLOQUEO ---
_ultimo_pkt_valido = 0      # Timestamp del ultimo paquete valido recibido

# Contador de transmisiones - persiste entre cambios de modo
# Solo se resetea con reset fisico de la placa
_tx_contador = 0

# --- ADC BATERIA (GPIO1 = ADC1_CH0 en Heltec V3) ---
_adc_bat = None

def _init_bateria():
    global _adc_bat
    try:
        _adc_bat = ADC(Pin(1))
        _adc_bat.atten(ADC.ATTN_11DB)
        _adc_bat.width(ADC.WIDTH_12BIT)
    except Exception as e:
        print("[WARN] ADC bateria no inicializado:", e)
        _adc_bat = None

def _leer_bateria_raw():
    """Devuelve el promedio de 10 lecturas RAW del ADC (0-4095)."""
    if _adc_bat is None:
        return 0
    total = 0
    for _ in range(10):
        total += _adc_bat.read()
        time.sleep_ms(5)
    return total // 10

def _interpolar_bateria(raw_val):
    """Convierte raw a voltaje real usando interpolacion lineal por tramos."""
    tabla = _TABLA_CALIBRACION
    if not tabla:
        return 0.0
    if raw_val >= tabla[0][0]:
        return tabla[0][1]
    if raw_val <= tabla[-1][0]:
        return tabla[-1][1]
    for i in range(len(tabla) - 1):
        r_sup, v_sup = tabla[i]
        r_inf, v_inf = tabla[i + 1]
        if r_inf <= raw_val <= r_sup:
            if r_sup == r_inf:
                return v_sup
            ratio = (raw_val - r_inf) / (r_sup - r_inf)
            return v_inf + ratio * (v_sup - v_inf)
    return tabla[-1][1]

def _fmt_bateria(v_real, raw):
    """Formatea voltaje real. Si raw==0, sabemos que es <3.5V."""
    if raw == 0:
        return "V<3.5!"
    if v_real < _BAT_UMBRAL:
        return "V{:.2f}!".format(v_real)
    return "V{:.2f}".format(v_real)

def _rx_callback(e):
    global _rx_flag
    _rx_flag = True

def _blink_led(ms=150, doble=False):
    if not doble:
        led_on(); time.sleep_ms(ms); led_off()
    else:
        led_on(); time.sleep_ms(80); led_off()
        time.sleep_ms(80)
        led_on(); time.sleep_ms(80); led_off()

# --- APAGADO Y DEEP SLEEP ---
def _apagar_y_dormir(sx):
    print(">>> Entrando en deep sleep...")
    try:
        vext = Pin(_VEXT, Pin.OUT)
        vext.value(1)
    except Exception:
        pass
    try:
        if sx is not None:
            sx.standby()
    except Exception:
        pass
    try:
        led_off()
    except Exception:
        pass
    prg_raw = machine.Pin(0, machine.Pin.IN, machine.Pin.PULL_UP)
    print(">>> Suelta PRG para confirmar...")
    while prg_raw.value() == 0:
        time.sleep_ms(50)
    time.sleep_ms(300)
    if _HAS_ESP32:
        try:
            esp32.wake_on_ext0(pin=prg_raw, level=0)
        except Exception as e:
            print("[WARN] wake_on_ext0 fallo:", e)
    time.sleep_ms(100)
    machine.deepsleep()

# --- DETECCION DE PRG: MAQUINA DE ESTADOS CON DEBOUNCE ---
def _check_prg():
    global _prg_estado, _prg_t0, _prg_t_debounce, _prg_resultado

    ahora = time.ticks_ms()
    pulsado = prg_pulsado()

    if _prg_resultado is not None:
        res = _prg_resultado
        _prg_resultado = None
        return res

    if _prg_estado == _PRG_ESTADO_IDLE:
        if pulsado:
            _prg_estado = _PRG_ESTADO_PRESIONANDO
            _prg_t_debounce = ahora

    elif _prg_estado == _PRG_ESTADO_PRESIONANDO:
        if time.ticks_diff(ahora, _prg_t_debounce) >= _PRG_DEBOUNCE_MS:
            if pulsado:
                _prg_estado = _PRG_ESTADO_PRESIONADO
                _prg_t0 = ahora
                print("[PRG] Presion confirmada")
            else:
                _prg_estado = _PRG_ESTADO_IDLE
                print("[PRG] Falso positivo (rebote)")

    elif _prg_estado == _PRG_ESTADO_PRESIONADO:
        if not pulsado:
            _prg_estado = _PRG_ESTADO_SOLTANDO
            _prg_t_debounce = ahora
        else:
            dt = time.ticks_diff(ahora, _prg_t0)
            if dt >= _PRG_SHUTDOWN_MS:
                _prg_resultado = "SHUTDOWN"
                _prg_estado = _PRG_ESTADO_ESPERA
                print("[PRG] -> SHUTDOWN ({}ms)".format(dt))

    elif _prg_estado == _PRG_ESTADO_SOLTANDO:
        if time.ticks_diff(ahora, _prg_t_debounce) >= _PRG_DEBOUNCE_MS:
            if not pulsado:
                dt = time.ticks_diff(_prg_t_debounce, _prg_t0)
                if dt >= _PRG_TOGGLEM_MIN and dt < _PRG_TOGGLEM_MAX:
                    _prg_resultado = "TOGGLE"
                    print("[PRG] -> TOGGLE ({}ms)".format(dt))
                elif dt < _PRG_TOGGLEM_MIN:
                    print("[PRG] Pulsacion muy corta ({}ms), ignorada".format(dt))
                else:
                    print("[PRG] Pulsacion muy larga ({}ms), ignorada".format(dt))
                _prg_estado = _PRG_ESTADO_IDLE
            else:
                _prg_estado = _PRG_ESTADO_PRESIONADO
                print("[PRG] Falso suelto (rebote)")

    elif _prg_estado == _PRG_ESTADO_ESPERA:
        if not pulsado:
            _prg_estado = _PRG_ESTADO_IDLE
            print("[PRG] Liberado tras SHUTDOWN")

    return None

# --- INICIALIZACION OLED ---
def _inicializar_oled():
    try:
        vext = Pin(_VEXT, Pin.OUT)
        vext.value(0)
        time.sleep_ms(50)
        rst = Pin(_OLED_RST, Pin.OUT)
        rst.value(0)
        time.sleep_ms(20)
        rst.value(1)
        time.sleep_ms(50)
        i2c = SoftI2C(scl=Pin(_I2C_SCL), sda=Pin(_I2C_SDA), freq=400000)
        oled = ssd1306.SSD1306_I2C(128, 64, i2c, addr=_OLED_ADDR)
        oled.fill(0)
        oled.show()
        return True, oled
    except Exception as e:
        print("[WARN] OLED no disponible:", e)
        return False, None

oled_ok = False
oled = None

def _mostrar(linea0="", linea1="", linea2="", linea3="", linea4="", linea5=""):
    print(linea0)
    if linea1: print(linea1)
    if linea2: print(linea2)
    if linea3: print(linea3)
    if linea4: print(linea4)
    if linea5: print(linea5)
    print("-" * 40)
    if oled_ok and oled is not None:
        try:
            oled.fill(0)
            oled.text(linea0[:16], 0, 0)
            if linea1: oled.text(linea1[:16], 0, 10)
            if linea2: oled.text(linea2[:16], 0, 20)
            if linea3: oled.text(linea3[:16], 0, 30)
            if linea4: oled.text(linea4[:16], 0, 40)
            if linea5: oled.text(linea5[:16], 0, 50)
            oled.show()
        except Exception as e:
            print("[WARN] Error OLED:", e)

def _rssi_snr_reales(sx):
    """Lee RSSI y SNR con proteccion de timeout."""
    try:
        rssi = sx.getRSSI()
        snr = sx.getSNR()
        return rssi, snr
    except Exception as e:
        print("[WARN] Error leyendo RSSI/SNR:", e)
        return -120.0, -20.0

# --- REINICIO DE RADIO ---
def _reiniciar_radio(sx):
    """Reinicia la radio por si se ha bloqueado."""
    global _rx_flag
    print("[RADIO] Reiniciando radio...")
    try:
        sx.standby()
        time.sleep_ms(100)
        sx.setBlockingCallback(False, _rx_callback)
        time.sleep_ms(50)
        _rx_flag = False
        print("[RADIO] Reinicio OK")
        return True
    except Exception as e:
        print("[ERROR] Fallo al reiniciar radio:", e)
        return False

# --- FUNCIONES AUXILIARES PARA ESTADO DE RADIO ---
def _safe_standby(sx):
    """Pone la radio en standby con proteccion."""
    try:
        sx.standby()
        return True
    except Exception as e:
        print("[WARN] standby() fallo:", e)
        return False

def _limpiar_irqs(sx):
    """Limpia IRQs pendientes para evitar estados inconsistentes."""
    try:
        # Intentar limpiar IRQs si la libreria lo soporta
        if hasattr(sx, 'clearIrqStatus'):
            sx.clearIrqStatus(0xFFFF)
        elif hasattr(sx, 'clearIRQ'):
            sx.clearIRQ()
        time.sleep_ms(10)
        return True
    except Exception as e:
        print("[WARN] No se pudieron limpiar IRQs:", e)
        return False

# --- MODO TRANSMISOR ---
def _modo_tx(sx):
    global _rx_flag, _tx_contador

    # FIX 1: Desregistrar callback RX antes de entrar en modo TX
    # El callback RX no debe estar activo mientras transmitimos
    try:
        sx.setBlockingCallback(True)  # Modo bloqueante SIN callback
        time.sleep_ms(50)
        _rx_flag = False
        print("[TX] Callback RX desregistrado")
    except Exception as e:
        print("[WARN] No se pudo desregistrar callback:", e)

    # FIX 2: Poner radio en standby y limpiar IRQs
    _safe_standby(sx)
    _limpiar_irqs(sx)

    while True:
        accion = _check_prg()
        if accion == "SHUTDOWN":
            _apagar_y_dormir(sx)
            return None
        if accion == "TOGGLE":
            _safe_standby(sx)
            return "RX"

        msg = "T{:03d}".format(_tx_contador)

        # FIX 3: standby() + limpiar IRQs ANTES de cada send()
        _safe_standby(sx)
        _limpiar_irqs(sx)
        time.sleep_ms(20)

        # FIX 4: send() con timeout por software
        t_send_start = time.ticks_ms()
        send_ok = False
        try:
            sx.send(msg.encode())
            send_ok = True
        except Exception as e:
            print("[ERROR] send() fallo:", e)
            # Intentar recuperar
            _safe_standby(sx)
            _limpiar_irqs(sx)
            time.sleep_ms(100)

        # Verificar si send() tardó demasiado (indica bloqueo)
        t_send = time.ticks_diff(time.ticks_ms(), t_send_start)
        if t_send > _TX_TIMEOUT_MS:
            print("[WARN] send() muy lento ({}ms), posible bloqueo".format(t_send))
            # Forzar recuperación
            _reiniciar_radio(sx)
            _safe_standby(sx)

        if not send_ok:
            # Si send falló, saltar a la siguiente iteración
            time.sleep_ms(500)
            continue

        # Comprobar PRG inmediatamente despues de send
        accion = _check_prg()
        if accion == "SHUTDOWN":
            _apagar_y_dormir(sx)
            return None
        if accion == "TOGGLE":
            _safe_standby(sx)
            return "RX"

        raw = _leer_bateria_raw()
        v_real = _interpolar_bateria(raw)
        low = (raw == 0) or (v_real < _BAT_UMBRAL)

        # Comprobar PRG despues de leer bateria
        accion = _check_prg()
        if accion == "SHUTDOWN":
            _apagar_y_dormir(sx)
            return None
        if accion == "TOGGLE":
            _safe_standby(sx)
            return "RX"

        _blink_led(150, doble=low)

        bat = _fmt_bateria(v_real, raw)

        _mostrar(
            "MODO: EMISOR",
            "Frq:{:.3f}MHz".format(FREQ),
            "TX->" + msg,
            "PWR:{}dBm{}".format(POWER, bat),
            "SF{} BW{} CR{}".format(SF, BW, CR),
            "Pulsa PRG=RX"
        )
        _tx_contador = (_tx_contador + 1) % 1000

        t0 = time.ticks_ms()
        while time.ticks_diff(time.ticks_ms(), t0) < TX_INTERVALO_MS:
            accion = _check_prg()
            if accion == "SHUTDOWN":
                _apagar_y_dormir(sx)
                return None
            if accion == "TOGGLE":
                _safe_standby(sx)
                return "RX"
            time.sleep_ms(10)

# --- MODO RECEPTOR ---
def _modo_rx(sx):
    global _rx_flag, _ultimo_pkt_valido
    ultimo_pkt = "---"
    ultimo_pkt_raw = b""
    ultimo_rssi = 0.0
    ultimo_snr = 0.0
    cont = 0
    ultimo_adv_bat = time.ticks_ms()
    _ultimo_pkt_valido = time.ticks_ms()
    ultimo_refresh_pantalla = time.ticks_ms()

    # FIX 5: Al entrar en RX, registrar callback y limpiar IRQs
    try:
        sx.setBlockingCallback(False, _rx_callback)
        time.sleep_ms(50)
        _rx_flag = False
        print("[RX] Callback registrado, iniciando recepcion")
    except Exception as e:
        print("[WARN] Error registrando callback RX:", e)

    _limpiar_irqs(sx)

    # Asegurar que la radio está en modo recepción
    for intento in range(3):
        try:
            sx.startReceive()
            print("[RX] startReceive OK (intento {})".format(intento + 1))
            break
        except Exception as e:
            print("[WARN] startReceive fallo (intento {}): {}".format(intento + 1, e))
            time.sleep_ms(100)
    time.sleep_ms(200)

    # === MOSTRAR PANTALLA INICIAL AL ENTRAR EN RX ===
    raw = _leer_bateria_raw()
    v_real = _interpolar_bateria(raw)
    bat = _fmt_bateria(v_real, raw)
    _mostrar(
        "MODO:RECEPTOR",
        "Frq:{:.3f}MHz".format(FREQ),
        "Esperando paquete...",
        "PWR:{}dBm{}".format(POWER, bat),
        "SF{} BW{} CR{}".format(SF, BW, CR),
        "Pulsa PRG=TX"
    )

    while True:
        # === SIEMPRE comprobar PRG primero, incluso si hay flag RX ===
        accion = _check_prg()
        if accion == "SHUTDOWN":
            _apagar_y_dormir(sx)
            return None
        if accion == "TOGGLE":
            return "TX"

        raw = _leer_bateria_raw()
        v_real = _interpolar_bateria(raw)
        low = (raw == 0) or (v_real < _BAT_UMBRAL)

        if low and time.ticks_diff(time.ticks_ms(), ultimo_adv_bat) > 5000:
            _blink_led(80, doble=True)
            ultimo_adv_bat = time.ticks_ms()

        # === Procesar recepcion con proteccion total ===
        if _rx_flag:
            _rx_flag = False
            try:
                # Timeout por software: si recv() tarda mas de _RX_TIMEOUT_MS, abortar
                t_recv_start = time.ticks_ms()
                datos, estado = sx.recv()
                t_recv = time.ticks_diff(time.ticks_ms(), t_recv_start)

                if t_recv > _RX_TIMEOUT_MS:
                    print("[WARN] recv() lento: {}ms".format(t_recv))

                if datos and len(datos) > 0:
                    if datos != ultimo_pkt_raw:
                        ultimo_pkt_raw = datos
                        ultimo_pkt = datos.decode("utf-8", "ignore")
                        _ultimo_pkt_valido = time.ticks_ms()

                        # Leer RSSI/SNR con proteccion
                        ultimo_rssi, ultimo_snr = _rssi_snr_reales(sx)
                        cont += 1
                        _blink_led(150, doble=low)
                        bat = _fmt_bateria(v_real, raw)

                        _mostrar(
                            "MODO:RECEPTOR",
                            "Frq:{:.3f}MHz".format(FREQ),
                            "RX:" + ultimo_pkt[:8],
                            "RSSI:{:.1f}{}".format(ultimo_rssi, bat),
                            "SNR:{:.1f} P:{}".format(ultimo_snr, cont),
                            "Pulsa PRG=TX"
                        )
                        ultimo_adv_bat = time.ticks_ms()
                        ultimo_refresh_pantalla = time.ticks_ms()
                else:
                    # Paquete vacio o con error
                    print("[RX] Paquete vacio/erroneo, estado={}".format(estado))

            except Exception as e:
                print("[ERROR] Excepcion en recepcion:", e)
                # Intentar recuperar la radio
                _reiniciar_radio(sx)
                # Asegurar que vuelve a escuchar tras reinicio
                try:
                    sx.startReceive()
                except Exception as e2:
                    print("[WARN] startReceive tras reinicio:", e2)

        # === Refrescar pantalla periodicamente si no hay paquetes ===
        if time.ticks_diff(time.ticks_ms(), ultimo_refresh_pantalla) > 8000:
            bat = _fmt_bateria(v_real, raw)
            _mostrar(
                "MODO:RECEPTOR",
                "Frq:{:.3f}MHz".format(FREQ),
                "Esperando...",
                "PWR:{}dBm{}".format(POWER, bat),
                "SF{} BW{} CR{}".format(SF, BW, CR),
                "Pulsa PRG=TX"
            )
            ultimo_refresh_pantalla = time.ticks_ms()

        # === Proteccion: si no hay paquetes validos durante mucho tiempo, reiniciar radio ===
        tiempo_sin_pkt = time.ticks_diff(time.ticks_ms(), _ultimo_pkt_valido)
        if tiempo_sin_pkt > _RADIO_RESET_INTERVAL:
            print("[WARN] Sin paquetes validos durante {}s, reiniciando radio...".format(tiempo_sin_pkt // 1000))
            _reiniciar_radio(sx)
            _ultimo_pkt_valido = time.ticks_ms()
            # Asegurar que vuelve a escuchar tras reinicio
            try:
                sx.startReceive()
            except Exception as e:
                print("[WARN] startReceive tras reinicio:", e)

        time.sleep_ms(10)

# --- MAIN ---
def ejecutar():
    global oled_ok, oled, _rx_flag

    if machine.reset_cause() != machine.DEEPSLEEP_RESET:
        _apagar_y_dormir(None)

    _init_bateria()

    oled_ok, oled = _inicializar_oled()
    raw = _leer_bateria_raw()
    v_real = _interpolar_bateria(raw)
    bat = _fmt_bateria(v_real, raw)
    print("[INIT] raw={} v_real={:.3f}V {}".format(raw, v_real, bat))
    _mostrar("DESPERTANDO...", "Suelta PRG...", "BAT:{}".format(bat))
    while prg_pulsado():
        time.sleep_ms(50)

    _mostrar("INICIANDO...", "Frq:{:.3f}MHz".format(FREQ), "BAT:{}".format(bat))

    sx = crear_radio()
    sx.begin(freq=FREQ, bw=BW, sf=SF, cr=CR, syncWord=SW,
             power=POWER, preambleLength=PR, currentLimit=60.0)

    modo = "RX"

    # FIX 6: Inicializar en RX con callback registrado
    try:
        sx.setBlockingCallback(False, _rx_callback)
        time.sleep_ms(50)
        _rx_flag = False
    except Exception as e:
        print("[WARN] Error inicializando callback:", e)

    _mostrar("Pulsa PRG para", "cambiar TX/RX", "BAT:{}".format(bat), "Iniciando en RX...")
    time.sleep_ms(1500)

    while True:
        if modo == "TX":
            modo = _modo_tx(sx)
        else:
            modo = _modo_rx(sx)

        if modo is None:
            _apagar_y_dormir(sx)

        # Mostrar transicion para que el usuario vea el cambio
        if modo == "RX":
            _mostrar("CAMBIANDO A...", "MODO RECEPTOR", "Sincronizando...")
        else:
            _mostrar("CAMBIANDO A...", "MODO EMISOR", "Preparando...")

        # FIX 7: Transicion segura entre modos
        _safe_standby(sx)
        _limpiar_irqs(sx)
        time.sleep_ms(50)
        _rx_flag = False
        time.sleep_ms(10)

        # No es necesario cambiar callback aqui, cada modo lo gestiona al entrar

if __name__ == "__main__":
    ejecutar()
