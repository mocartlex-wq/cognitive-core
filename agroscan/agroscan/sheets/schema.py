# -*- coding: utf-8 -*-
"""Схема расположения ЗУ и контуров частей — основной лист комплекта.

A4 альбомная, 400 dpi. Состав частей, их назначение и подписи берутся из
конфига участка, поэтому лист собирается для любого набора ЧЗУ, а не только
для четырёх частей 1173.
"""
import math
import numpy as np

from ..sheet import Sheet, PART_COLOR, PART_HATCH, ZUG, RED, fmt_ha, fmt_m2
from ..geo import Local

LEGEND = {
    '1': 'Контур части ЗУ с/х назначения, покрытый\nкустарниковой и древесной растительностью,\nна котором планируется проведение\nкультуртехнических мероприятий',
    '2': 'Контур части ЗУ, не обработанный, покрытый\nтравяной и кустарниковой растительностью\nбез древесной (залежь)',
    '3': 'Зона с особыми условиями использования\nтерритории (мероприятия не проводятся)',
    '4': 'Защитные лесные насаждения (полезащитные\nлесные полосы) — раскорчёвке не подлежат,\nиз площади мероприятий исключены',
}

def _ring_area(r):
    a = np.asarray(r, float)
    return abs(float(np.sum(a[:, 0] * np.roll(a[:, 1], -1) - np.roll(a[:, 0], -1) * a[:, 1]) / 2))

def _anchor(r):
    from shapely.geometry import Polygon
    try:
        p = Polygon(r).buffer(0).representative_point()
        return p.x, p.y
    except Exception:
        return sum(q[0] for q in r) / len(r), sum(q[1] for q in r) / len(r)

def _compass(s, cx, cy):
    R, r2 = s.mm(7), s.mm(2.6)
    s.d.ellipse([cx - R, cy - R, cx + R, cy + R], outline=(120, 120, 120), width=s.W(0.26))
    for k in range(4):
        a = math.pi / 4 + k * math.pi / 2
        s.d.polygon([(cx + R * .6 * math.cos(a), cy + R * .6 * math.sin(a)),
                     (cx + r2 * .45 * math.cos(a + math.pi / 2), cy + r2 * .45 * math.sin(a + math.pi / 2)),
                     (cx + r2 * .45 * math.cos(a - math.pi / 2), cy + r2 * .45 * math.sin(a - math.pi / 2))],
                    fill=(150, 150, 150))
    s.d.polygon([(cx, cy - R), (cx - r2 * .5, cy), (cx, cy - r2 * .3)], fill=(210, 0, 0))
    s.d.polygon([(cx, cy - R), (cx + r2 * .5, cy), (cx, cy - r2 * .3)], fill=(120, 0, 0))
    s.d.polygon([(cx, cy + R), (cx - r2 * .5, cy), (cx, cy + r2 * .3)], fill=(70, 70, 70))
    s.d.polygon([(cx, cy + R), (cx + r2 * .5, cy), (cx, cy + r2 * .3)], fill=(30, 30, 30))
    for t, (a_, b_) in [('С', (cx, cy - R - s.mm(2.9))), ('Ю', (cx, cy + R + s.mm(2.9))),
                        ('З', (cx - R - s.mm(2.9), cy)), ('В', (cx + R + s.mm(2.9), cy))]:
        s.d.text((a_, b_), t, font=s.F(2.7, True), fill='black', anchor='mm')

