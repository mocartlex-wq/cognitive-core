# -*- coding: utf-8 -*-
"""Обзорная карта: участок на Почвенной карте РСФСР 1:2 500 000.

Записка утверждает тип почвы — специалист должен иметь возможность это
проверить, а не поверить на слово. Поэтому карта рисуется с координатной
сеткой: берёшь подписанную линию, сверяешь с выпиской или с сайтом
источника и видишь, в какой контур попал участок.

Раскраска взята у самой карты (сервер отдаёт цвета контуров), чтобы лист
и источник читались одинаково.
"""
import math

from PIL import Image, ImageDraw, ImageFont

from ..sheet import SERIF, SERIF_B
from ..sources import fridland_map as fm
from ..sources import soil as soil_src

PARCEL = (200, 0, 0)
GRID = (70, 70, 70)

def _hex(c, default=(200, 200, 200)):
    c = (c or '').lstrip('#')
    if len(c) != 6:
        return default
    try:
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return default

def bbox_of(rings, pad_km=11.0, aspect=1.45):
    """Габарит карты вокруг участка: пэд в километрах, а не в градусах.

    В градусах долготы километр на этой широте почти вдвое короче, чем
    в градусах широты, и кадр вышел бы вытянутым.
    """
    pts = [p for r in rings for p in r]
    lo = [p[0] for p in pts]; la = [p[1] for p in pts]
    clat = (min(la) + max(la)) / 2
    dlat = pad_km / 111.3
    dlon = pad_km / (111.3 * math.cos(math.radians(clat)))
    b = [min(lo) - dlon, min(la) - dlat, max(lo) + dlon, max(la) + dlat]
    # доводим до нужного отношения сторон, растягивая меньшую сторону
    w_km = (b[2] - b[0]) * 111.3 * math.cos(math.radians(clat))
    h_km = (b[3] - b[1]) * 111.3
    if w_km / h_km < aspect:
        need = h_km * aspect - w_km
        d = need / 2 / (111.3 * math.cos(math.radians(clat)))
        b[0] -= d; b[2] += d
    else:
        need = w_km / aspect - h_km
        d = need / 2 / 111.3
        b[1] -= d; b[3] += d
    return tuple(b)

def frame_pad_km(parcel_w_km, parcel_h_km, aspect, fill=0.20, min_pad=0.35):
    """Отступ вокруг участка, при котором он займёт fill ширины кадра.

    Постоянный отступ плохо переносится на поток: у мелкого ЗУ кадр
    выходил пустым, у крупного — тесным. Считаем от размера участка,
    но не подпускаем контур вплотную к рамке.
    """
    need_h = parcel_w_km / (aspect * max(fill, 1e-3))
    return max((need_h - parcel_h_km) / 2, parcel_h_km * 0.15, min_pad)

def size_km(rings):
    """Габарит участка в километрах: ширина, высота, широта центра."""
    pts = [p for r in rings for p in r]
    lat = sum(p[1] for p in pts) / len(pts)
    w = (max(p[0] for p in pts) - min(p[0] for p in pts)) * 111.3 * math.cos(math.radians(lat))
    h = (max(p[1] for p in pts) - min(p[1] for p in pts)) * 111.3
    return w, h, lat

def projector(bbox, size):
    """Долгота/широта → пиксель кадра (равнопромежуточная, широта вверх)."""
    lon0, lat0, lon1, lat1 = bbox
    w, h = size
    return lambda lon, lat: ((lon - lon0) / (lon1 - lon0) * w,
                             (lat1 - lat) / (lat1 - lat0) * h)

def grid_step(span_deg):
    """Шаг сетки: 3–6 линий в кадре, круглое число градусов."""
    for s in (0.02, 0.05, 0.1, 0.2, 0.5, 1.0):
        if span_deg / s <= 8:
            return s
    return 1.0

def _polys(geom):
    if not geom:
        return []
    return geom['coordinates'] if geom.get('type') == 'MultiPolygon' else [geom['coordinates']]

def _label_spot(pr, poly, size):
    """Точка для подписи контура: центр его видимой части кадра."""
    pts = [pr(x, y) for x, y in poly[0]]
    ins = [p for p in pts if 0 < p[0] < size[0] and 0 < p[1] < size[1]]
    if not ins:
        return None
    return (sum(p[0] for p in ins) / len(ins), sum(p[1] for p in ins) / len(ins))

