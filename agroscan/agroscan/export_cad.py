# -*- coding: utf-8 -*-
"""Схема ЧЗУ как чертёж AutoCAD: модель — местность, лист — документ.

В обменных форматах до сих пор уезжали только границы частей. Владелец
работает в AutoCAD и просил саму схему: с рамкой, заголовком, условными
обозначениями, штампом и подложкой — чтобы открыть и печатать, а не
собирать лист заново.

Устройство обычное для кадастрового чертежа: в модели всё в метрах местной
СК (растр, границы, штриховки, подписи), а на листе A4 — рамка, таблицы и
видовой экран с картой в круглом масштабе. Высота текста в модели считается
из высоты на бумаге и знаменателя масштаба, поэтому при печати выходит
ровно то, что на PDF-схеме.
"""
import math
import os

from . import areas
from .export import DXF_HATCH, DXF_RGB, normalize, put_image, simplify
from .geo import Local
from .sheets.schema import LEGEND, extreme_points

# круглые знаменатели: инженер ждёт 1:2000, а не 1:2907
SCALES = (500, 1000, 2000, 2500, 3000, 4000, 5000, 10000, 12500, 15000, 20000, 25000, 50000)
SHEET = (297.0, 210.0)          # A4 альбомная, мм
SHEET_RGB = {'Ramka': (0, 0, 0), 'Zagolovok': (0, 0, 0), 'Legenda': (0, 0, 0),
             'Shtamp': (0, 0, 0), 'Kompas': (90, 90, 90)}
TITLE = ('Схема расположения земельного участка сельскохозяйственного назначения '
         'с кадастровым номером %s и контуров частей земельного участка, покрытых '
         'древесной и кустарниковой растительностью, на которых планируется '
         'проведение культуртехнических мероприятий')

def wrap_chars(text, chars):
    """Перенос по числу знаков: в DXF ширину строки не измерить, а заголовок
    в одну строку вылезал за рамку листа."""
    out, cur = [], ''
    for w in text.split():
        probe = (cur + ' ' + w).strip()
        if cur and len(probe) > chars:
            out.append(cur); cur = w
        else:
            cur = probe
    if cur:
        out.append(cur)
    return out

def scale_for(rings, w_mm, h_mm, pad=1.12):
    """Круглый знаменатель масштаба, при котором участок влезает в окно."""
    E = [p[0] for r in rings for p in r]; N = [p[1] for r in rings for p in r]
    need = max((max(E) - min(E)) / max(w_mm, 1), (max(N) - min(N)) / max(h_mm, 1)) * 1000 * pad
    for d in SCALES:
        if d >= need:
            return d
    return int(round(need / 1000.0) * 1000)

def _txt(space, text, xy, height, layer, color=None, align='MIDDLE_CENTER', rotation=0):
    from ezdxf.enums import TextEntityAlignment
    a = {'layer': layer, 'height': height}
    if rotation:
        a['rotation'] = rotation
    t = space.add_text(text, dxfattribs=a)
    t.set_placement(xy, align=getattr(TextEntityAlignment, align))
    if color:
        t.rgb = color
    return t

def _fill(space, x0, y0, x1, y1, layer):
    """Маска под таблицей: карта не должна просвечивать сквозь строки.

    Именно WIPEOUT, а не залитый прямоугольник: у SOLID цвет берётся от слоя
    и на листе выходил чёрный прямоугольник поверх таблицы.
    """
    return space.add_wipeout([(x0, y0), (x1, y0), (x1, y1), (x0, y1)],
                             dxfattribs={'layer': layer})

def _rect(space, x0, y0, x1, y1, layer, lw=None):
    p = space.add_lwpolyline([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], close=True,
                             dxfattribs={'layer': layer})
    if lw:
        p.dxf.const_width = lw
    return p

