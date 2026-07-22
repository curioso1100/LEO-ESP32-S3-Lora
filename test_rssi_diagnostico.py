# =========================================================================
# test_rssi_diagnostico.py - Diagnostico de bytes raw packetStatus
# CORREGIDO v2
# =========================================================================

import time
import gc
from placa import crear_radio, prg_pulsado, led_on, led_off

# --- Configuracion (misma que tu config.json para TRISAT-4) ---
FREQ = 436.700
BW = 250.0
SF = 10
CR = 5
SW = 18
POWER = 14
PR = 8

# --- Flag para IRQ (global, debe declararse antes de usar) ---
_rx_flag = False

def _rx_callback(events):
    global _rx_flag
    _rx_flag = True

def _leer_packet_status_raw(sx):
    """
    Lee los 3 bytes raw de packetStatus directamente del SX1262.
    """
    try:
        packet_status_val = sx.getPacketStatus()
        b0 = (packet_status_val >> 16) & 0xFF
        b1 = (packet_status_val >> 8) & 0xFF
        b2 = packet_status_val & 0xFF
        return b0, b1, b2
    except Exception as e:
        print("[ERROR] Fallo leyendo packetStatus:", e)
        return None, None, None

def _calcular_rssi_snr_actual(b0, b1, b2):
    """Calcula RSSI y SNR con el codigo ACTUAL (probablemente bug)."""
    packet_status = (b0 << 16) | (b1 << 8) | b2
    rssi_pkt = int((packet_status >> 8) & 0xFF)
    rssi = -1.0 * rssi_pkt / 2.0
    snr_pkt = int(packet_status & 0xFF)
    if snr_pkt < 128:
        snr = snr_pkt / 4.0
    else:
        snr = (snr_pkt - 256) / 4.0
    return rssi, snr

def _calcular_rssi_snr_corregido(b0, b1, b2):
    """Calcula RSSI y SNR con los indices CORREGIDOS."""
    rssi = -1.0 * b0 / 2.0
    if b1 < 128:
        snr = b1 / 4.0
    else:
        snr = (b1 - 256) / 4.0
    signal_rssi = -1.0 * b2 / 2.0
    return rssi, snr, signal_rssi

def _mostrar_diagnostico(sx, datos, estado_rx):
    b0, b1, b2 = _leer_packet_status_raw(sx)
    if b0 is None:
        print("=" * 60)
        print("[DIAG] ERROR al leer packetStatus")
        print("=" * 60)
        return
    
    rssi_actual, snr_actual = _calcular_rssi_snr_actual(b0, b1, b2)
    rssi_corr, snr_corr, signal_rssi = _calcular_rssi_snr_corregido(b0, b1, b2)
    
    payload_hex = datos.hex() if hasattr(datos, "hex") else str(datos)
    payload_len = len(datos)
    
    t = time.localtime()
    ts = "{:02d}:{:02d}:{:02d}".format(t[3], t[4], t[5])
    
    print("\n" + "=" * 60)
    print("[DIAG] {} | PAQUETE RECIBIDO".format(ts))
    print("=" * 60)
    print("  Payload: {} ({} bytes)".format(payload_hex, payload_len))
    print("  Estado RX: {}".format(estado_rx))
    print("-" * 60)
    print("  BYTES RAW packetStatus:")
    print("    data[0] (RssiPkt)      : 0x{:02X} = {:3d}".format(b0, b0))
    print("    data[1] (SnrPkt)       : 0x{:02X} = {:3d}".format(b1, b1))
    print("    data[2] (SignalRssiPkt): 0x{:02X} = {:3d}".format(b2, b2))
    print("-" * 60)
    print("  CALCULO ACTUAL (codigo actual):")
    print("    RSSI = -({})/2 = {:.1f} dBm  <-- lee data[1] (SnrPkt!)".format(b1, rssi_actual))
    print("    SNR  = {:d}/4 = {:.1f} dB    <-- lee data[2] (SignalRssiPkt!)".format(b2 if b2 < 128 else b2 - 256, snr_actual))
    print("-" * 60)
    print("  CALCULO CORREGIDO:")
    print("    RSSI = -({})/2 = {:.1f} dBm  <-- lee data[0] (RssiPkt)".format(b0, rssi_corr))
    print("    SNR  = {:d}/4 = {:.1f} dB    <-- lee data[1] (SnrPkt)".format(b1 if b1 < 128 else b1 - 256, snr_corr))
    print("    SignalRSSI = -({})/2 = {:.1f} dBm".format(b2, signal_rssi))
    print("=" * 60)
    
    led_on()
    time.sleep_ms(100)
    led_off()

def ejecutar():
    global _rx_flag
    
    print("\n" + "=" * 60)
    print("DIAGNOSTICO RSSI/SNR - SX1262")
    print("=" * 60)
    print("Frecuencia: {:.3f} MHz".format(FREQ))
    print("SF: {} | BW: {} | CR: {} | SW: {}".format(SF, BW, CR, SW))
    print("-" * 60)
    print("Esperando paquetes del 'satelite-falso'...")
    print("Activa el transmisor y observa los bytes raw.")
    print("=" * 60 + "\n")
    
    sx = crear_radio()
    sx.begin(freq=FREQ, bw=BW, sf=SF, cr=CR, syncWord=SW,
             power=POWER, preambleLength=PR, currentLimit=60.0)
    
    sx.setBlockingCallback(False, _rx_callback)
    
    gc.collect()
    
    while True:
        if _rx_flag:
            _rx_flag = False
            try:
                datos, estado_rx = sx.recv()
                if datos and len(datos) > 0:
                    _mostrar_diagnostico(sx, datos, estado_rx)
                else:
                    print("[DIAG] Paquete vacio o error, estado={}".format(estado_rx))
            except Exception as e:
                print("[ERROR] Excepcion en recepcion:", e)
                try:
                    sx.setBlockingCallback(False, _rx_callback)
                except:
                    pass
        
        if prg_pulsado():
            print("\n[SALIDA] PRG pulsado - terminando diagnostico")
            try:
                sx.standby()
            except:
                pass
            break
        
        time.sleep_ms(10)

if __name__ == "__main__":
    ejecutar()