def scale_bar_km(span_km):
    """Длина линейки: самое крупное круглое число, занимающее до трети кадра.

    Жёсткое «5 км» и узкое окно 15–30 % давали то полкадра, то откат
    на 50 км, и линейка растягивалась на всю карту.
    """
    best = 0.5
    for km in (0.5, 1, 2, 5, 10, 20, 50, 100):
        if km / span_km <= 0.33:
            best = km
    return best

def _frame(size, bbox, contours, rings_wgs, profiles=(), F=20, kn='', scale=True,
           labels=True, avoid=None):
    """Один кадр карты: заливки, сетка, участок. Возвращает изображение.

    Один и тот же код рисует и крупный план, и обзорную врезку — иначе
    они разъезжаются в мелочах и перестают быть одной картой.
    """
    W, H = size
    fb = ImageFont.truetype(SERIF_B, F)
    fs = ImageFont.truetype(SERIF, int(F * 0.85))
    mp = Image.new('RGB', size, 'white')
    d = ImageDraw.Draw(mp)
    pr = projector(bbox, size)
    used = [c for c in contours if c.get('geometry')]

    for c in used:
        for poly in _polys(c['geometry']):
            d.polygon([pr(x, y) for x, y in poly[0]], fill=_hex(c['заливка']))
    for c in used:                       # обводки поверх всех заливок
        for poly in _polys(c['geometry']):
            d.line([pr(x, y) for x, y in poly[0]] + [pr(*poly[0][0])],
                   fill=_hex(c['обводка'], (100, 100, 100)), width=max(1, W // 700))

    # координатная сетка — то, ради чего лист и делается
    step = grid_step(max(bbox[2] - bbox[0], bbox[3] - bbox[1]))
    lon = math.ceil(bbox[0] / step) * step
    while lon < bbox[2]:
        x = pr(lon, bbox[3])[0]
        d.line([(x, 0), (x, H)], fill=GRID, width=max(1, W // 1200))
        if labels:
            # подписи ставим у обеих рамок: врезка-обзор закрывает один угол,
            # и одиночная подпись могла оказаться под ней
            t = ('%.2f°' % lon).replace('.', ',')
            for yy in (H - F * 1.4, F * 0.4):
                d.text((x + F * 0.3, yy), t, font=fs, fill=GRID,
                       stroke_width=max(2, F // 6), stroke_fill='white')
        lon += step
    lat = math.ceil(bbox[1] / step) * step
    while lat < bbox[3]:
        y = pr(bbox[0], lat)[1]
        d.line([(0, y), (W, y)], fill=GRID, width=max(1, W // 1200))
        if labels:
            t = ('%.2f°' % lat).replace('.', ',')
            d.text((F * 0.4, y - F * 1.3), t, font=fs, fill=GRID,
                   stroke_width=max(2, F // 6), stroke_fill='white')
            if y < H - F * 4:        # в правом нижнем углу стоит масштабная линейка
                d.text((W - F * 0.4, y - F * 1.3), t, font=fs, fill=GRID, anchor='ra',
                       stroke_width=max(2, F // 6), stroke_fill='white')
        lat += step

    cx = sum(p[0] for p in rings_wgs[0]) / len(rings_wgs[0])
    cy = sum(p[1] for p in rings_wgs[0]) / len(rings_wgs[0])
    px, py = pr(cx, cy)
    label_box = None
    if kn:
        # место подписи резервируем заранее: индексы контуров рисуются
        # первыми и раньше залезали под неё
        tag = 'ЗУ %s' % kn
        tw = d.textlength(tag, font=fb)
        pts = [pr(*p) for r in rings_wgs for p in r]
        pbox = (min(p[0] for p in pts), min(p[1] for p in pts),
                max(p[0] for p in pts), max(p[1] for p in pts))

        def _hits(box, x, y):
            return (x < box[2] and x + tw > box[0]
                    and y - F < box[3] and y + F * 1.6 > box[1])

        def _free(x, y, strict=True):
            if not (0 < x and x + tw < W and F * 2 < y < H - F * 2):
                return False
            if avoid and _hits(avoid, x, y):     # под врезкой-обзором подписи не ставим
                return False
            # и по возможности не поверх самого участка — он теперь крупный
            return not (strict and _hits(pbox, x, y))

        spots = [(F * 3, -F * 3), (F * 3, F * 3), (-F * 3 - tw, -F * 3), (-F * 3 - tw, F * 3),
                 (F * 3, -F * 7), (-F * 3 - tw, F * 7),
                 ((pbox[2] - px) + F * 2, 0), (pbox[0] - px - tw - F * 2, 0),
                 (0, (pbox[3] - py) + F * 2.5), (0, (pbox[1] - py) - F * 2.5)]
        lx, ly = px + F * 3, py - F * 3
        for strict in (True, False):
            found = False
            for dx, dy in spots:
                if _free(px + dx, py + dy, strict):
                    lx, ly = px + dx, py + dy
                    found = True
                    break
            if found:
                break
        label_box = (lx - F, ly - F, lx + tw + F, ly + F * 1.6)

    if labels:
        for c in used:                   # индексы контуров
            for poly in _polys(c['geometry']):
                spot = _label_spot(pr, poly, size)
                busy = label_box and (label_box[0] < spot[0] < label_box[2]
                                      and label_box[1] < spot[1] < label_box[3]) if spot else False
                if spot and c.get('индекс') and not busy:
                    d.text(spot, c['индекс'], font=fb, fill=(30, 30, 30), anchor='mm',
                           stroke_width=max(2, F // 5), stroke_fill='white')
                break

    for p in profiles:                   # разрезы, если попали в кадр
        if bbox[0] < p['lon'] < bbox[2] and bbox[1] < p['lat'] < bbox[3]:
            x, y = pr(p['lon'], p['lat'])
            r = F * 0.45
            d.ellipse([x - r, y - r, x + r, y + r], fill='white', outline=(20, 20, 20),
                      width=max(1, W // 1200))
            d.text((x + r * 1.6, y), 'разрез %s' % p['id'], font=fs, fill=(20, 20, 20),
                   anchor='lm', stroke_width=max(2, F // 6), stroke_fill='white')

    for r in rings_wgs:                  # участок
        d.line([pr(*p) for p in r] + [pr(*r[0])], fill=PARCEL, width=max(2, W // 260))
    if label_box:
        d.line([(px, py), (lx if lx > px else lx + tw, ly + F * 0.6)], fill=PARCEL,
               width=max(2, W // 800))
        # плашка под подписью: белого контура вокруг букв мало, когда под ними
        # проходит граница контура карты
        d.rectangle([lx - F * 0.4, ly - F * 0.6, lx + tw + F * 0.4, ly + F * 0.95],
                    fill='white', outline=PARCEL, width=max(1, W // 1600))
        d.text((lx, ly - F * 0.4), tag, font=fb, fill=PARCEL)

    if scale:
        span_km = (bbox[2] - bbox[0]) * 111.3 * math.cos(math.radians(cy))
        km = scale_bar_km(span_km)
        w_px = km / span_km * W
        x0, y0 = W - w_px - F * 2, H - F * 2.6
        d.rectangle([x0, y0, x0 + w_px, y0 + F * 0.6], fill='white', outline=(30, 30, 30))
        d.rectangle([x0, y0, x0 + w_px / 2, y0 + F * 0.6], fill=(30, 30, 30))
        d.text((x0, y0 - F * 1.25), '0', font=fs, fill=(30, 30, 30),
               stroke_width=max(2, F // 6), stroke_fill='white')
        d.text((x0 + w_px, y0 - F * 1.25), ('%g км' % km).replace('.', ','), font=fs,
               fill=(30, 30, 30), anchor='ma', stroke_width=max(2, F // 6), stroke_fill='white')
    d.rectangle([0, 0, W - 1, H - 1], outline=(40, 40, 40), width=max(2, W // 1200))
    return mp, pr, (px, py)

def render(rings_wgs, contours, profiles=(), size=(3600, 2400), kn='',
           fill=0.20, wide_km=16.0):
    """Лист карты: крупный план участка, врезка-обзор и легенда.

    Владелец обвёл на прежней карте область вокруг участка — «покрупнее».
    Кадр 35 км показывал контекст, но не участок; теперь главный кадр
    крупный, а контекст ушёл во врезку с рамкой увеличенного места.
    """
    W = size[0]
    F = max(12, int(W / 62))
    f = ImageFont.truetype(SERIF, F)
    fb = ImageFont.truetype(SERIF_B, F)
    fs = ImageFont.truetype(SERIF, int(F * 0.85))

    used = [c for c in contours if c.get('geometry')]
    names = {}
    for c in used:
        row = soil_src.fridland(index=c['индекс']) if c.get('индекс') else None
        names[c['индекс']] = row['название'] if row else None
    legend = sorted({c['индекс'] for c in used if c.get('индекс')})
    rows_leg = (len(legend) + 1) // 2
    LEG_H = int(F * 1.9) * rows_leg + int(F * 2.6)
    MAP_H = size[1] - LEG_H

    pw, ph, _ = size_km(rings_wgs)
    near = bbox_of(rings_wgs, pad_km=frame_pad_km(pw, ph, W / MAP_H, fill),
                   aspect=W / MAP_H)
    # место врезки выбираем до отрисовки кадра: иначе подпись участка
    # встаёт там же и уходит под врезку
    iw = int(W * 0.30)
    ih = int(iw / 1.5)
    m = int(F * 0.8)
    prm = projector(near, (W, MAP_H))
    cx = sum(p[0] for p in rings_wgs[0]) / len(rings_wgs[0])
    cy = sum(p[1] for p in rings_wgs[0]) / len(rings_wgs[0])
    parcel_px = prm(cx, cy)
    corners = [(m, m), (W - iw - m, m), (m, MAP_H - ih - int(F * 3.6))]
    ix, iy = max(corners, key=lambda c: (c[0] + iw / 2 - parcel_px[0]) ** 2
                                        + (c[1] + ih / 2 - parcel_px[1]) ** 2)
    box = (ix - m / 2, iy - m / 2, ix + iw + m / 2, iy + ih + m / 2)

    mp, pr, _ = _frame((W, MAP_H), near, used, rings_wgs, profiles, F=F, kn=kn, avoid=box)

    # врезка-обзор: показывает, где мы, и что именно увеличено
    wide = bbox_of(rings_wgs, pad_km=wide_km, aspect=iw / ih)
    ins, ipr, _ = _frame((iw, ih), wide, used, rings_wgs, F=max(9, int(F * 0.62)),
                         scale=False, labels=False)
    di = ImageDraw.Draw(ins)
    x0, y0 = ipr(near[0], near[3]); x1, y1 = ipr(near[2], near[1])
    di.rectangle([x0, y0, x1, y1], outline=PARCEL, width=max(2, iw // 150))
    d = ImageDraw.Draw(mp)
    d.rectangle(list(box), fill='white')
    mp.paste(ins, (ix, iy))
    d.rectangle([ix, iy, ix + iw, iy + ih], outline=(40, 40, 40), width=max(2, W // 1200))
    d.text((ix + F * 0.4, iy + F * 0.3), 'обзор', font=fb, fill=(30, 30, 30),
           stroke_width=max(2, F // 5), stroke_fill='white')

    img = Image.new('RGB', size, 'white')
    img.paste(mp, (0, 0))
    d = ImageDraw.Draw(img)

    # легенда: цвет — индекс — название из легенды карты
    ly = MAP_H + int(F * 0.9)
    d.text((0, ly), 'Контуры карты', font=fb, fill='black')
    ly += int(F * 1.7)
    colw = W // 2
    for i, ix_ in enumerate(legend):
        col = next(c for c in used if c['индекс'] == ix_)
        x = (i % 2) * colw
        y = ly + (i // 2) * int(F * 1.9)
        d.rectangle([x, y, x + F * 1.4, y + F], fill=_hex(col['заливка']),
                    outline=(90, 90, 90))
        d.text((x + F * 2.0, y - F * 0.1), '%s — %s' % (ix_, names.get(ix_) or 'нет в легенде'),
               font=f, fill=(40, 40, 40))
    d.text((W - F * 0.5, MAP_H + int(F * 0.9)),
           'Почвенная карта РСФСР 1:2 500 000 (Фридланд и др., 1988) · '
           'сетка WGS-84 · равнопромежуточная проекция',
           font=fs, fill=(110, 110, 110), anchor='ra')
    return img