def _model(msp, doc, kn, rings, parts, zone, meta, image_name, neighbors, place, den):
    """Местность: растр, границы, части, подписи — всё в метрах МСК."""
    mm = den / 1000.0                      # миллиметр бумаги в метрах местности
    if image_name and meta:
        idef = doc.add_image_def(filename=image_name, size_in_pixel=(meta['W'], meta['H']))
        msp.add_image(idef, insert=(meta['e0'], meta['n0']),
                      size_in_units=(meta['e1'] - meta['e0'], meta['n1'] - meta['n0']),
                      dxfattribs={'layer': 'Podlozhka'})
        doc.set_raster_variables(frame=0, quality=1, units='m')

    poly = lambda r, layer, w: msp.add_lwpolyline(
        [(float(x), float(y)) for x, y in r], close=True,
        dxfattribs={'layer': layer, 'const_width': w * mm})
    # смежники: подписываем только крупные, как на схеме. Без этого на 1173
    # три сотни номеров ложились друг на друга сплошной кашей
    E0 = [p[0] for r in rings for p in r]; N0 = [p[1] for r in rings for p in r]
    span = max(max(E0) - min(E0), max(N0) - min(N0)) * 1.6
    cx0, cy0 = (max(E0) + min(E0)) / 2, (max(N0) + min(N0)) / 2
    area = lambda r: abs(sum(r[i][0] * r[(i + 1) % len(r)][1] - r[(i + 1) % len(r)][0] * r[i][1]
                             for i in range(len(r))) / 2)
    for nb in (neighbors or []):
        for r in nb.get('rings', []):
            poly(r, 'Smezhnye', 0.0)
        big = max(nb.get('rings') or [[]], key=len)
        if len(big) < 3 or not nb.get('kn') or area(big) < 9000:
            continue
        c = (sum(q[0] for q in big) / len(big), sum(q[1] for q in big) / len(big))
        if abs(c[0] - cx0) > span or abs(c[1] - cy0) > span:
            continue                       # за пределами видового экрана
        _txt(msp, nb['kn'], c, 2.0 * mm, 'Smezhnye', DXF_RGB['Smezhnye'])

    for k in sorted(parts):
        layer = 'CHZU_%s' % k
        if layer not in DXF_RGB:
            layer = 'Podpisi'
        outer = normalize(parts[k]['outer']); inner = normalize(parts[k].get('inner', []))
        if DXF_HATCH.get(layer) and outer:
            h = msp.add_hatch(dxfattribs={'layer': layer})
            h.set_pattern_fill(DXF_HATCH[layer], scale=3.0 * mm)
            h.rgb = DXF_RGB[layer]
            for r in outer:
                h.paths.add_polyline_path([(float(x), float(y)) for x, y in r], is_closed=True)
            for r in inner:
                h.paths.add_polyline_path([(float(x), float(y)) for x, y in r], is_closed=True,
                                          flags=0)
        for r in outer + inner:
            poly(r, layer, 0.40)
        if outer:
            from shapely.geometry import Polygon
            big = max(outer, key=lambda r: Polygon(r).area)
            c = Polygon(big).buffer(0).representative_point()
            _txt(msp, 'ЧЗУ/%s' % k, (c.x, c.y), 2.7 * mm, 'Podpisi')

    for r in normalize(rings):
        poly(r, 'Granica_ZU', 0.70)
    E = [p[0] for r in rings for p in r]; N = [p[1] for r in rings for p in r]
    ce, cn = (max(E) + min(E)) / 2, (max(N) + min(N)) / 2
    _txt(msp, kn, (ce, cn), 2.9 * mm, 'Podpisi')

    # координаты характерных точек — широта и долгота, как на схеме
    loc = Local(zone)
    out = rings[0]
    for j, i in enumerate(extreme_points(out)):
        e, n = out[i]
        lon, lat = loc.to_wgs([(e, n)])[0]
        dx = (1 if e > ce else -1) * 14 * mm
        dy = (1 if n > cn else -1) * 10 * mm
        msp.add_line((e, n), (e + dx, n + dy), dxfattribs={'layer': 'Podpisi'})
        msp.add_line((e + dx - 9 * mm, n + dy), (e + dx + 9 * mm, n + dy),
                     dxfattribs={'layer': 'Podpisi'})
        _txt(msp, '%.7f' % lat, (e + dx, n + dy + 2.0 * mm), 2.5 * mm, 'Podpisi')
        _txt(msp, '%.7f' % lon, (e + dx, n + dy - 2.0 * mm), 2.5 * mm, 'Podpisi')

    if place:
        te, tn = loc.from_wgs([(place['lon'], place['lat'])])[0]
        vx, vy = te - ce, tn - cn
        ln = math.hypot(vx, vy) or 1.0
        vx, vy = vx / ln, vy / ln
        r0 = max(math.hypot(q[0] - ce, q[1] - cn) for r in rings for q in r) + 6 * mm
        a0 = (ce + vx * r0, cn + vy * r0)
        a1 = (ce + vx * (r0 + 34 * mm), cn + vy * (r0 + 34 * mm))
        msp.add_line(a0, a1, dxfattribs={'layer': 'Podpisi'})
        ang = math.atan2(a1[1] - a0[1], a1[0] - a0[0]); hh = 2.6 * mm
        msp.add_solid([a1,
                       (a1[0] - hh * math.cos(ang - 0.38), a1[1] - hh * math.sin(ang - 0.38)),
                       (a1[0] - hh * math.cos(ang + 0.38), a1[1] - hh * math.sin(ang + 0.38))],
                      dxfattribs={'layer': 'Podpisi'})
        # подпись к стрелке печатается на листе рядом с масштабом: в модели
        # она садилась на компас и её перекрывало

