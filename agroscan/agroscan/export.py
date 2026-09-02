# -*- coding: utf-8 -*-
"""Обменные форматы для кадастрового инженера.

PDF показывает результат, но взять из него геометрию нельзя. Здесь она
выдаётся в том виде, в каком её ждут в работе: DXF и MIF/MID для загрузки
в чертёж, каталог координат для межевого плана, GeoJSON для QGIS.

Все выгрузки строятся из одного нормализованного набора колец, поэтому
каталог, DXF и MIF не могут разойтись между собой.
"""
import csv
import json
import math
import os
import numpy as np

from .geo import Local

def normalize(rings):
    """Единый обход и стартовая точка: по часовой стрелке от самой северной.

    Без этого при каждом перезапуске каталог координат начинается с другой
    точки и его нельзя сравнить с предыдущей версией.
    """
    out = []
    for r in rings:
        a = np.asarray(r, float)
        if len(a) > 1 and np.allclose(a[0], a[-1]):
            a = a[:-1]
        s = float(np.sum(a[:, 0] * np.roll(a[:, 1], -1) - np.roll(a[:, 0], -1) * a[:, 1]))
        if s > 0:                      # против часовой → развернуть
            a = a[::-1]
        i = int(np.lexsort((a[:, 0], -a[:, 1]))[0])    # самая северная, при равенстве западная
        out.append(np.roll(a, -i, axis=0))
    return out

def simplify(rings, tol_m):
    if not tol_m:
        return rings
    from shapely.geometry import Polygon
    out = []
    for r in rings:
        p = Polygon(r).buffer(0).simplify(tol_m, preserve_topology=True)
        if p.is_empty:
            continue
        out.append(np.asarray(p.exterior.coords[:-1]))
    return out

def _layers(parts, prefix='ЧЗУ_'):
    for k in sorted(parts):
        yield prefix + str(k), parts[k]

# ── DXF (R12 ASCII: читается всеми CAD, зависимостей не требует) ────────
def _dxf_polyline(f, layer, ring, closed=True):
    f.write('0\nPOLYLINE\n8\n%s\n66\n1\n70\n%d\n' % (layer, 1 if closed else 0))
    for x, y in ring:
        f.write('0\nVERTEX\n8\n%s\n10\n%.3f\n20\n%.3f\n' % (layer, x, y))
    f.write('0\nSEQEND\n8\n%s\n' % layer)

def dxf(path, rings, parts, tol_m=0.0):
    layers = [('Granica_ZU', [normalize(rings)[0]]), ('Vyrezy_ZU', normalize(rings)[1:])]
    for name, p in _layers(parts):
        name = name.replace('ЧЗУ_', 'CHZU_')      # имена слоёв латиницей: DXF R12 не любит кириллицу
        layers.append((name, simplify(normalize(p['outer']), tol_m)))
        if p.get('inner'):
            layers.append((name + '_holes', simplify(normalize(p['inner']), tol_m)))
    with open(path, 'w', encoding='cp1251', errors='replace') as f:
        f.write('0\nSECTION\n2\nTABLES\n0\nTABLE\n2\nLAYER\n70\n%d\n' % len(layers))
        for i, (name, _) in enumerate(layers):
            f.write('0\nLAYER\n2\n%s\n70\n0\n62\n%d\n6\nCONTINUOUS\n' % (name, 1 + i % 7))
        f.write('0\nENDTAB\n0\nENDSEC\n0\nSECTION\n2\nENTITIES\n')
        for name, rs in layers:
            for r in rs:
                _dxf_polyline(f, name, r)
        f.write('0\nENDSEC\n0\nEOF\n')
    return path

# ── MIF/MID для MapInfo ─────────────────────────────────────────────────
def mif(path, rings, parts, zone, tol_m=0.0):
    base = os.path.splitext(path)[0]
    items = [('Граница ЗУ', normalize(rings)[0], 0.0)]
    for name, p in _layers(parts):
        for r in simplify(normalize(p['outer']), tol_m):
            items.append((name, r, p['areaHa']))
    with open(base + '.mif', 'w', encoding='cp1251', errors='replace') as f:
        f.write('Version 300\nCharset "WindowsCyrillic"\nDelimiter ","\n')
        f.write('CoordSys Earth Projection 8, 1001, "m", 46.05, 0, 1, 2300000, -5514743.504\n')
        f.write('Columns 2\n  SLOI char(32)\n  PLOSHAD_GA float\nData\n\n')
        for name, r, ha in items:
            f.write('Region 1\n  %d\n' % len(r))
            for x, y in r:
                f.write('%.3f %.3f\n' % (x, y))
            f.write('    Pen (1,2,0)\n')
    with open(base + '.mid', 'w', encoding='cp1251', errors='replace') as f:
        for name, r, ha in items:
            f.write('"%s",%.4f\n' % (name, ha))
    return base + '.mif'

