# =========================================================================
# MÓDULO: email_state.py - Gestión de horas fijas de envío y persistencia
# =========================================================================
# Extraído de fase3.py V7.0.0
# Sin cambios funcionales.
# =========================================================================

import time
import os


class EstadoEmail:
    """Gestiona horas fijas de envío y persistencia."""

    def __init__(self, horas_fijas_seg, email_cada_seg):
        self._horas_fijas = horas_fijas_seg
        self._email_cada_seg = email_cada_seg
        self._ultimo_email_ts = time.time() if email_cada_seg > 0 else 0
        self._ultima_hora_enviada = self._cargar_ultima_hora()

    def _cargar_ultima_hora(self):
        try:
            with open("ultima_hora_email.txt", "r") as f:
                return int(f.read().strip())
        except Exception:
            return None

    def _guardar_ultima_hora(self, hora_seg):
        try:
            with open("ultima_hora_email.txt", "w") as f:
                f.write(str(hora_seg))
                f.flush()
                os.sync()
        except Exception:
            pass

    def toca_enviar(self, t_local_tuple, forzado=False):
        if forzado:
            return True
        if self._horas_fijas:
            hh, mm, ss = t_local_tuple[3], t_local_tuple[4], t_local_tuple[5]
            actual_seg = hh * 3600 + mm * 60 + ss
            for hf_seg in self._horas_fijas:
                diff = actual_seg - hf_seg
                if 0 <= diff <= 30:
                    if self._ultima_hora_enviada != hf_seg:
                        self._ultima_hora_enviada = hf_seg
                        self._guardar_ultima_hora(hf_seg)
                        return True
            return False
        elif self._email_cada_seg > 0:
            if (time.time() - self._ultimo_email_ts) >= self._email_cada_seg:
                self._ultimo_email_ts = time.time()
                return True
        return False

    def marcar_enviado(self):
        self._ultimo_email_ts = time.time()

    def proxima_hora_str(self, t_local_tuple):
        if not self._horas_fijas:
            return "---"
        hh, mm, ss = t_local_tuple[3], t_local_tuple[4], t_local_tuple[5]
        actual_seg = hh * 3600 + mm * 60 + ss
        for hf_seg in self._horas_fijas:
            if hf_seg > actual_seg:
                return '{:02d}:{:02d}'.format(hf_seg // 3600, (hf_seg % 3600) // 60)
        hf_seg = self._horas_fijas[0]
        return '{:02d}:{:02d}'.format(hf_seg // 3600, (hf_seg % 3600) // 60)

    def info_str(self, t_local_tuple):
        if self._horas_fijas:
            return "EMAIL:FIJO->{}".format(self.proxima_hora_str(t_local_tuple))
        elif self._email_cada_seg > 0:
            return "EMAIL:{}s/{}s".format(int(time.time() - self._ultimo_email_ts), self._email_cada_seg)
        return "EMAIL:OFF"