def build(path, kn, rings, parts, egrn_ha, zone, neighbors=None, title=None):
    """parts — {ключ: {'outer','inner','areaHa','название'}} в порядке отрисовки."""
    keys = sorted(parts)
    s = Sheet(297, 210)
    P = s.map_field(rings, pad_m=260, shift_mm=28)
    TW, TBL = s.mm(103), s.mm(12 + 14 * (len(keys) + 3))
    SW, SH = s.mm(62), s.mm(30)
    s.block(s.MW - TW, s.MH - TBL, s.MW, s.MH)
    s.block(0, s.MH - SH, SW, s.MH)
    s.block(0, 0, s.mm(25), s.mm(27))

    # смежные участки — красная сеть; подписи только тем, что крупнее 0,9 га
    f_adj = s.F(2.05)
    for o in (neighbors or []):
        if o.get('kn') == kn:
            continue
        for r in o['rings']:
            s.md.line([P(*p) for p in r] + [P(*r[0])], fill=RED, width=s.W(0.22))
        r = max(o['rings'], key=len)
        if _ring_area(r) > 9000:
            cx = sum(p[0] for p in r) / len(r); cy = sum(p[1] for p in r) / len(r)
            s.label(*P(cx, cy), o['kn'], f_adj, fill=RED, halo=None,
                    offsets=((0, 0), (0, -3.4), (0, 3.4), (0, -6.8), (0, 6.8)))

    for k in keys:                                   # штриховка
        if k in PART_HATCH:
            s.hatch(parts[k]['outer'], parts[k]['inner'], PART_COLOR[k], PART_HATCH[k])
    for k in keys:                                   # контуры частей, кроме ЗОУИТ
        if k == '3':
            continue
        s.polyline(parts[k]['outer'], PART_COLOR[k], 0.40)
        s.polyline(parts[k]['inner'], PART_COLOR[k], 0.32)
    # граница ЗУ под ЗОУИТ: на западной стороне они совпадают, и магента
    # поверх зелёной читается как «розовая жила в зелёной кайме»
    s.polyline(rings[:1], ZUG, 0.70)
    s.polyline(rings[1:], ZUG, 0.56)
    if '3' in parts:
        s.polyline(parts['3']['outer'], PART_COLOR['3'], 0.38)
        s.polyline(parts['3']['inner'], PART_COLOR['3'], 0.32)

    # кадастровый номер: рисуется до подписей частей и сразу занимает своё место
    E = [p[0] for r in rings for p in r]; N = [p[1] for r in rings for p in r]
    x, y = P(sum(E) / len(E), sum(N) / len(N))
    f_kn = s.F(2.9)
    bb = s.md.textbbox((x, y), kn, font=f_kn, anchor='mm')
    box = [bb[0] - s.mm(1.4), bb[1] - s.mm(1.0), bb[2] + s.mm(1.4), bb[3] + s.mm(1.0)]
    s.md.rectangle(box, fill='white', outline=ZUG, width=s.W(0.5))
    s.md.text((x, y), kn, font=f_kn, fill='black', anchor='mm')
    s.reserve(box)

    f_ch = s.F(2.7)
    for k in keys:
        for r in sorted(parts[k]['outer'], key=lambda r: -_ring_area(r))[:4]:
            if _ring_area(r) < 3000:
                continue
            s.label(*P(*_anchor(r)), 'ЧЗУ/%s' % k, f_ch)

    # географические координаты характерных точек
    loc = Local(zone); f_geo = s.F(2.5); out = rings[0]
    idx = [int(np.argmax([p[1] for p in out])), int(np.argmin([p[1] for p in out])),
           int(np.argmin([p[0] for p in out])), int(np.argmax([p[0] for p in out]))]
    for i, (ox, oy) in zip(idx, [(0, -1), (0, 1), (-1, 0), (0.4, -1)]):
        e, n = out[i]; lon, lat = loc.to_wgs([(e, n)])[0]
        x, y = P(e, n)
        tx = ty = None
        for cx_, cy_ in [(ox, oy), (1.1, .6), (1.1, -.6), (-1.1, .6), (-1.1, -.6), (0, -1.4), (0, 1.4)]:
            a, b = x + cx_ * s.mm(13), y + cy_ * s.mm(8)
            if s.free(a - s.mm(11), b - s.mm(4.5), a + s.mm(11), b + s.mm(4.5)):
                tx, ty = a, b; break
        if tx is None:
            tx = min(max(x + ox * s.mm(13), s.mm(12)), s.MW - s.mm(12))
            ty = min(max(y + oy * s.mm(8), s.mm(5)), s.MH - s.mm(5))
        s.md.line([(x, y), (tx, ty)], fill='black', width=s.W(0.24))
        s.md.text((tx, ty - s.mm(1.9)), '%.7f' % lat, font=f_geo, fill='black', anchor='mm',
                  stroke_width=s.W(0.6), stroke_fill='white')
        s.md.line([(tx - s.mm(9), ty), (tx + s.mm(9), ty)], fill='white', width=s.W(0.8))
        s.md.line([(tx - s.mm(9), ty), (tx + s.mm(9), ty)], fill='black', width=s.W(0.26))
        s.md.text((tx, ty + s.mm(1.9)), '%.7f' % lon, font=f_geo, fill='black', anchor='mm',
                  stroke_width=s.W(0.6), stroke_fill='white')
        s.md.ellipse([x - s.mm(.5), y - s.mm(.5), x + s.mm(.5), y + s.mm(.5)], fill='black')

    s.paste_map()
    s.frame(outer_mm=4)
    s.title(title or [
        'Схема расположения земельного участка сельскохозяйственного назначения '
        'с кадастровым номером  %s  и контуров частей' % kn,
        'земельного участка, покрытых древесной и кустарниковой растительностью, '
        'на которых планируется проведение культуртехнических мероприятий'], size=3.0)
    _compass(s, s.MX0 + s.mm(11), s.MY0 + s.mm(13))

    # таблица условных обозначений
    tx0 = s.IN1 - s.mm(2) - TW; ty0 = s.PH - s.margin - s.mm(2) - TBL
    s.d.rectangle([tx0, ty0, tx0 + TW, ty0 + TBL], fill='white', outline='black', width=s.W(0.55))
    s.d.text((tx0 + TW / 2, ty0 + s.mm(1.1)), 'Условные обозначения:', font=s.F(3.0),
             fill='black', anchor='ma')
    hdr = ty0 + s.mm(6.0); c1 = tx0 + s.mm(25); c2 = tx0 + TW - s.mm(19)
    s.d.line([tx0, hdr, tx0 + TW, hdr], fill='black', width=s.W(0.4))
    for xx, t in ((tx0 + s.mm(13.5), 'Графика'), ((c1 + c2) / 2, 'Описание'),
                  ((c2 + tx0 + TW) / 2, 'Площадь')):
        s.d.text((xx, hdr + s.mm(1.1)), t, font=s.F(2.7), fill='black', anchor='ma')
    s.d.line([tx0, hdr + s.mm(6), tx0 + TW, hdr + s.mm(6)], fill='black', width=s.W(0.4))
    for cx_ in (c1, c2):
        s.d.line([cx_, hdr, cx_, ty0 + TBL], fill='black', width=s.W(0.3))
    f_c = s.F(2.25)
    rows = [(k, LEGEND.get(k, parts[k].get('название', '')), parts[k]['areaHa'])
            for k in sorted(keys, key=lambda q: -parts[q]['areaHa'])]
    rows.append((None, 'Контур и кадастровый номер земельного участка\nсогласно сведений ЕГРН', egrn_ha))
    y = hdr + s.mm(6)
    hgt = (ty0 + TBL - y) / len(rows)
    for k, desc, ha in rows:
        gy = y + hgt / 2
        gx0, gx1 = tx0 + s.mm(4), c1 - s.mm(4)
        if k is None:
            s.d.rectangle([gx0, gy - s.mm(3), gx1, gy + s.mm(3)], fill='white',
                          outline=ZUG, width=s.W(0.6))
            # номер подгоняется по ширине образца: 58:28:0500401:74 не влезал
            fs = 2.5
            while fs > 1.6 and s.d.textlength(kn, font=s.F(fs)) > (gx1 - gx0) - s.mm(2):
                fs -= 0.1
            s.d.text(((gx0 + gx1) / 2, gy), kn, font=s.F(fs), fill='black', anchor='mm')
        else:
            col = PART_COLOR.get(k, (0, 0, 0))
            s.d.rectangle([gx0, gy - s.mm(3), gx1, gy + s.mm(3)], outline=col, width=s.W(0.55))
            if k in PART_HATCH:
                # штриховка образца рисуется в отдельный кусок и вставляется:
                # иначе диагонали вылезают за рамку образца
                from PIL import Image as _I, ImageDraw as _D
                w_, h_ = int(gx1 - gx0) - 2, s.mm(6) - 2
                sw = _I.new('RGB', (w_, h_), 'white'); sd = _D.Draw(sw)
                st = s.W(2.2)
                for c in range(-h_, w_ + h_, st):
                    if PART_HATCH[k] > 0:
                        sd.line([(c, h_), (c + h_, 0)], fill=col, width=s.W(0.22))
                    elif PART_HATCH[k] < 0:
                        sd.line([(c, 0), (c + h_, h_)], fill=col, width=s.W(0.22))
                    else:
                        sd.line([(c, 0), (c, h_)], fill=col, width=s.W(0.22))
                s.page.paste(sw, (int(gx0) + 1, int(gy - s.mm(3)) + 1))
                s.d.rectangle([gx0, gy - s.mm(3), gx1, gy + s.mm(3)], outline=col, width=s.W(0.55))
            s.d.text(((gx0 + gx1) / 2, gy), 'ЧЗУ/%s' % k, font=s.F(2.6), fill='black', anchor='mm',
                     stroke_width=s.W(0.5), stroke_fill='white')
        s.d.multiline_text((c1 + s.mm(2.0), gy), desc, font=f_c, fill='black', anchor='lm',
                           spacing=s.mm(0.9))
        s.d.multiline_text(((c2 + tx0 + TW) / 2, gy), '(%s м²)\n(%s га)' % (fmt_m2(ha), fmt_ha(ha)),
                           font=f_c, fill='black', anchor='mm', align='center', spacing=s.mm(0.9))
        y += hgt
        if y < ty0 + TBL - 1:
            s.d.line([tx0, y, tx0 + TW, y], fill='black', width=s.W(0.3))

    # штамп «Утверждаю»
    sx0, sy0 = s.IN0 + s.mm(2), s.PH - s.margin - s.mm(2) - SH
    s.d.rectangle([sx0, sy0, sx0 + SW, sy0 + SH], fill='white', outline='black', width=s.W(0.55))
    s.d.text((sx0 + SW / 2, sy0 + s.mm(2.0)), 'Утверждаю:', font=s.F(2.9), fill='black', anchor='ma')
    s.d.text((sx0 + SW / 2, sy0 + s.mm(6.4)), '_________________________', font=s.F(2.4),
             fill='black', anchor='ma')
    s.d.text((sx0 + SW / 2, sy0 + s.mm(9.2)), '(должность, фамилия, инициалы)', font=s.F(2.1),
             fill=(90, 90, 90), anchor='ma')
    s.d.text((sx0 + s.mm(6), sy0 + s.mm(14.2)), '__________________', font=s.F(2.4), fill='black')
    s.d.text((sx0 + s.mm(19), sy0 + s.mm(17.4)), '(подпись)', font=s.F(2.1), fill='black', anchor='ma')
    s.d.text((sx0 + s.mm(38), sy0 + s.mm(15.4)), '/______________/', font=s.F(2.1), fill='black')
    s.d.text((sx0 + s.mm(5), sy0 + s.mm(23)), '«____» ______________ 20____ г.', font=s.F(2.1), fill='black')
    s.save(path)
    return path, s.denominator()