# ── каталог координат ───────────────────────────────────────────────────
def catalog(path, parts, zone, tol_m=0.0, xlsx=True):
    """Номер точки, X (север), Y (восток), длина линии, дирекционный угол.

    В кадастре X — север, Y — восток; во внутреннем представлении наоборот,
    поэтому здесь колонки меняются местами ровно один раз.
    """
    loc = Local(zone)
    rows = [('Часть', 'Тип контура', 'Контур', 'Точка', 'X, м', 'Y, м', 'Длина линии, м',
             'Дирекционный угол', 'Широта', 'Долгота')]
    for name, p in _layers(parts):
        # вырезы обязаны попасть в каталог: без них площадь по координатам
        # расходится с ведомостью (на 1173 ЧЗУ/3 давала 53,02 вместо 41,65 га)
        groups = [('внешний', simplify(normalize(p['outer']), tol_m)),
                  ('вырез', simplify(normalize(p.get('inner', [])), tol_m))]
        ci = 0
        for kind, rr in groups:
            for r in rr:
                ci += 1
                wgs = loc.to_wgs([(x, y) for x, y in r])
                for i, (e, n) in enumerate(r):
                    e2, n2 = r[(i + 1) % len(r)]
                    d = math.hypot(e2 - e, n2 - n)
                    ang = math.degrees(math.atan2(e2 - e, n2 - n)) % 360
                    rows.append((name, kind, ci, i + 1, round(n, 2), round(e, 2), round(d, 2),
                                 '%d°%02d\'%02d"' % (int(ang), int(ang % 1 * 60),
                                                     int(ang * 3600 % 60)),
                                 round(wgs[i][1], 7), round(wgs[i][0], 7)))
    base = os.path.splitext(path)[0]
    with open(base + '.csv', 'w', encoding='cp1251', errors='replace', newline='') as f:
        csv.writer(f, delimiter=';').writerows(rows)
    made = [base + '.csv']
    if xlsx:
        try:
            import openpyxl
            wb = openpyxl.Workbook(); ws = wb.active; ws.title = 'Каталог координат'
            for r in rows:
                ws.append(list(r))
            for i, w in enumerate((10, 12, 8, 8, 14, 14, 16, 18, 13, 13), 1):
                ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w
            ws.freeze_panes = 'A2'
            wb.save(base + '.xlsx'); made.append(base + '.xlsx')
        except ImportError:
            pass
    return made, len(rows) - 1

def areas_table(path, parts, egrn_ha):
    """Ведомость площадей частей."""
    rows = [('Обозначение', 'Наименование', 'Площадь, м²', 'Площадь, га', 'Доля, %', 'Контуров')]
    for name, p in _layers(parts, prefix='ЧЗУ/'):
        rows.append((name, p.get('название', ''), int(round(p['areaHa'] * 10000)),
                     round(p['areaHa'], 4), round(100 * p['areaHa'] / egrn_ha, 2),
                     len(p['outer'])))
    tot = sum(p['areaHa'] for _, p in _layers(parts))
    rows.append(('Итого', 'площадь ЗУ по сведениям ЕГРН', int(round(tot * 10000)),
                 round(tot, 4), round(100 * tot / egrn_ha, 2), ''))
    with open(path, 'w', encoding='cp1251', errors='replace', newline='') as f:
        csv.writer(f, delimiter=';').writerows(rows)
    return path

# ── GeoJSON ─────────────────────────────────────────────────────────────
def geojson(path, rings, parts, zone, wgs=False):
    loc = Local(zone)
    conv = (lambda r: [list(map(float, p)) for p in loc.to_wgs(r)]) if wgs else \
           (lambda r: [list(map(float, p)) for p in r])
    feats = [{'type': 'Feature', 'properties': {'слой': 'Граница ЗУ'},
              'geometry': {'type': 'Polygon',
                           'coordinates': [conv(r) + [conv(r)[0]] for r in normalize(rings)]}}]
    for name, p in _layers(parts, prefix='ЧЗУ/'):
        polys = [[conv(r) + [conv(r)[0]]] for r in normalize(p['outer'])]
        for h in normalize(p.get('inner', [])):
            polys[0].append(conv(h) + [conv(h)[0]])
        feats.append({'type': 'Feature',
                      'properties': {'слой': name, 'площадь_га': round(p['areaHa'], 4),
                                     'название': p.get('название', '')},
                      'geometry': {'type': 'MultiPolygon', 'coordinates': polys}})
    fc = {'type': 'FeatureCollection', 'features': feats}
    if wgs:
        fc['crs'] = {'type': 'name', 'properties': {'name': 'urn:ogc:def:crs:OGC:1.3:CRS84'}}
    json.dump(fc, open(path, 'w', encoding='utf-8'), ensure_ascii=False)
    return path

def all_formats(out_dir, kn, rings, parts, egrn_ha, zone, tol_m=0.0, prefix=None):
    """Обменные форматы комплекта. prefix — «<КН>_<вид работ>» для имён файлов."""
    pre = prefix or kn.replace(':', '-')
    P = lambda name: os.path.join(out_dir, '%s_%s' % (pre, name))
    made = {}
    made['dxf'] = dxf(P('Границы_частей.dxf'), rings, parts, tol_m)
    made['mif'] = mif(P('Границы_частей.mif'), rings, parts, zone, tol_m)
    cat, npts = catalog(P('Каталог_координат.csv'), parts, zone, tol_m)
    made['каталог'] = cat; made['точек'] = npts
    made['ведомость'] = areas_table(P('Ведомость_площадей.csv'), parts, egrn_ha)
    made['geojson_msk'] = geojson(P('Границы_частей_msk.geojson'), rings, parts, zone)
    made['geojson_wgs'] = geojson(P('Границы_частей_wgs84.geojson'), rings, parts, zone, wgs=True)
    return made
