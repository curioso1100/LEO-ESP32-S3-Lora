#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ITV LEO V9.1 Visualizador de Datos
Genera un dashboard HTML interactivo a partir de los emails de datos de captura del sistema ITV LEO.

Uso:
    python leo_visualizer.py datos.txt
Genera:
    datos.html

Requisitos: solo Python 3.7+ (libreria estandar)
"""

import sys
import re
import json
import os
from collections import defaultdict
from datetime import datetime

SAT_COLORS = {
    'TRISAT-4':  '#e74c3c',
    'MORSAT-1':  '#3498db',
    'KOSAR-1.5': '#2ecc71',
    'SM-3.1':    '#f39c12',
    'NORBY-2':   '#9b59b6',
    'MULE-4T':   '#1abc9c',
    'HUCSat':    '#e91e63',
    'BASE':      '#7f8c8d',
    'PASE':      '#2c3e50',
}

def get_color(name):
    return SAT_COLORS.get(name, '#95a5a6')


def split_by_gaps(items, gap_minutes=30):
    """Inserta None entre items separados por mas de gap_minutes.
    Rompe la linea en Plotly entre pases diferentes del mismo satelite."""
    if not items:
        return items
    result = [items[0]]
    for i in range(1, len(items)):
        t1 = datetime.strptime(items[i-1]['datetime'], '%Y-%m-%d %H:%M:%S')
        t2 = datetime.strptime(items[i]['datetime'], '%Y-%m-%d %H:%M:%S')
        gap = (t2 - t1).total_seconds() / 60
        if gap > gap_minutes:
            # Punto fantasma con None en Y para romper la linea
            phantom = dict(items[i-1])
            phantom['rssi'] = None
            phantom['snr'] = None
            phantom['elevation'] = None
            phantom['ram'] = None
            phantom['temp'] = None
            result.append(phantom)
        result.append(items[i])
    return result


def is_valid_date(dt_str):
    """Filtra fechas de RTC no sincronizado (ej: 2000-01-01)."""
    try:
        year = int(dt_str[:4])
        return year >= 2024
    except:
        return False


def parse_file(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        raw = f.read()

    hb_pattern = re.compile(
        r'HB\s+(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s+'
        r'(BASE|PASE)\s+(.*?)\s+'
        r'([\d.]+)\s+SF(\d+)\s+BW([\d.]+)\s+CR(\d+)\s+SW(\d+)\s+'
        r'C(\d+)\s+I(\d+)\s+RAM=(\d+)\s+IRQ=(\d+)\s+'
        r'(?:E=(\S+)\s+)?T=([\d.]+)\s+V=(\w+)\s+FS=(\d+)'
    )

    cap_pattern = re.compile(
        r'SAT=(\S+)\s+HORA=(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s+'
        r'RSSI=([\-\d.]+)\s+SNR=([\-\d.]+)\s+'
        r'FREC=([\d.]+)MHz\s+SF=(\d+)\s+BW=([\d.]+)\s+CR=(\d+)\s+SW=(\d+)\s+'
        r'RXIQ=(\w+)\s+CRC=(\w+)\s+IMPL=(\w+)\s+PLEN=(\d+)\s+MODO=(\w+)\s+'
        r'DATA=(\S+)'
    )

    meta_pattern = re.compile(r'---BEGIN_META---\n(.*?)\n---END_META---', re.DOTALL)

    sys_pattern = re.compile(
        r'RSSI WiFi:\s+([\-\d]+)\s+dBm.*?'
        r'Temperatura CPU:\s+([\d.]+)C.*?'
        r'Espacio filesystem:\s+(\d+)KB libres.*?'
        r'Paquetes capturados:\s+(\d+)\s+Descartados:\s+(\d+)',
        re.DOTALL
    )

    # Patrones para la agenda de pases diarios
    agenda_header_pattern = re.compile(
        r'Reporte diario de pases LEO V9\.1\n={2,}\n'
        r'RSSI WiFi:\s+[\-\d]+\s+dBm\n'
        r'Fecha Agenda:\s+(\d{4}-\d{2}-\d{2})'
    )

    pase_line_pattern = re.compile(
        r'\*\s+\[\d{2}/\d{2}\]\s+Pase:\s+(\d{2}:\d{2})\s+a\s+(\d{2}:\d{2})\s+-\s+Satélite:\s+(\S+)\s+\(Elev:\s+(\d+)\s+grados\s+-\s+Frec:\s+(\d+)\s+Hz\)'
    )

    heartbeats = []
    skipped_hb = 0
    for m in hb_pattern.finditer(raw):
        dt = m.group(1)
        if not is_valid_date(dt):
            skipped_hb += 1
            continue
        heartbeats.append({
            'datetime': dt,
            'mode': m.group(2),
            'satellite': m.group(3).strip(),
            'freq': float(m.group(4)),
            'sf': int(m.group(5)),
            'bw': float(m.group(6)),
            'cr': int(m.group(7)),
            'sw': int(m.group(8)),
            'c': int(m.group(9)),
            'i': int(m.group(10)),
            'ram': int(m.group(11)),
            'irq': int(m.group(12)),
            'elevation': m.group(13) if m.group(13) else 'N/A',
            'temp': float(m.group(14)),
            'fan': m.group(15),
            'fs': int(m.group(16)),
        })

    captures = []
    for m in cap_pattern.finditer(raw):
        captures.append({
            'satellite': m.group(1),
            'datetime': m.group(2),
            'rssi': float(m.group(3)),
            'snr': float(m.group(4)),
            'freq': float(m.group(5)),
            'sf': int(m.group(6)),
            'bw': float(m.group(7)),
            'cr': int(m.group(8)),
            'sw': int(m.group(9)),
            'rxiq': m.group(10) == 'True',
            'crc': m.group(11) == 'True',
            'impl': m.group(12) == 'True',
            'plen': int(m.group(13)),
            'mode': m.group(14),
            'data': m.group(15),
        })

    metas = []
    for m in meta_pattern.finditer(raw):
        metas.append(m.group(1))

    systems = []
    for m in sys_pattern.finditer(raw):
        systems.append({
            'wifi_rssi': int(m.group(1)),
            'cpu_temp': float(m.group(2)),
            'fs_kb': int(m.group(3)),
            'captured': int(m.group(4)),
            'dropped': int(m.group(5)),
        })

    # Parsear agenda de pases diarios
    daily_passes = []
    for agenda_m in agenda_header_pattern.finditer(raw):
        agenda_date = agenda_m.group(1)
        # Buscar líneas de pase desde la posición del header hasta el siguiente bloque
        block_start = agenda_m.end()
        # Encontrar el final del bloque (próxima sección conocida)
        next_block = raw.find('=== LOGS OPERATIVOS ===', block_start)
        if next_block == -1:
            next_block = raw.find('=== TODOS LOS HEARTBEATS', block_start)
        if next_block == -1:
            next_block = len(raw)
        block_text = raw[block_start:next_block]
        for pase_m in pase_line_pattern.finditer(block_text):
            start_time = pase_m.group(1)
            end_time = pase_m.group(2)
            daily_passes.append({
                'date': agenda_date,
                'start': f"{agenda_date} {start_time}:00",
                'end': f"{agenda_date} {end_time}:00",
                'satellite': pase_m.group(3),
                'elevation': int(pase_m.group(4)),
                'freq_hz': int(pase_m.group(5)),
                'freq_mhz': int(pase_m.group(5)) / 1e6,
            })

    if skipped_hb > 0:
        print(f"   Heartbeats descartados (RTC no sincronizado): {skipped_hb}")

    return heartbeats, captures, metas, systems, daily_passes


def generate_html(heartbeats, captures, metas, systems, daily_passes, outfile):
    cap_by_sat = defaultdict(list)
    for c in captures:
        cap_by_sat[c['satellite']].append(c)

    pase_hbs = [hb for hb in heartbeats if hb['mode'] == 'PASE']
    base_hbs = [hb for hb in heartbeats if hb['mode'] == 'BASE']

    total_captures = len(captures)
    unique_sats = sorted(set(c['satellite'] for c in captures))
    total_hbs = len(heartbeats)
    total_pase = len(pase_hbs)
    total_base = len(base_hbs)
    total_daily_passes = len(daily_passes)

    if heartbeats:
        dtimes = [datetime.strptime(h['datetime'], '%Y-%m-%d %H:%M:%S') for h in heartbeats]
        date_min = min(dtimes).strftime('%Y-%m-%d %H:%M')
        date_max = max(dtimes).strftime('%Y-%m-%d %H:%M')
    else:
        date_min = date_max = 'N/A'

    # --- Datos para gráficos ---

    rssi_traces = []
    for sat in unique_sats:
        items = sorted(cap_by_sat[sat], key=lambda x: x['datetime'])
        items = split_by_gaps(items, gap_minutes=30)
        rssi_traces.append({
            'x': [i['datetime'] for i in items],
            'y': [i['rssi'] if i.get('rssi') is not None else None for i in items],
            'mode': 'lines+markers',
            'name': sat,
            'line': {'color': get_color(sat), 'width': 2},
            'marker': {'size': 8},
        })

    snr_traces = []
    for sat in unique_sats:
        items = sorted(cap_by_sat[sat], key=lambda x: x['datetime'])
        items = split_by_gaps(items, gap_minutes=30)
        snr_traces.append({
            'x': [i['datetime'] for i in items],
            'y': [i['snr'] if i.get('snr') is not None else None for i in items],
            'mode': 'lines+markers',
            'name': sat,
            'line': {'color': get_color(sat), 'width': 2},
            'marker': {'size': 8},
        })

    bar_counts = [len(cap_by_sat[s]) for s in unique_sats]
    bar_colors = [get_color(s) for s in unique_sats]
    bar_trace = [{
        'x': unique_sats,
        'y': bar_counts,
        'type': 'bar',
        'marker': {'color': bar_colors},
        'text': [str(c) for c in bar_counts],
        'textposition': 'outside',
    }]

    elev_traces = []
    pase_sats = sorted(set(h['satellite'] for h in pase_hbs if h['satellite'] != '-'))
    for sat in pase_sats:
        items = [h for h in pase_hbs if h['satellite'] == sat and h['elevation'] != 'N/A']
        items = sorted(items, key=lambda x: x['datetime'])
        items = split_by_gaps(items, gap_minutes=30)
        if items:
            elev_traces.append({
                'x': [i['datetime'] for i in items],
                'y': [int(i['elevation']) if i.get('elevation') is not None else None for i in items],
                'mode': 'lines+markers',
                'name': sat,
                'line': {'color': get_color(sat), 'width': 2},
                'marker': {'size': 8},
            })

    temp_trace = [{
        'x': [h['datetime'] for h in heartbeats],
        'y': [h['temp'] for h in heartbeats],
        'mode': 'lines+markers',
        'name': 'CPU Temp',
        'line': {'color': '#e74c3c', 'width': 2},
        'marker': {'size': 6},
    }]

    ram_trace = [{
        'x': [h['datetime'] for h in heartbeats],
        'y': [h['ram'] for h in heartbeats],
        'mode': 'lines+markers',
        'name': 'RAM libre',
        'line': {'color': '#3498db', 'width': 2},
        'marker': {'size': 6},
    }]

    scatter_traces = []
    for sat in unique_sats:
        items = cap_by_sat[sat]
        scatter_traces.append({
            'x': [i['rssi'] for i in items],
            'y': [i['snr'] for i in items],
            'mode': 'markers',
            'name': sat,
            'marker': {'color': get_color(sat), 'size': 12, 'opacity': 0.8},
            'text': [i['datetime'] for i in items],
        })

    # --- Timeline de pases programados (Gantt-style) ---
    pass_timeline_traces = []
    pass_by_sat = defaultdict(list)
    for p in daily_passes:
        pass_by_sat[p['satellite']].append(p)

    timeline_sats = sorted(pass_by_sat.keys())
    for idx, sat in enumerate(timeline_sats):
        items = sorted(pass_by_sat[sat], key=lambda x: x['start'])
        for p in items:
            pass_timeline_traces.append({
                'x': [p['start'], p['end']],
                'y': [sat, sat],
                'mode': 'lines',
                'line': {'color': get_color(sat), 'width': 14},
                'showlegend': False,
                'hoverinfo': 'text',
                'text': f"{sat}<br>{p['start'][11:16]} → {p['end'][11:16]}<br>Elev: {p['elevation']}°<br>Frec: {p['freq_mhz']:.3f} MHz",
            })

    # --- Datos para tablas ---

    cap_rows = []
    for c in sorted(captures, key=lambda x: x['datetime'], reverse=True)[:50]:
        cap_rows.append([
            c['satellite'], c['datetime'],
            f"{c['rssi']:.1f}", f"{c['snr']:.1f}",
            f"{c['freq']:.3f}", str(c['sf']), f"{c['bw']}",
            str(c['cr']), c['mode'],
            c['data'][:40] + ('...' if len(c['data']) > 40 else '')
        ])

    hb_rows = []
    for h in sorted(heartbeats, key=lambda x: x['datetime'], reverse=True)[:50]:
        hb_rows.append([
            h['datetime'], h['mode'],
            h['satellite'] if h['satellite'] != '-' else '—',
            f"{h['freq']:.3f}", str(h['sf']), f"{h['bw']}",
            str(h['cr']), h['elevation'],
            f"{h['temp']:.1f}", str(h['ram']),
            str(h['irq']), h['fan']
        ])

    pass_rows = []
    for p in sorted(daily_passes, key=lambda x: x['start'], reverse=True):
        pass_rows.append([
            p['date'],
            p['start'][11:16],
            p['end'][11:16],
            p['satellite'],
            str(p['elevation']),
            f"{p['freq_mhz']:.3f}",
        ])

    plotly_data = {
        'rssi': rssi_traces,
        'snr': snr_traces,
        'bar': bar_trace,
        'elevation': elev_traces,
        'temp': temp_trace,
        'ram': ram_trace,
        'scatter': scatter_traces,
        'pass_timeline': pass_timeline_traces,
    }

    sat_stats = []
    for sat in unique_sats:
        items = cap_by_sat[sat]
        rssis = [i['rssi'] for i in items]
        snrs = [i['snr'] for i in items]
        sat_stats.append({
            'name': sat,
            'count': len(items),
            'color': get_color(sat),
            'rssi_min': min(rssis), 'rssi_max': max(rssis),
            'rssi_avg': sum(rssis) / len(rssis),
            'snr_min': min(snrs), 'snr_max': max(snrs),
            'snr_avg': sum(snrs) / len(snrs),
            'freq': items[0]['freq'],
            'sf': items[0]['sf'],
            'bw': items[0]['bw'],
        })

    # Estadísticas de pases programados
    pass_stats = []
    for sat in sorted(pass_by_sat.keys()):
        items = pass_by_sat[sat]
        elevs = [i['elevation'] for i in items]
        pass_stats.append({
            'name': sat,
            'count': len(items),
            'color': get_color(sat),
            'elev_min': min(elevs),
            'elev_max': max(elevs),
            'elev_avg': sum(elevs) / len(elevs),
        })

    latest_sys = systems[-1] if systems else None

    parts = []
    def hp(s):
        parts.append(s)

    hp('<!DOCTYPE html>')
    hp('<html lang="es">')
    hp('<head>')
    hp('<meta charset="UTF-8">')
    hp('<meta name="viewport" content="width=device-width, initial-scale=1.0">')
    hp('<title>ITV LEO V9.1 - Dashboard de Capturas</title>')
    hp('<script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>')
    hp('<style>')
    hp(':root { --bg: #0f172a; --card: #1e293b; --card-hover: #334155; --text: #e2e8f0; --text-dim: #94a3b8; --accent: #38bdf8; --border: #334155; }')
    hp('* { margin: 0; padding: 0; box-sizing: border-box; }')
    hp('body { font-family: "Segoe UI", system-ui, -apple-system, sans-serif; background: var(--bg); color: var(--text); line-height: 1.5; padding: 20px; }')
    hp('.header { text-align: center; margin-bottom: 30px; padding-bottom: 20px; border-bottom: 2px solid var(--border); }')
    hp('.header h1 { font-size: 2rem; color: var(--accent); margin-bottom: 8px; }')
    hp('.header p { color: var(--text-dim); font-size: 0.95rem; }')
    hp('.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; margin-bottom: 30px; }')
    hp('.card { background: var(--card); border-radius: 12px; padding: 20px; border: 1px solid var(--border); transition: transform 0.2s, background 0.2s; }')
    hp('.card:hover { background: var(--card-hover); transform: translateY(-2px); }')
    hp('.card-header { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }')
    hp('.dot { width: 14px; height: 14px; border-radius: 50%; flex-shrink: 0; }')
    hp('.card-title { font-size: 1.1rem; font-weight: 600; }')
    hp('.card-sub { color: var(--text-dim); font-size: 0.85rem; margin-bottom: 10px; }')
    hp('.stat-row { display: flex; justify-content: space-between; padding: 4px 0; font-size: 0.9rem; border-bottom: 1px solid rgba(255,255,255,0.05); }')
    hp('.stat-row:last-child { border-bottom: none; }')
    hp('.stat-label { color: var(--text-dim); }')
    hp('.stat-value { font-weight: 600; font-family: "SF Mono", monospace; }')
    hp('.summary-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-bottom: 30px; }')
    hp('.summary-item { background: var(--card); border-radius: 10px; padding: 16px; text-align: center; border: 1px solid var(--border); }')
    hp('.summary-value { font-size: 1.8rem; font-weight: 700; color: var(--accent); font-family: "SF Mono", monospace; }')
    hp('.summary-label { color: var(--text-dim); font-size: 0.85rem; margin-top: 4px; }')
    hp('.chart-container { background: var(--card); border-radius: 12px; padding: 20px; margin-bottom: 24px; border: 1px solid var(--border); }')
    hp('.chart-title { font-size: 1.15rem; font-weight: 600; margin-bottom: 12px; color: var(--accent); }')
    hp('.table-container { background: var(--card); border-radius: 12px; padding: 20px; margin-bottom: 24px; border: 1px solid var(--border); overflow-x: auto; }')
    hp('table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }')
    hp('th { text-align: left; padding: 10px 8px; color: var(--accent); border-bottom: 2px solid var(--border); font-weight: 600; white-space: nowrap; }')
    hp('td { padding: 8px; border-bottom: 1px solid rgba(255,255,255,0.05); font-family: "SF Mono", monospace; font-size: 0.8rem; }')
    hp('tr:hover td { background: rgba(255,255,255,0.03); }')
    hp('.badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: 600; font-family: sans-serif; }')
    hp('.footer { text-align: center; color: var(--text-dim); font-size: 0.8rem; margin-top: 20px; padding-top: 20px; border-top: 1px solid var(--border); }')
    hp('@media (max-width: 600px) { body { padding: 10px; } .header h1 { font-size: 1.4rem; } }')
    hp('</style>')
    hp('</head>')
    hp('<body>')

    hp('<div class="header">')
    hp('<h1>&#128752; ITV LEO V9.1 - Dashboard de Capturas</h1>')
    hp(f'<p>Periodo: {date_min} &rarr; {date_max} | Sistema ITV LEO en techo</p>')
    hp('</div>')

    cpu_temp_str = f"{latest_sys['cpu_temp']:.1f}" if latest_sys else "N/A"
    hp('<div class="summary-grid">')
    hp(f'<div class="summary-item"><div class="summary-value">{total_captures}</div><div class="summary-label">Paquetes Capturados</div></div>')
    hp(f'<div class="summary-item"><div class="summary-value">{len(unique_sats)}</div><div class="summary-label">Satelites Detectados</div></div>')
    hp(f'<div class="summary-item"><div class="summary-value">{total_hbs}</div><div class="summary-label">Heartbeats Totales</div></div>')
    hp(f'<div class="summary-item"><div class="summary-value">{total_pase}</div><div class="summary-label">Heartbeats PASE</div></div>')
    hp(f'<div class="summary-item"><div class="summary-value">{total_base}</div><div class="summary-label">Heartbeats BASE</div></div>')
    hp(f'<div class="summary-item"><div class="summary-value">{cpu_temp_str}C</div><div class="summary-label">Ultima Temp. CPU</div></div>')
    if total_daily_passes > 0:
        hp(f'<div class="summary-item"><div class="summary-value">{total_daily_passes}</div><div class="summary-label">Pases Programados</div></div>')
    hp('</div>')

    hp('<h2 style="color:var(--accent); margin-bottom:16px; font-size:1.3rem;">&#128202; Estadisticas por Satelite (Capturas)</h2>')
    hp('<div class="grid">')
    for stat in sat_stats:
        hp('<div class="card">')
        hp(f'<div class="card-header"><div class="dot" style="background:{stat["color"]}"></div><div class="card-title">{stat["name"]}</div></div>')
        hp(f'<div class="card-sub">{stat["freq"]:.3f} MHz | SF{stat["sf"]} | BW{stat["bw"]} kHz</div>')
        hp(f'<div class="stat-row"><span class="stat-label">Capturas</span><span class="stat-value">{stat["count"]}</span></div>')
        hp(f'<div class="stat-row"><span class="stat-label">RSSI medio</span><span class="stat-value">{stat["rssi_avg"]:.1f} dBm</span></div>')
        hp(f'<div class="stat-row"><span class="stat-label">RSSI rango</span><span class="stat-value">{stat["rssi_min"]:.1f} &rarr; {stat["rssi_max"]:.1f}</span></div>')
        hp(f'<div class="stat-row"><span class="stat-label">SNR medio</span><span class="stat-value">{stat["snr_avg"]:.1f} dB</span></div>')
        hp(f'<div class="stat-row"><span class="stat-label">SNR rango</span><span class="stat-value">{stat["snr_min"]:.1f} &rarr; {stat["snr_max"]:.1f}</span></div>')
        hp('</div>')
    hp('</div>')

    if pass_stats:
        hp('<h2 style="color:var(--accent); margin-bottom:16px; font-size:1.3rem;">&#128197; Estadisticas de Pases Programados</h2>')
        hp('<div class="grid">')
        for stat in pass_stats:
            hp('<div class="card">')
            hp(f'<div class="card-header"><div class="dot" style="background:{stat["color"]}"></div><div class="card-title">{stat["name"]}</div></div>')
            hp(f'<div class="stat-row"><span class="stat-label">Pases programados</span><span class="stat-value">{stat["count"]}</span></div>')
            hp(f'<div class="stat-row"><span class="stat-label">Elev. media</span><span class="stat-value">{stat["elev_avg"]:.0f}°</span></div>')
            hp(f'<div class="stat-row"><span class="stat-label">Elev. rango</span><span class="stat-value">{stat["elev_min"]}° &rarr; {stat["elev_max"]}°</span></div>')
            hp('</div>')
        hp('</div>')

    hp('<h2 style="color:var(--accent); margin-bottom:16px; font-size:1.3rem;">&#128200; Graficos</h2>')

    charts = [
        ('chart-rssi', 'RSSI vs Tiempo (por satelite)', 'RSSI (dBm)'),
        ('chart-snr', 'SNR vs Tiempo (por satelite)', 'SNR (dB)'),
        ('chart-bar', 'Paquetes Capturados por Satelite', 'N de paquetes'),
        ('chart-elevation', 'Elevacion del Pase vs Tiempo', 'Elevacion (grados)'),
        ('chart-scatter', 'RSSI vs SNR (scatter por satelite)', 'SNR (dB)'),
    ]
    for cid, title, ytitle in charts:
        hp(f'<div class="chart-container"><div class="chart-title">{title}</div><div id="{cid}" style="width:100%; height:420px;"></div></div>')

    if pass_timeline_traces:
        hp('<div class="chart-container"><div class="chart-title">Timeline de Pases Programados (Agenda)</div><div id="chart-passes" style="width:100%; height:320px;"></div></div>')

    hp('<div style="display:grid; grid-template-columns: 1fr 1fr; gap: 20px;">')
    hp('<div class="chart-container"><div class="chart-title">Temperatura CPU vs Tiempo</div><div id="chart-temp" style="width:100%; height:320px;"></div></div>')
    hp('<div class="chart-container"><div class="chart-title">RAM Libre vs Tiempo</div><div id="chart-ram" style="width:100%; height:320px;"></div></div>')
    hp('</div>')

    hp('<h2 style="color:var(--accent); margin:24px 0 16px; font-size:1.3rem;">&#128203; Tablas de Datos</h2>')

    if pass_rows:
        hp('<div class="table-container"><div class="chart-title">Pases Programados (Agenda)</div>')
        hp('<table><thead><tr><th>Fecha</th><th>Inicio</th><th>Fin</th><th>Satelite</th><th>Elev. Max</th><th>Frec MHz</th></tr></thead><tbody>')
        for row in pass_rows:
            sat_color = get_color(row[3])
            hp(f'<tr><td>{row[0]}</td><td>{row[1]}</td><td>{row[2]}</td>')
            hp(f'<td><span class="badge" style="background:{sat_color}22; color:{sat_color}; border:1px solid {sat_color}44;">{row[3]}</span></td>')
            hp(f'<td>{row[4]}°</td><td>{row[5]}</td></tr>')
        hp('</tbody></table></div>')

    hp('<div class="table-container"><div class="chart-title">Ultimas 50 Capturas</div>')
    hp('<table><thead><tr><th>Satelite</th><th>Hora</th><th>RSSI</th><th>SNR</th><th>Frec MHz</th><th>SF</th><th>BW</th><th>CR</th><th>Modo</th><th>Data hex</th></tr></thead><tbody>')
    for row in cap_rows:
        sat_color = get_color(row[0])
        hp(f'<tr><td><span class="badge" style="background:{sat_color}22; color:{sat_color}; border:1px solid {sat_color}44;">{row[0]}</span></td>')
        hp(f'<td>{row[1]}</td><td>{row[2]}</td><td>{row[3]}</td><td>{row[4]}</td><td>{row[5]}</td><td>{row[6]}</td><td>{row[7]}</td><td>{row[8]}</td>')
        hp(f'<td style="max-width:200px; overflow:hidden; text-overflow:ellipsis;">{row[9]}</td></tr>')
    hp('</tbody></table></div>')

    hp('<div class="table-container"><div class="chart-title">Ultimos 50 Heartbeats</div>')
    hp('<table><thead><tr><th>Hora</th><th>Modo</th><th>Satelite</th><th>Frec MHz</th><th>SF</th><th>BW</th><th>CR</th><th>Elev</th><th>Temp C</th><th>RAM</th><th>IRQ</th><th>Vent</th></tr></thead><tbody>')
    for row in hb_rows:
        mode_color = '#2ecc71' if row[1] == 'PASE' else '#7f8c8d'
        hp(f'<tr><td>{row[0]}</td><td><span class="badge" style="background:{mode_color}22; color:{mode_color}; border:1px solid {mode_color}44;">{row[1]}</span></td>')
        hp(f'<td>{row[2]}</td><td>{row[3]}</td><td>{row[4]}</td><td>{row[5]}</td><td>{row[6]}</td><td>{row[7]}</td><td>{row[8]}</td><td>{row[9]}</td><td>{row[10]}</td><td>{row[11]}</td></tr>')
    hp('</tbody></table></div>')

    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    hp(f'<div class="footer">Generado el {now_str} | ITV LEO V9.1 Visualizador | Datos parseados: {total_captures} capturas, {total_hbs} heartbeats, {total_daily_passes} pases programados</div>')

    hp('<script>')
    hp('const plotlyConfig = { responsive: true, displayModeBar: true, displaylogo: false };')
    hp('')
    hp('const plotlyLayout = {')
    hp("    paper_bgcolor: 'rgba(0,0,0,0',")
    hp("    plot_bgcolor: 'rgba(0,0,0,0)',")
    hp("    font: { color: '#e2e8f0', family: 'Segoe UI, sans-serif' },")
    hp("    xaxis: { gridcolor: 'rgba(255,255,255,0.08)', linecolor: 'rgba(255,255,255,0.15)' },")
    hp("    yaxis: { gridcolor: 'rgba(255,255,255,0.08)', linecolor: 'rgba(255,255,255,0.15)' },")
    hp("    legend: { bgcolor: 'rgba(30,41,59,0.8)', bordercolor: 'rgba(255,255,255,0.1)', borderwidth: 1 },")
    hp('    margin: { t: 30, r: 20, b: 50, l: 60 }')
    hp('};')
    hp(f'const data = {json.dumps(plotly_data)};')
    hp('')
    hp("Plotly.newPlot('chart-rssi', data.rssi, {...plotlyLayout, yaxis: {...plotlyLayout.yaxis, title: 'RSSI (dBm)'}, xaxis: {...plotlyLayout.xaxis, title: 'Hora'}}, plotlyConfig);")
    hp("Plotly.newPlot('chart-snr', data.snr, {...plotlyLayout, yaxis: {...plotlyLayout.yaxis, title: 'SNR (dB)'}, xaxis: {...plotlyLayout.xaxis, title: 'Hora'}}, plotlyConfig);")
    hp("Plotly.newPlot('chart-bar', data.bar, {...plotlyLayout, yaxis: {...plotlyLayout.yaxis, title: 'N de paquetes'}, xaxis: {...plotlyLayout.xaxis, title: 'Satelite'}}, plotlyConfig);")
    hp("Plotly.newPlot('chart-elevation', data.elevation, {...plotlyLayout, yaxis: {...plotlyLayout.yaxis, title: 'Elevacion (grados)', range: [0, 100]}, xaxis: {...plotlyLayout.xaxis, title: 'Hora'}}, plotlyConfig);")
    hp("Plotly.newPlot('chart-scatter', data.scatter, {...plotlyLayout, yaxis: {...plotlyLayout.yaxis, title: 'SNR (dB)'}, xaxis: {...plotlyLayout.xaxis, title: 'RSSI (dBm)'}}, plotlyConfig);")
    hp("Plotly.newPlot('chart-temp', data.temp, {...plotlyLayout, yaxis: {...plotlyLayout.yaxis, title: 'Temperatura (C)'}, xaxis: {...plotlyLayout.xaxis, title: 'Hora'}, showlegend: false}, plotlyConfig);")
    hp("Plotly.newPlot('chart-ram', data.ram, {...plotlyLayout, yaxis: {...plotlyLayout.yaxis, title: 'RAM libre (bytes)'}, xaxis: {...plotlyLayout.xaxis, title: 'Hora'}, showlegend: false}, plotlyConfig);")
    if pass_timeline_traces:
        hp("Plotly.newPlot('chart-passes', data.pass_timeline, {...plotlyLayout, yaxis: {...plotlyLayout.yaxis, title: 'Satelite', autorange: 'reversed'}, xaxis: {...plotlyLayout.xaxis, title: 'Hora'}, showlegend: false, hovermode: 'closest'}, plotlyConfig);")
    hp('</script>')
    hp('</body>')
    hp('</html>')

    with open(outfile, 'w', encoding='utf-8') as f2:
        f2.write('\n'.join(parts))
    print(f"HTML generado: {os.path.abspath(outfile)}")


def main():
    if len(sys.argv) < 2:
        print("Uso: python leo_visualizer.py <fichero_datos.txt>")
        print("Ejemplo: python leo_visualizer.py Datos.txt")
        sys.exit(1)

    infile = sys.argv[1]
    if not os.path.exists(infile):
        print(f"Error: no se encuentra '{infile}'")
        sys.exit(1)

    outfile = os.path.join(os.getcwd(), os.path.basename(os.path.splitext(infile)[0]) + '.html')

    print(f"Leyendo: {infile}")
    heartbeats, captures, metas, systems, daily_passes = parse_file(infile)
    print(f"   Heartbeats V9.1 validos: {len(heartbeats)}")
    print(f"   Capturas: {len(captures)}")
    print(f"   Estados sistema: {len(systems)}")
    print(f"   Pases programados: {len(daily_passes)}")
    if not heartbeats and not captures and not daily_passes:
        print("No se encontraron datos V9.1. El fichero tiene el formato correcto?")
        sys.exit(1)
    print("Generando HTML...")
    generate_html(heartbeats, captures, metas, systems, daily_passes, outfile)


if __name__ == '__main__':
    main()
