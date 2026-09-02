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

def render(rings_wgs, contours, profiles=(), size=(2400, 1650), pad_km=11.0, kn=''):
    """Карта как одно изображение: контуры, участок, сетка, легенда."""
    W = size[0]
    # шрифт считаем от конечного размера на листе: карта уходит в записку
    # шириной 155 мм, при F = W/105 подписи выходили в миллиметр высотой
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
    bbox = bbox_of(rings_wgs, pad_km=pad_km, aspect=W / MAP_H)
    pr = projector(bbox, (W, MAP_H))

    img = Image.new('RGB', size, 'white')
    # карта рисуется в собственном холсте: полигоны кадром не обрезаются
    # и иначе затекают в полосу легенды
    mp = Image.new('RGB', (W, MAP_H), 'white')
    d = ImageDraw.Draw(mp)

    for c in used:
        for poly in _polys(c['geometry']):
            d.polygon([pr(x, y) for x, y in poly[0]], fill=_hex(c['заливка']))
    for c in used:                       # обводки поверх всех заливок
        for poly in _polys(c['geometry']):
            d.line([pr(x, y) for x, y in poly[0]] + [pr(*poly[0][0])],
                   fill=_hex(c['обводка'], (100, 100, 100)), width=max(1, W // 900))

    # координатная сетка — то, ради чего лист и делается
    step = grid_step(max(bbox[2] - bbox[0], bbox[3] - bbox[1]))
    lon = math.ceil(bbox[0] / step) * step
    while lon < bbox[2]:
        x = pr(lon, bbox[3])[0]
        d.line([(x, 0), (x, MAP_H)], fill=GRID, width=max(1, W // 1600))
        d.text((x + F * 0.3, MAP_H - F * 1.4), ('%.2f°' % lon).replace('.', ','),
               font=fs, fill=GRID, stroke_width=max(2, F // 6), stroke_fill='white')
        lon += step
    lat = math.ceil(bbox[1] / step) * step
    while lat < bbox[3]:
        y = pr(bbox[0], lat)[1]
        d.line([(0, y), (W, y)], fill=GRID, width=max(1, W // 1600))
        d.text((F * 0.4, y - F * 1.3), ('%.2f°' % lat).replace('.', ','),
               font=fs, fill=GRID, stroke_width=max(2, F // 6), stroke_fill='white')
        lat += step

    # место подписи участка резервируем заранее: индексы контуров рисуются
    # первыми и раньше залезали под неё
    cx = sum(p[0] for p in rings_wgs[0]) / len(rings_wgs[0])
    cy = sum(p[1] for p in rings_wgs[0]) / len(rings_wgs[0])
    px, py = pr(cx, cy)
    tag = 'ЗУ %s' % kn if kn else 'земельный участок'
    tw = d.textlength(tag, font=fb)
    for dx, dy in ((F * 4, -F * 3), (F * 4, F * 3), (-F * 4 - tw, -F * 3), (-F * 4 - tw, F * 3)):
        lx, ly_ = px + dx, py + dy
        if 0 < lx and lx + tw < W and F * 2 < ly_ < MAP_H - F * 2:
            break
    label_box = (lx - F, ly_ - F, lx + tw + F, ly_ + F * 1.6)

    def _busy(spot):
        return (label_box[0] < spot[0] < label_box[2]) and (label_box[1] < spot[1] < label_box[3])

    for c in used:                       # индексы контуров
        for poly in _polys(c['geometry']):
            spot = _label_spot(pr, poly, (W, MAP_H))
            if spot and c.get('индекс') and not _busy(spot):
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
        d.line([pr(*p) for p in r] + [pr(*r[0])], fill=PARCEL, width=max(3, W // 300))
    d.line([(px, py), (lx if lx > px else lx + tw, ly_ + F * 0.6)], fill=PARCEL,
           width=max(2, W // 800))
    # плашка под подписью: белого контура вокруг букв мало, когда под ними
    # проходит граница контура карты
    d.rectangle([lx - F * 0.4, ly_ - F * 0.6, lx + tw + F * 0.4, ly_ + F * 0.95],
                fill='white', outline=PARCEL, width=max(1, W // 1600))
    d.text((lx, ly_ - F * 0.4), tag, font=fb, fill=PARCEL)

    # масштабная линейка
    km = 5.0
    w_px = km / ((bbox[2] - bbox[0]) * 111.3 * math.cos(math.radians(cy))) * W
    x0, y0 = W - w_px - F * 2, MAP_H - F * 2.6
    d.rectangle([x0, y0, x0 + w_px, y0 + F * 0.6], fill='white', outline=(30, 30, 30))
    d.rectangle([x0, y0, x0 + w_px / 2, y0 + F * 0.6], fill=(30, 30, 30))
    d.text((x0, y0 - F * 1.25), '0', font=fs, fill=(30, 30, 30),
           stroke_width=max(2, F // 6), stroke_fill='white')
    d.text((x0 + w_px, y0 - F * 1.25), '%d км' % km, font=fs, fill=(30, 30, 30), anchor='ma',
           stroke_width=max(2, F // 6), stroke_fill='white')
    d.rectangle([0, 0, W - 1, MAP_H - 1], outline=(40, 40, 40), width=max(2, W // 1200))
    img.paste(mp, (0, 0))
    d = ImageDraw.Draw(img)

    # легенда: цвет — индекс — название из легенды карты
    ly = MAP_H + int(F * 0.9)
    d.text((0, ly), 'Контуры карты в кадре', font=fb, fill='black')
    ly += int(F * 1.7)
    colw = W // 2
    for i, ix in enumerate(legend):
        col = next(c for c in used if c['индекс'] == ix)
        x = (i % 2) * colw
        y = ly + (i // 2) * int(F * 1.9)
        d.rectangle([x, y, x + F * 1.4, y + F], fill=_hex(col['заливка']),
                    outline=(90, 90, 90))
        d.text((x + F * 2.0, y - F * 0.1), '%s — %s' % (ix, names.get(ix) or 'нет в легенде'),
               font=f, fill=(40, 40, 40))
    d.text((W - F * 0.5, MAP_H + int(F * 0.9)),
           'Почвенная карта РСФСР 1:2 500 000 (Фридланд и др., 1988) · '
           'сетка WGS-84 · равнопромежуточная проекция',
           font=fs, fill=(110, 110, 110), anchor='ra')
    return img