def _legend(psp, x0, y0, w, kn, parts, egrn_ha, keys):
    """Таблица условных обозначений на листе; возвращает её высоту в мм."""
    rows = [(k, LEGEND.get(k, parts[k].get('название', '')), areas.m2(parts[k]))
            for k in sorted(keys, key=lambda q: -parts[q]['areaHa'])]
    rows.append((None, 'Контур и кадастровый номер земельного участка '
                       'согласно сведений ЕГРН', int(round(egrn_ha * 10000))))
    c1, c2 = x0 + 19, x0 + w - 20
    # MTEXT переносит по ширине колонки: при высоте знака 2 мм в 36 мм
    # помещается около 34 знаков — по ним и считаем высоту строки
    # ширина знака у чертёжного шрифта около 0,75 высоты, поэтому в колонку
    # 36 мм при высоте 2 мм влезает ~24 знака: по ним и считаем высоту строки
    per_line = max(12, int((c2 - c1 - 3) / 1.5))
    nlines = [len(wrap_chars(t.replace('\n', ' '), per_line)) for _, t, _ in rows]
    hs = [max(8.6, 3.2 * n + 4.6) for n in nlines]
    H = 8.5 + sum(hs)
    top = y0 + H
    _fill(psp, x0, y0, x0 + w, top, 'Legenda')
    _rect(psp, x0, y0, x0 + w, top, 'Legenda')
    _txt(psp, 'Условные обозначения:', (x0 + w / 2, top - 2.4), 2.6, 'Legenda')
    hdr = top - 4.6
    psp.add_line((x0, hdr), (x0 + w, hdr), dxfattribs={'layer': 'Legenda'})
    for xx, t in ((x0 + 9.5, 'Графика'), ((c1 + c2) / 2, 'Описание'),
                  ((c2 + x0 + w) / 2, 'Площадь')):
        _txt(psp, t, (xx, hdr - 2.0), 2.3, 'Legenda')
    psp.add_line((x0, hdr - 3.9), (x0 + w, hdr - 3.9), dxfattribs={'layer': 'Legenda'})
    for xx in (c1, c2):
        psp.add_line((xx, hdr), (xx, y0), dxfattribs={'layer': 'Legenda'})
    y = hdr - 3.9
    for (k, desc, m2), hgt in zip(rows, hs):
        gy = y - hgt / 2
        layer = 'CHZU_%s' % k if k else 'Granica_ZU'
        col = DXF_RGB.get(layer, (0, 0, 0))
        # образец — только цветная рамка с обозначением: штриховка в ячейке
        # 14 × 5 мм вылезала за границы и пересекала описание
        _rect(psp, x0 + 2.4, gy - 2.6, c1 - 2.4, gy + 2.6, 'Legenda').rgb = col
        _txt(psp, 'ЧЗУ/%s' % k if k else kn,
             ((x0 + 2.4 + c1 - 2.4) / 2, gy), 2.3 if k else 1.8, 'Legenda')
        m = psp.add_mtext(desc.replace('\n', ' '),
                          dxfattribs={'layer': 'Legenda', 'char_height': 2.0,
                                      'width': c2 - c1 - 3, 'line_spacing_factor': 0.85})
        m.set_location((c1 + 1.6, gy + hgt / 2 - 2.2))
        _txt(psp, '(%s м²)' % format(m2, ',d').replace(',', ' '),
             ((c2 + x0 + w) / 2, gy + 1.4), 2.0, 'Legenda')
        _txt(psp, '(%s га)' % ('%.2f' % (m2 / 10000.0)).replace('.', ','),
             ((c2 + x0 + w) / 2, gy - 1.8), 2.0, 'Legenda')
        y -= hgt
        if y > y0 + 0.5:
            psp.add_line((x0, y), (x0 + w, y), dxfattribs={'layer': 'Legenda'})
    return H

def _stamp(psp, x0, y0, w=62.0, h=30.0):
    _fill(psp, x0, y0, x0 + w, y0 + h, 'Shtamp')
    _rect(psp, x0, y0, x0 + w, y0 + h, 'Shtamp')
    _txt(psp, 'Утверждаю:', (x0 + w / 2, y0 + h - 4.0), 2.9, 'Shtamp')
    psp.add_line((x0 + 6, y0 + h - 11), (x0 + w - 6, y0 + h - 11), dxfattribs={'layer': 'Shtamp'})
    _txt(psp, '(должность, фамилия, инициалы)', (x0 + w / 2, y0 + h - 13.5), 2.1, 'Shtamp',
         (90, 90, 90))
    psp.add_line((x0 + 6, y0 + h - 19), (x0 + 26, y0 + h - 19), dxfattribs={'layer': 'Shtamp'})
    psp.add_line((x0 + 34, y0 + h - 19), (x0 + w - 6, y0 + h - 19), dxfattribs={'layer': 'Shtamp'})
    _txt(psp, '(подпись)', (x0 + 16, y0 + h - 21.5), 2.1, 'Shtamp', (90, 90, 90))
    _txt(psp, '«____» ______________ 20____ г.', (x0 + w / 2, y0 + 4.0), 2.3, 'Shtamp')

