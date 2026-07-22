# =========================================================================
# SCRIPT PRINCIPAL: main.py (Versión Modularizada)
# =========================================================================
from estado import leer_fase

# Leer la fase activa al arrancar el microcontrolador
fase = leer_fase()

if fase == 1:
    import fase1
    fase1.ejecutar()

elif fase == 2:
    import fase2
    fase2.ejecutar()

elif fase == 3:
    import fase3
    fase3.ejecutar()

elif fase == 4:
    import fase4
    fase4.ejecutar()
