import gc
import sys
import os
import machine
import network
import micropython
import json
import time

def separador(titulo):
    print("\n" + "=" * 60)
    print("  " + titulo)
    print("=" * 60)

def conectar_wifi():
    """Conecta al WiFi usando la configuración de config.json"""
    print("\n[WiFi] Leyendo configuración...")
    try:
        with open("config.json", "r") as f:
            cfg = json.load(f)
        ssid = cfg.get("wifi_ssid", "")
        password = cfg.get("wifi_pass", "")
        
        if not ssid:
            print("[WiFi] ERROR: No se encontró 'wifi_ssid' en config.json")
            return False
            
        print("[WiFi] SSID: " + ssid)
        
        wlan = network.WLAN(network.STA_IF)
        wlan.active(True)
        
        if wlan.isconnected():
            print("[WiFi] Ya estaba conectado")
            return True
            
        wlan.connect(ssid, password)
        
        print("[WiFi] Conectando", end="")
        intentos = 0
        while not wlan.isconnected() and intentos < 15:
            print(".", end="")
            time.sleep(2)
            intentos += 1
            
        print()
        
        if wlan.isconnected():
            print("[WiFi] ¡Conectado! IP: " + wlan.ifconfig()[0])
            return True
        else:
            print("[WiFi] ERROR: No se pudo conectar tras 15 intentos")
            return False
            
    except Exception as e:
        print("[WiFi] ERROR: " + str(e))
        return False

def main():
    print("\n" + "#" * 60)
    print("#  DIAGNÓSTICO COMPLETO DE PLACA ESP32")
    print("#" * 60)
    
    # 0. CONEXIÓN WIFI (Imprescindible para pruebas SSL)
    separador("0. CONEXIÓN WIFI")
    if not conectar_wifi():
        print("\n[ABORTADO] Sin WiFi no se pueden hacer las pruebas SSL.")
        return
    
    # 1. SISTEMA
    separador("1. INFORMACIÓN DEL SISTEMA")
    try:
        uname = os.uname()
        print("Sistema:  " + uname.sysname)
        print("Release:  " + uname.release)
        print("Versión:  " + uname.version)
        print("Máquina:  " + uname.machine)
    except Exception as e:
        print("Error: " + str(e))
        
    print("\nMicroPython: " + sys.version)
    
    # 2. HARDWARE
    separador("2. HARDWARE")
    print("CPU Freq: " + str(machine.freq() // 1000000) + " MHz")
    
    try:
        import esp
        if hasattr(esp, 'flash_size'):
            print("Flash:    " + str(esp.flash_size() // 1024 // 1024) + " MB")
    except:
        pass

    # 3. MEMORIA (Lo más importante para nuestro problema SSL)
    separador("3. MEMORIA (GC)")
    gc.collect()
    print("Libre:    " + str(gc.mem_free()) + " bytes")
    print("Asignada: " + str(gc.mem_alloc()) + " bytes")
    
    print("\n--- Detalle de Fragmentación ---")
    try:
        # El '1' fuerza la impresión del layout y estadísticas (incluye max new split)
        micropython.mem_info(1)
    except Exception as e:
        print("Error mem_info: " + str(e))

    # 4. RED
    separador("4. RED")
    try:
        wlan = network.WLAN(network.STA_IF)
        mac = wlan.config('mac')
        # Formato seguro de MAC sin f-strings complejas
        mac_str = ':'.join(['%02x' % x for x in mac])
        print("MAC:      " + mac_str)
        print("Estado:   " + ("Conectado" if wlan.isconnected() else "Desconectado"))
        if wlan.isconnected():
            print("IP:       " + wlan.ifconfig()[0])
    except Exception as e:
        print("Error Red: " + str(e))

    # 5. ALMACENAMIENTO
    separador("5. ALMACENAMIENTO")
    try:
        s = os.statvfs('/')
        total = (s[0] * s[2]) // (1024*1024)
        libre = (s[0] * s[3]) // (1024*1024)
        print("Total:    " + str(total) + " MB")
        print("Libre:    " + str(libre) + " MB")
    except:
        pass
        
    print("\nArchivos en la raíz:")
    try:
        archivos = os.listdir('/')
        for f in archivos:
            try:
                stat = os.stat('/' + f)
                size = stat[6] if len(stat) > 6 else '?'
                # Formateo simple sin ljust ni f-strings con ancho
                print("  " + f + " " * (30 - len(f)) + str(size) + " bytes")
            except:
                print("  " + f)
    except Exception as e:
        print("Error listando archivos: " + str(e))

    # 6. PRUEBA SSL DIRECTA
    separador("6. PRUEBA SSL (smtp.gmail.com)")
    import socket
    import ssl
    
    try:
        res = socket.getaddrinfo("smtp.gmail.com", 465)
        ip = res[-1][-1][0]
        print("IP Gmail: " + ip)
        
        # Intento 1: Básico (Sin SNI)
        print("\n[Prueba 1] SSL básico (sin server_hostname)...")
        gc.collect()
        print("Memoria antes: " + str(gc.mem_free()))
        try:
            s = socket.socket()
            s.settimeout(10)
            s.connect((ip, 465))
            ss = ssl.wrap_socket(s)
            print("RESULTADO: ¡ÉXITO!")
            print("Memoria después: " + str(gc.mem_free()))
            ss.close()
            s.close()
        except Exception as e:
            print("RESULTADO: FALLO (" + str(e) + ")")
            try: s.close()
            except: pass
            
        # Intento 2: Con SNI
        print("\n[Prueba 2] SSL con server_hostname (SNI)...")
        gc.collect()
        print("Memoria antes: " + str(gc.mem_free()))
        try:
            s = socket.socket()
            s.settimeout(10)
            s.connect((ip, 465))
            ss = ssl.wrap_socket(s, server_hostname="smtp.gmail.com")
            print("RESULTADO: ¡ÉXITO!")
            print("Memoria después: " + str(gc.mem_free()))
            ss.close()
            s.close()
        except Exception as e:
            print("RESULTADO: FALLO (" + str(e) + ")")
            try: s.close()
            except: pass
            
        # Intento 3: Con SNI + cert_reqs=0
        print("\n[Prueba 3] SSL con SNI + cert_reqs=0...")
        gc.collect()
        print("Memoria antes: " + str(gc.mem_free()))
        try:
            s = socket.socket()
            s.settimeout(10)
            s.connect((ip, 465))
            ss = ssl.wrap_socket(s, server_hostname="smtp.gmail.com", cert_reqs=0)
            print("RESULTADO: ¡ÉXITO!")
            print("Memoria después: " + str(gc.mem_free()))
            ss.close()
            s.close()
        except Exception as e:
            print("RESULTADO: FALLO (" + str(e) + ")")
            try: s.close()
            except: pass
            
    except Exception as e:
        print("Error DNS/SSL: " + str(e))

    separador("FIN DEL DIAGNÓSTICO")
    print("Copia TODO el texto de esta consola y pégalo en el chat.")

if __name__ == "__main__":
    main()