def _compass(psp, cx, cy, r=7.0):
    _fill(psp, cx - r - 4.5, cy - r - 4.5, cx + r + 4.5, cy + r + 4.5, 'Kompas')
    psp.add_circle((cx, cy), r, dxfattribs={'layer': 'Kompas'})
    psp.add_solid([(cx, cy + r), (cx - 1.6, cy), (cx + 1.6, cy)],
                  dxfattribs={'layer': 'Kompas'}).rgb = (200, 0, 0)
    for t, xy in (('С', (cx, cy + r + 3.0)), ('Ю', (cx, cy - r - 3.0)),
                  ('З', (cx - r - 3.0, cy)), ('В', (cx + r + 3.0, cy))):
        _txt(psp, t, xy, 2.7, 'Kompas', (0, 0, 0))

def schema_dxf(path, kn, rings, parts, egrn_ha, zone, meta=None, image=None,
               neighbors=None, place=None, tol_m=0.0):
    """Чертёж схемы: модель в МСК, лист A4 с рамкой, легендой и штампом."""
    try:
        import ezdxf
    except ImportError:
        return None, 0
    img_name = put_image(path, image, meta) if (image and meta and os.path.exists(image)) else None

    doc = ezdxf.new('R2000', setup=True)
    doc.header['$INSUNITS'] = 6
    for name, rgb in list(DXF_RGB.items()) + list(SHEET_RGB.items()):
        doc.layers.add(name).rgb = rgb
    msp = doc.modelspace()

    keys = sorted(parts)
    lay = doc.layouts.new('Схема ЧЗУ')
    lay.page_setup(size=SHEET, margins=(0, 0, 0, 0), units='mm')
    psp = lay
    W, H = SHEET
    _rect(psp, 5, 5, W - 5, H - 5, 'Ramka')
    _rect(psp, 7, 7, W - 7, H - 7, 'Ramka', lw=0.7)
    TH = 16.0                                   # высота полосы заголовка
    psp.add_line((7, H - 7 - TH), (W - 7, H - 7 - TH), dxfattribs={'layer': 'Ramka'})
    lines = wrap_chars(TITLE % kn, 118)[:3]
    for i, t in enumerate(lines):
        _txt(psp, t, (W / 2, H - 7 - 3.8 - i * 4.2), 2.7, 'Zagolovok')

    vx0, vy0, vx1, vy1 = 9.0, 9.0, W - 9.0, H - 9.0 - TH
    den = scale_for(rings, vx1 - vx0, vy1 - vy0)
    E = [p[0] for r in rings for p in r]; N = [p[1] for r in rings for p in r]
    ce, cn = (max(E) + min(E)) / 2, (max(N) + min(N)) / 2
    psp.add_viewport(center=((vx0 + vx1) / 2, (vy0 + vy1) / 2),
                     size=(vx1 - vx0, vy1 - vy0),
                     view_center_point=(ce, cn),
                     view_height=(vy1 - vy0) * den / 1000.0)

    _compass(psp, vx0 + 11, vy1 - 13)
    LW = 84.0
    lh = _legend(psp, W - 9 - LW, 9 + 2, LW, kn, parts, egrn_ha, keys)
    _stamp(psp, vx0 + 2, 9 + 2)
    ny = 9 + 2 + 30 + 1
    nh = 9.0 + (4.0 if place else 0.0)
    _fill(psp, vx0 + 2, ny, vx0 + 2 + 74, ny + nh, 'Zagolovok')
    _txt(psp, 'Масштаб 1:%d' % den, (vx0 + 4, ny + nh - 2.6), 2.6, 'Zagolovok',
         align='MIDDLE_LEFT')
    if place:
        from .sources.places import line as place_line
        _txt(psp, 'Ближайший населённый пункт: %s' % place_line(place),
             (vx0 + 4, ny + nh - 6.4), 2.2, 'Zagolovok', align='MIDDLE_LEFT')
    if img_name:
        _txt(psp, 'подложка: %s + .jgw' % img_name[:44],
             (vx0 + 4, ny + 2.4), 1.9, 'Zagolovok', (110, 110, 110), align='MIDDLE_LEFT')

    _model(msp, doc, kn, rings, parts, zone, meta if img_name else None, img_name,
           neighbors, place, den)
    doc.set_modelspace_vport(height=(vy1 - vy0) * den / 1000.0, center=(ce, cn))
    doc.saveas(path)
    return path, den
