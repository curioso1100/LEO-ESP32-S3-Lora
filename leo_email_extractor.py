#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LEO Email Extractor v5 para OpenSUSE Tumbleweed
Extrae emails no leídos de Gmail con asunto "LEO" y vuelca su contenido
a ficheros de texto plano ORGANIZADOS POR DÍA, ORDENADOS CRONOLÓGICAMENTE.

Cambios v5 (orden cronológico):
  - Recolecta todos los emails en memoria ANTES de escribir
  - Parsea HORA_PLACA de cada uno y ordena cronológicamente
  - Escribe al fichero en orden de tiempo real de la placa
  - Mantiene: UIDs, salida por día, append, move a etiqueta, try/except

Requisitos:
    zypper in python3 python3-BeautifulSoup4 python3-lxml

Uso:
    python3 leo_email_extractor_v5.py
    python3 leo_email_extractor_v5.py --dry-run
    python3 leo_email_extractor_v5.py --output-dir /ruta/custom/
"""

import argparse
import email
import imaplib
import os
import re
import sys
from datetime import datetime
from email.header import decode_header
from email.message import EmailMessage

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

# ============================================================================
# CONFIGURACIÓN — Edita estos valores
# ============================================================================
CONFIG_GMAIL_USER      = "emilio.florido@gmail.com"
CONFIG_APP_PASSWORD    = "ptdidmptfgtifuum"
CONFIG_IMAP_SERVER     = "imap.gmail.com"
CONFIG_IMAP_PORT       = 993
CONFIG_SUBJECT_FILTER  = "LEO"
CONFIG_OUTPUT_DIR      = os.path.expanduser("~/Descargas/LEO_Datos")
CONFIG_PROCESSED_LABEL = "LEO-Procesados"
CONFIG_MOVE_TO_LABEL   = True
# ============================================================================


def decode_subject(msg: EmailMessage) -> str:
    """Decodifica el asunto del email."""
    subject_raw = msg.get("Subject", "")
    if not subject_raw:
        return ""
    parts = decode_header(subject_raw)
    subject = ""
    for part, charset in parts:
        if isinstance(part, bytes):
            try:
                subject += part.decode(charset or "utf-8", errors="replace")
            except Exception:
                subject += part.decode("utf-8", errors="replace")
        else:
            subject += part
    return subject


def get_body_text(msg: EmailMessage) -> str:
    """Extrae el cuerpo del email como texto plano."""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition", ""))
            if "attachment" in content_disposition:
                continue
            if content_type == "text/plain":
                try:
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        body += payload.decode(charset, errors="replace")
                except Exception:
                    pass
            elif content_type == "text/html" and not body:
                try:
                    payload = part.get_payload(decode=True)
                    if payload and BeautifulSoup:
                        charset = part.get_content_charset() or "utf-8"
                        html = payload.decode(charset, errors="replace")
                        soup = BeautifulSoup(html, "lxml")
                        body += soup.get_text(separator="\n", strip=True)
                except Exception:
                    pass
    else:
        content_type = msg.get_content_type()
        try:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                text = payload.decode(charset, errors="replace")
                if content_type == "text/html" and BeautifulSoup:
                    soup = BeautifulSoup(text, "lxml")
                    body = soup.get_text(separator="\n", strip=True)
                else:
                    body = text
        except Exception:
            pass
    return body


def parse_placa_datetime(body: str) -> datetime:
    """
    Extrae la fecha/hora real de la placa del cuerpo del email.
    Devuelve un objeto datetime para ordenación cronológica.
    """
    m = re.search(r"Enviado:\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})\s+CEST", body)
    if m:
        dt_str = f"{m.group(1)} {m.group(2)}"
        try:
            return datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    # Fallback: cualquier fecha-hora YYYY-MM-DD HH:MM:SS
    m = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})", body)
    if m:
        dt_str = f"{m.group(1)} {m.group(2)}"
        try:
            return datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    return datetime.now()


def parse_placa_time(body: str) -> str:
    """Extrae la hora completa de la placa para el header (string)."""
    m = re.search(r"Enviado:\s+(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\s+CEST)", body)
    if m:
        return m.group(1)
    return "N/A"


def ensure_label_exists(mail: imaplib.IMAP4_SSL, label_name: str) -> bool:
    """Comprueba si una etiqueta existe; si no, la crea."""
    try:
        status, labels = mail.list()
        if status != "OK":
            return False
        label_name_quoted = '"' + label_name + '"'
        for label in labels:
            if label is None:
                continue
            parts = label.decode("utf-8", errors="replace").split(' "')
            if len(parts) >= 2:
                existing = parts[-1].strip('"')
                if existing == label_name:
                    return True
        status, _ = mail.create(label_name_quoted)
        return status == "OK"
    except Exception as e:
        print(f"[AVISO] No se pudo verificar/crear etiqueta '{label_name}': {e}")
        return False


def fetch_email_raw(mail: imaplib.IMAP4_SSL, uid_str: str) -> bytes:
    """Fetch robusto usando UID."""
    status, msg_data = mail.uid('fetch', uid_str, "(RFC822)")
    if status != "OK" or not msg_data:
        raise RuntimeError(f"Fetch fallido: status={status}, data={msg_data}")

    for item in msg_data:
        if isinstance(item, tuple) and len(item) >= 2:
            return item[1]
        elif isinstance(item, bytes) and item.startswith(b"From "):
            return item

    best = None
    best_len = 0
    for item in msg_data:
        if isinstance(item, bytes) and len(item) > best_len:
            best = item
            best_len = len(item)
    if best and best_len > 100:
        return best

    raise RuntimeError(f"No se encontró cuerpo de email en la respuesta IMAP: {msg_data}")


def extract_emails(dry_run: bool = False, output_dir: str = None):
    out_dir = output_dir or CONFIG_OUTPUT_DIR
    print(f"[INFO] Conectando a {CONFIG_IMAP_SERVER}:{CONFIG_IMAP_PORT}...")

    try:
        mail = imaplib.IMAP4_SSL(CONFIG_IMAP_SERVER, CONFIG_IMAP_PORT)
        mail.login(CONFIG_GMAIL_USER, CONFIG_APP_PASSWORD)
        print(f"[INFO] Login OK como {CONFIG_GMAIL_USER}")
    except Exception as e:
        print(f"[ERROR] Fallo de conexión/login: {e}")
        sys.exit(1)

    try:
        status, _ = mail.select("inbox")
        if status != "OK":
            print("[ERROR] No se pudo seleccionar INBOX")
            return

        # BUSCAR por UID
        search_criteria = f'(UNSEEN SUBJECT "{CONFIG_SUBJECT_FILTER}")'
        status, data = mail.uid('search', None, search_criteria)

        if status != "OK" or not data or not data[0]:
            print("[INFO] No hay emails nuevos sin leer con ese filtro.")
            mail.close()
            mail.logout()
            return

        email_uids = data[0].split()
        print(f"[INFO] Encontrados {len(email_uids)} email(s) sin procesar.")

        # ================================================================
        # FASE 1: Recolectar TODOS los emails en memoria
        # ================================================================
        emails_collected = []  # lista de dicts con todos los datos

        for uid in email_uids:
            uid_str = uid.decode()
            try:
                raw_email = fetch_email_raw(mail, uid_str)
                msg = email.message_from_bytes(raw_email)

                subject = decode_subject(msg)
                from_str = msg.get("From", "Desconocido")
                body = get_body_text(msg).strip()

                if not body:
                    print(f"  ⚠ UID {uid_str}: cuerpo vacío, saltando.")
                    if not dry_run:
                        mail.uid('store', uid_str, '+FLAGS', '\\Seen')
                    continue

                placa_dt = parse_placa_datetime(body)
                placa_time = parse_placa_time(body)
                placa_date = placa_dt.strftime("%Y-%m-%d")

                emails_collected.append({
                    "uid": uid_str,
                    "subject": subject,
                    "from": from_str,
                    "body": body,
                    "placa_dt": placa_dt,
                    "placa_time": placa_time,
                    "placa_date": placa_date,
                })
                print(f"  📥 UID {uid_str} | {placa_time} | {subject[:45]}...")

            except Exception as e:
                print(f"  ✗ UID {uid_str}: ERROR al fetch - {e}")
                continue

        if not emails_collected:
            print("[INFO] Ningún email con contenido válido. Nada que escribir.")
            mail.close()
            mail.logout()
            return

        # ================================================================
        # FASE 2: Ordenar cronológicamente por HORA_PLACA
        # ================================================================
        emails_collected.sort(key=lambda x: x["placa_dt"])
        print(f"[INFO] Ordenados {len(emails_collected)} email(s) cronológicamente.")

        # ================================================================
        # FASE 3: Escribir al fichero en orden
        # ================================================================
        os.makedirs(out_dir, exist_ok=True)
        files_written = {}

        for em in emails_collected:
            out_filename = f"Datos_{em['placa_date']}.txt"
            out_path = os.path.join(out_dir, out_filename)

            with open(out_path, "a", encoding="utf-8") as fout:
                fout.write("=" * 80 + "\n")
                fout.write(f"EXTRACTOR: {datetime.now().isoformat()}\n")
                fout.write(f"UID: {em['uid']}\n")
                fout.write(f"HORA_PLACA: {em['placa_time']}\n")
                fout.write(f"FROM: {em['from']}\n")
                fout.write(f"SUBJECT: {em['subject']}\n")
                fout.write("=" * 80 + "\n")
                fout.write(em["body"])
                fout.write("\n\n")

            files_written[em['placa_date']] = files_written.get(em['placa_date'], 0) + 1
            print(f"  ✓ {em['placa_date']} {em['placa_time']} | {em['subject'][:45]}...")

        # ================================================================
        # FASE 4: Mover/marcar como procesados
        # ================================================================
        if not dry_run:
            if CONFIG_MOVE_TO_LABEL:
                if ensure_label_exists(mail, CONFIG_PROCESSED_LABEL):
                    for em in emails_collected:
                        mail.uid('copy', em["uid"], CONFIG_PROCESSED_LABEL)
                        mail.uid('store', em["uid"], '+FLAGS', '\\Deleted')
                    mail.expunge()
                    print(f"[INFO] Emails movidos a etiqueta '{CONFIG_PROCESSED_LABEL}'.")
                else:
                    for em in emails_collected:
                        mail.uid('store', em["uid"], '+FLAGS', '\\Seen')
                    print("[INFO] Emails marcados como leídos (etiqueta no disponible).")
            else:
                for em in emails_collected:
                    mail.uid('store', em["uid"], '+FLAGS', '\\Seen')
                print("[INFO] Emails marcados como leídos.")

        total = sum(files_written.values())
        print(f"[INFO] Resumen: {total} email(s) escrito(s):")
        for day, count in sorted(files_written.items()):
            print(f"        -> {out_dir}/Datos_{day}.txt  ({count} email(s))")

        mail.close()
        mail.logout()
        print("[INFO] Desconectado. Listo.")

    except Exception as e:
        print(f"[ERROR] {e}")
        try:
            mail.logout()
        except Exception:
            pass
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Extrae emails LEO de Gmail a ficheros por día, ordenados cronológicamente."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Extrae pero NO marca como leídos ni mueve de etiqueta."
    )
    parser.add_argument(
        "--output-dir", type=str, default=None,
        help=f"Directorio de salida (por defecto: {CONFIG_OUTPUT_DIR})"
    )
    args = parser.parse_args()

    extract_emails(dry_run=args.dry_run, output_dir=args.output_dir)


if __name__ == "__main__":
    main()
