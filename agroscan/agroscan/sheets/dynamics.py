# -*- coding: utf-8 -*-
"""Приложение: динамика зарастания по ряду NDVI.

Отвечает на вопрос, который заказчик задаёт первым: когда участок перестали
обрабатывать. Ответ подкрепляется тремя независимыми линиями — оптический
ряд с 1977 года, высота полога и чужая классификация покрова.
"""
import numpy as np
from PIL import Image, ImageDraw

from ..sheet import Sheet, fmt_ha

# Ступени палитры: голая почва, разреженная трава, залежь, сомкнутый полог.
# Плавный градиент 0…0,9 делал все годы одинаково зелёными — на нём не видно
# ровно того, ради чего лист собирается.
_STOPS = [(0.10, (0.72, 0.60, 0.42)), (0.35, (0.86, 0.80, 0.45)),
          (0.50, (0.62, 0.74, 0.36)), (0.62, (0.33, 0.60, 0.26)),
          (0.75, (0.13, 0.42, 0.18)), (0.90, (0.04, 0.24, 0.10))]

def _ndvi_image(a, mask_rings, meta, size, lo=0.10, hi=0.90):
    """Карта NDVI по ступенчатой шкале «почва → трава → залежь → полог»."""
    v = np.nan_to_num(a, nan=lo)
    rgb = np.zeros(v.shape + (3,), np.float32)
    for i in range(len(_STOPS) - 1):
        (v0, c0), (v1, c1) = _STOPS[i], _STOPS[i + 1]
        m = (v >= v0) & (v < v1) if i else (v < v1)
        if i == len(_STOPS) - 2:
            m |= v >= v1
        if not m.any():
            continue
        t = np.clip((v[m] - v0) / max(v1 - v0, 1e-6), 0, 1)[:, None]
        rgb[m] = np.array(c0) * (1 - t) + np.array(c1) * t
    im = Image.fromarray((np.clip(rgb, 0, 1) * 255).astype(np.uint8))
    im = im.resize(size, Image.LANCZOS)
    d = ImageDraw.Draw(im)
    pr = lambda e, n: ((e - meta['e0']) / (meta['e1'] - meta['e0']) * size[0],
                       (meta['n1'] - n) / (meta['n1'] - meta['n0']) * size[1])
    for r in mask_rings:
        d.line([pr(*p) for p in r] + [pr(*r[0])], fill=(200, 0, 190), width=max(2, size[0] // 260))
    return im

def _chart(s, x, y, w, h, rows):
    """Доли пара и сомкнутого полога по годам."""
    years = sorted(rows)
    if len(years) < 2:
        return
    y0, y1 = years[0], years[-1]
    px = lambda yy: x + (yy - y0) / max(y1 - y0, 1) * w
    py = lambda v: y + h - v * h
    s.d.rectangle([x, y, x + w, y + h], outline=(150, 150, 150), width=s.W(0.3))
    for v in (0, 0.25, 0.5, 0.75, 1.0):
        s.d.line([(x, py(v)), (x + w, py(v))], fill=(225, 225, 225), width=s.W(0.2))
        s.d.text((x - s.mm(1.5), py(v)), '%d' % (v * 100), font=s.F(2.2), fill=(120, 120, 120),
                 anchor='rm')
    for yy in years:
        if yy % 5 == 0 or yy in (y0, y1):
            s.d.text((px(yy), y + h + s.mm(1.2)), str(yy), font=s.F(2.2), fill=(120, 120, 120),
                     anchor='ma')
    for key, col, wdt in (('полог_доля', (20, 130, 60), 0.7), ('пар_доля', (200, 90, 20), 0.7)):
        pts = [(px(yy), py(rows[yy][key])) for yy in years]
        s.d.line(pts, fill=col, width=s.W(wdt), joint='curve')
        for p in pts:
            s.d.ellipse([p[0] - s.W(0.7), p[1] - s.W(0.7), p[0] + s.W(0.7), p[1] + s.W(0.7)], fill=col)

def build(path, kn, rings, meta, series, ts, egrn_ha, extra=None, years_shown=None,
          scope=None):
    """series — {год: массив NDVI}; ts — итог agroscan.timeseries.summary."""
    s = Sheet(420, 297, dpi=400, ss=2, margin_mm=8, title_mm=16)
    s.frame(outer_mm=5)
    rows = ts['ряд']; years = sorted(rows)
    s.d.text((s.PW / 2, s.margin + s.mm(2.2)),
             'Динамика зарастания земельного участка %s по данным ДЗЗ, %d–%d'
             % (kn, years[0], years[-1]), font=s.F(4.4, True), fill='black', anchor='ma')
    s.d.text((s.PW / 2, s.margin + s.mm(8.2)),
             'Landsat 2/5/7/8 (архив ESRI) · %d безоблачных летних сцен · '
             'статистика считается по %s' % (len(years), scope or 'всему участку'),
             font=s.F(2.8, True), fill=(120, 0, 0), anchor='ma')
    s.d.rectangle([s.IN0, s.margin, s.IN1, s.margin + s.title_h], outline='black', width=s.W(0.5))

    COLW = s.mm(86)
    GX0, GY0 = s.IN0 + s.mm(4), s.margin + s.title_h + s.mm(8)
    GW = s.IN1 - s.mm(6) - COLW - GX0

    # карты по ключевым годам: первый, последний с паром, первый без, последний
    if not years_shown:
        last = ts['последний_год_пара']
        cand = [years[0], last, (last + 5 if last else years[len(years) // 2]), years[-1]]
        years_shown = []
        for c in cand:
            near = min(years, key=lambda y: abs(y - (c or years[0])))
            if near not in years_shown:
                years_shown.append(near)
    n = len(years_shown)
    tw = int((GW - s.mm(4) * (n - 1)) / n); th = tw
    x = GX0
    for y in years_shown:
        im = _ndvi_image(series[y], rings, meta, (tw, th))
        s.page.paste(im, (x, GY0 + s.mm(7)))
        s.d.rectangle([x, GY0 + s.mm(7), x + tw, GY0 + s.mm(7) + th], outline=(60, 60, 60),
                      width=s.W(0.3))
        s.d.text((x, GY0), 'NDVI %d' % y, font=s.F(3.2, True), fill='black')
        r = rows[y]
        note = ('пар %s га' % fmt_ha(r['пар_га'])) if r['пар_доля'] >= 0.02 else 'пара нет'
        s.d.text((x, GY0 + s.mm(4.2)), '%s · полог %s га' % (note, fmt_ha(r['полог_га'])),
                 font=s.F(2.4), fill=(90, 90, 90))
        x += tw + s.mm(4)

    # график
    cy = GY0 + s.mm(7) + th + s.mm(14)
    ch = int(s.PH - s.margin - s.mm(20) - cy)
    s.d.text((GX0, cy - s.mm(6)), 'Доля площади участка, %', font=s.F(2.8, True), fill='black')
    for lab, col, dx in (('сомкнутый полог (NDVI > 0,70)', (20, 130, 60), 46),
                         ('пар и свежая вспашка (NDVI < 0,45)', (200, 90, 20), 108)):
        s.d.line([(GX0 + s.mm(dx), cy - s.mm(5)), (GX0 + s.mm(dx + 6), cy - s.mm(5))],
                 fill=col, width=s.W(0.8))
        s.d.text((GX0 + s.mm(dx + 7.5), cy - s.mm(6.6)), lab, font=s.F(2.4), fill=(70, 70, 70))
    _chart(s, GX0 + s.mm(4), cy, GW - s.mm(8), ch, rows)
    if ts['год_выбытия']:
        yy = ts['год_выбытия']
        xx = GX0 + s.mm(4) + (yy - years[0]) / max(years[-1] - years[0], 1) * (GW - s.mm(8))
        s.d.line([(xx, cy), (xx, cy + ch)], fill=(200, 0, 0), width=s.W(0.5))
        s.d.text((xx + s.mm(1.5), cy + s.mm(1)), 'выбытие из оборота', font=s.F(2.4, True),
                 fill=(200, 0, 0))

    # правая колонка
    tx, ty = s.IN1 - s.mm(4) - COLW, GY0
    s.d.text((tx, ty), 'Главный вывод', font=s.F(4.0, True), fill='black'); ty += s.mm(8)
    if ts['год_выбытия']:
        s.d.text((tx, ty), 'Участок обрабатывался', font=s.F(2.9), fill='black'); ty += s.mm(4.6)
        s.d.text((tx, ty), 'до %d года включительно' % ts['последний_год_пара'],
                 font=s.F(3.4, True), fill=(0, 110, 0)); ty += s.mm(5.4)
        # если следующего года в ряду нет, дата известна лишь до промежутка —
        # так и пишем, вместо того чтобы выдавать соседнюю сцену за истину
        if ts.get('год_выбытия_точный', True):
            s.d.text((tx, ty), 'и выбыл из оборота в %d-м.' % ts['год_выбытия'],
                     font=s.F(2.9), fill='black'); ty += s.mm(5)
        else:
            a, b = ts['интервал_выбытия']
            s.d.text((tx, ty), 'и выбыл из оборота между %d и %d,' % (a, b),
                     font=s.F(2.9), fill='black'); ty += s.mm(4.4)
            s.d.text((tx, ty), 'снимков за промежуток в ряду нет.',
                     font=s.F(2.5), fill=(170, 90, 0)); ty += s.mm(5)
        if 'возраст_зарастания_лет' in ts:
            s.d.text((tx, ty), 'Возраст зарастания — %d лет.' % ts['возраст_зарастания_лет'],
                     font=s.F(2.5), fill=(90, 90, 90)); ty += s.mm(5)
    else:
        s.d.text((tx, ty), 'Пар не фиксируется ни на одном снимке ряда:',
                 font=s.F(2.7), fill='black'); ty += s.mm(4.4)
        s.d.text((tx, ty), 'участок вышел из оборота до %d года.' % years[0],
                 font=s.F(2.7), fill='black'); ty += s.mm(6)
    ty += s.mm(2)
    s.d.line([tx, ty, tx + COLW - s.mm(4), ty], fill='black', width=s.W(0.5)); ty += s.mm(4)

    s.d.text((tx, ty), 'Чем это доказано', font=s.F(3.4, True), fill='black'); ty += s.mm(6)
    for y in years:
        r = rows[y]
        hot = (y == ts['последний_год_пара'] or y == ts['год_выбытия'])
        if not hot and r['пар_доля'] < 0.02 and y not in (years[0], years[-1]):
            continue
        s.d.text((tx, ty), '%d г. — пар' % y, font=s.F(2.5, hot), fill=(160, 0, 0) if hot else 'black')
        s.d.text((tx + COLW - s.mm(4), ty), '%s га' % fmt_ha(r['пар_га']),
                 font=s.F(2.5, hot), fill=(160, 0, 0) if hot else 'black', anchor='ra')
        ty += s.mm(4.2)
    ty += s.mm(3)
    s.d.multiline_text((tx, ty),
        'Пар и свежая вспашка дают летом NDVI ниже 0,45 — голая почва.\n'
        'Сомкнутый древостой держит выше 0,70 и год от года растёт.\n'
        'Разброс зелёной линии между годами — не изменение древостоя,\n'
        'а дата съёмки: июньская сцена застаёт максимум зелёной массы,\n'
        'августовская — начало усыхания листа.',
        font=s.F(2.35), fill=(70, 70, 70), spacing=s.mm(1.15))
    ty += s.mm(22)

    if extra:
        s.d.line([tx, ty, tx + COLW - s.mm(4), ty], fill='black', width=s.W(0.5)); ty += s.mm(4)
        s.d.text((tx, ty), 'Проверка другими источниками', font=s.F(3.4, True), fill='black')
        ty += s.mm(6)
        for lab, val, note in extra:
            s.d.text((tx, ty), lab, font=s.F(2.5), fill='black')
            s.d.text((tx + COLW - s.mm(4), ty), val, font=s.F(2.5, True), fill='black', anchor='ra')
            ty += s.mm(4.0)
            if note:
                s.d.text((tx + s.mm(2), ty), note, font=s.F(2.2), fill=(120, 120, 120))
                ty += s.mm(3.6)
    s.save(path)
    return path
