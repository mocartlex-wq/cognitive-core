# -*- coding: utf-8 -*-
"""Страница карты почвенных условий: заливка SoilGrids поверх снимка.

Таблица профиля отвечает на вопрос «какая почва», но не показывает, где
именно взяты числа и меняются ли они по участку. Здесь то же самое дано
по площади: снимок, поверх — ячейки модели 250 м, контур ЕГРН и точки,
из которых собрана таблица.

Заливка НЕ сглаживается: ячейка модели крупная, и интерполяция создала бы
видимость точности, которой у источника нет.
"""
import numpy as np
from PIL import Image, ImageDraw

from ..sheet import Sheet, draw_rings, fmt_ha

# Шкалы: список опорных цветов от нижнего края диапазона к верхнему.
RAMP_HUMUS = [(242, 234, 214), (196, 168, 118), (140, 104, 56), (74, 48, 22)]
RAMP_CLAY = [(228, 214, 172), (206, 178, 118), (170, 124, 78), (128, 78, 46)]
RAMP_PH = [(198, 82, 60), (222, 176, 92), (86, 156, 82), (92, 120, 190)]

LAYERS = [
    {'key': 'humus', 'title': 'Гумус в слое 0–5 см', 'unit': '%', 'ramp': RAMP_HUMUS,
     'domain': None, 'fmt': '%.1f',
     'short': 'гумус', 'sub': 'пересчитан из органического углерода, коэффициент 1,724'},
    {'key': 'clay', 'title': 'Глина в слое 0–5 см', 'unit': '%', 'ramp': RAMP_CLAY,
     'domain': None, 'fmt': '%.1f',
     'short': 'глина', 'sub': 'чем темнее, тем тяжелее гранулометрический состав'},
    {'key': 'phh2o', 'title': 'Кислотность pH (H₂O), 0–5 см', 'unit': '', 'ramp': RAMP_PH,
     'domain': (4.5, 8.0), 'fmt': '%.1f',
     'short': 'pH', 'sub': 'шкала фиксированная: красное — кислое, зелёное — близкое к нейтральному'},
]

def _ru(t):
    return t.replace('.', ',')

def ramp_color(t, ramp):
    """Цвет по доле t∈[0,1] — линейно между опорными цветами."""
    t = min(max(float(t), 0.0), 1.0)
    n = len(ramp) - 1
    i = min(int(t * n), n - 1)
    f = t * n - i
    a, b = ramp[i], ramp[i + 1]
    return tuple(int(round(a[k] + (b[k] - a[k]) * f)) for k in range(3))

def cell_edges(arr):
    """Границы ячеек: там, где значение соседа другое.

    Без них заливка читается как непрерывная поверхность, а она ступенчатая
    с шагом 250 м — и специалист должен это видеть.
    """
    e = np.zeros(arr.shape, bool)
    v = np.where(np.isfinite(arr), arr, np.nan)
    e[:, 1:] |= np.abs(np.diff(v, axis=1)) > 1e-9
    e[1:, :] |= np.abs(np.diff(v, axis=0)) > 1e-9
    return e

def _fill(base, arr, ramp, domain, size, alpha=0.55):
    """Снимок + полупрозрачная заливка ячеек + их границы."""
    fin = np.isfinite(arr)
    if not fin.any():
        return None, None
    lo, hi = domain or (float(np.nanmin(arr)), float(np.nanmax(arr)))
    if hi - lo < 1e-9:
        hi = lo + 1e-9
    t = np.clip((arr - lo) / (hi - lo), 0, 1)
    rgb = np.zeros(arr.shape + (3,), np.uint8)
    lut = np.array([ramp_color(i / 255.0, ramp) for i in range(256)], np.uint8)
    idx = np.where(fin, np.nan_to_num(t) * 255, 0).astype(np.uint8)
    rgb[:] = lut[idx]
    lay = Image.fromarray(rgb).resize(size, Image.NEAREST)
    msk = Image.fromarray((fin * int(alpha * 255)).astype(np.uint8)).resize(size, Image.NEAREST)
    img = base.copy()
    img.paste(lay, (0, 0), msk)
    ed = Image.fromarray((cell_edges(arr) & fin).astype(np.uint8) * 115).resize(size, Image.NEAREST)
    img.paste(Image.new('RGB', size, (40, 40, 40)), (0, 0), ed)
    return img, (lo, hi)

def _points(img, points):
    """Точки опробования с номерами — привязка та же, что у снимка."""
    d = ImageDraw.Draw(img)
    r = max(4, int(img.width / 95))
    for p in points:
        x, y = p['px'] * img.width, p['py'] * img.height
        d.ellipse([x - r, y - r, x + r, y + r], fill=(255, 255, 255), outline=(20, 20, 20),
                  width=max(1, r // 4))
        d.ellipse([x - r * .38, y - r * .38, x + r * .38, y + r * .38], fill=(20, 20, 20))
    return img

def _num(s, points, x0, y0, w, h):
    """Номера точек ставятся на листе, а не в картинке: шрифт тот же, что в тексте."""
    for i, p in enumerate(points, 1):
        x = x0 + p['px'] * w; y = y0 + p['py'] * h
        s.d.text((x + s.mm(2.0), y - s.mm(2.0)), str(i), font=s.F(2.6, True), fill='white',
                 anchor='lm', stroke_width=s.W(0.45), stroke_fill=(20, 20, 20))

def _scalebar(s, x0, y, w, ramp, lo, hi, fmt, unit):
    h = s.mm(3.4)
    for i in range(int(w)):
        s.d.line([(x0 + i, y), (x0 + i, y + h)], fill=ramp_color(i / max(w - 1, 1), ramp), width=1)
    s.d.rectangle([x0, y, x0 + w, y + h], outline=(70, 70, 70), width=s.W(0.25))
    u = (' ' + unit) if unit else ''
    s.d.text((x0, y + h + s.mm(1.2)), _ru(fmt % lo) + u, font=s.F(2.4), fill=(60, 60, 60))
    s.d.text((x0 + w, y + h + s.mm(1.2)), _ru(fmt % hi) + u, font=s.F(2.4), fill=(60, 60, 60),
             anchor='ra')

def _linescale(s, x0, y, meta, iw, step_m=500):
    """Линейный масштаб: без него по карте нельзя прикинуть расстояние."""
    w = iw * step_m / (meta['e1'] - meta['e0'])
    h = s.mm(1.6)
    s.d.rectangle([x0, y, x0 + w, y + h], fill='white', outline=(40, 40, 40), width=s.W(0.25))
    s.d.rectangle([x0, y, x0 + w / 2, y + h], fill=(40, 40, 40))
    s.d.text((x0, y + h + s.mm(1.2)), '0', font=s.F(2.3), fill=(60, 60, 60))
    s.d.text((x0 + w, y + h + s.mm(1.2)), '%d м' % step_m, font=s.F(2.3), fill=(60, 60, 60),
             anchor='ma')

def build(kn, rings, meta, image_path, arrays, points, place='', stats=None, egrn_ha=None,
          table_top=None):
    """Лист карты. Возвращает Sheet или None, если строить не из чего."""
    if not image_path or not arrays:
        return None
    have = [L for L in LAYERS if arrays.get(L['key']) is not None]
    if not have:
        return None

    s = Sheet(420, 297, dpi=400, ss=2, margin_mm=8, title_mm=16)
    s.frame(outer_mm=5)
    s.d.text((s.PW / 2, s.margin + s.mm(2.6)),
             'Карта почвенных условий земельного участка %s' % kn,
             font=s.F(4.2, True), fill='black', anchor='ma')
    s.d.text((s.PW / 2, s.margin + s.mm(9.4)),
             'SoilGrids v2.0 (ISRIC) · ячейка модели 250 м · подложка — космоснимок'
             + (' · площадь ЗУ %s га' % fmt_ha(egrn_ha) if egrn_ha else '')
             + (' · ' + place if place else ''),
             font=s.F(2.9, True), fill=(120, 0, 0), anchor='ma')
    s.d.rectangle([s.IN0, s.margin, s.IN1, s.margin + s.title_h], outline='black', width=s.W(0.5))

    # Размер панели считается от высоты листа: карта должна занять его
    # целиком, а колонка пояснений — забрать ровно остаток по ширине,
    # иначе между колонками панелей зияет пустая полоса.
    GX0 = s.IN0 + s.mm(4)
    GY0 = s.margin + s.title_h + s.mm(5)
    GH = s.PH - s.margin - s.mm(5) - GY0
    ih = int((GH - s.mm(34)) / 2)
    iw = int(ih * meta['W'] / meta['H'])
    room = (s.IN1 - s.mm(4) - GX0 - s.mm(6) - s.mm(90)) / 2      # не съедать текст
    if iw > room:
        iw = int(room); ih = int(iw * meta['H'] / meta['W'])
    PITCHX = iw + s.mm(6)
    PITCHY = ih + s.mm(17)
    # ширина листа шире, чем нужно квадратным панелям: остаток делим между
    # полями, иначе колонка пояснений растягивается на треть листа и подписи
    # улетают от своих значений
    TXTW = min(s.mm(122), s.IN1 - s.mm(4) - (GX0 + PITCHX + iw + s.mm(9)))
    GX0 += max(0, (s.IN1 - s.mm(4) - GX0 - (PITCHX + iw + s.mm(9) + TXTW)) / 2)

    base = Image.open(image_path).convert('RGB').resize((iw, ih), Image.LANCZOS)
    panels = [{'title': 'Космоснимок и точки опробования', 'sub':
               'по этим точкам собран профиль в таблице', 'img': base.copy()}]
    for L in have:
        img, dom = _fill(base, arrays[L['key']], L['ramp'], L['domain'], (iw, ih))
        if img is None:
            continue
        panels.append(dict(L, img=img, dom=dom))

    for i, p in enumerate(panels[:4]):
        px = GX0 + (i % 2) * PITCHX
        py = GY0 + (i // 2) * PITCHY
        s.d.text((px, py), p['title'], font=s.F(3.0, True), fill='black')
        s.d.text((px, py + s.mm(4.0)), p.get('sub', ''), font=s.F(2.35), fill=(90, 90, 90))
        iy = int(py + s.mm(7.0))
        img = draw_rings(_points(p['img'], points), rings, meta,
                         width=max(3, int(iw / 220)))
        s.page.paste(img, (int(px), iy))
        s.d.rectangle([px, iy, px + iw, iy + ih], outline=(60, 60, 60), width=s.W(0.3))
        _num(s, points, px, iy, iw, ih)
        if p.get('dom'):
            _scalebar(s, px, iy + ih + s.mm(2.4), iw, p['ramp'], p['dom'][0], p['dom'][1],
                      p['fmt'], p['unit'])
        else:
            _linescale(s, px, iy + ih + s.mm(2.8), meta, iw)

    tx = GX0 + PITCHX + iw + s.mm(9)
    ty = GY0
    s.d.text((tx, ty), 'Как читать', font=s.F(3.8, True), fill='black'); ty += s.mm(7)
    s.d.multiline_text((tx, ty),
        'Заливка — предсказание модели SoilGrids на сетке 250 м. Ступеньки\n'
        'между ячейками показаны линиями: это шаг модели, а не границы\n'
        'почвенных контуров. На участок такого размера приходится\n'
        'несколько ячеек, поэтому карта показывает тенденцию по массиву,\n'
        'а не пестроту внутри поля.\n\n'
        'Точки 1–%d — узлы, по которым запрошен профиль; значения в таблице\n'
        'приложения усреднены именно по ним.' % len(points),
        font=s.F(2.5), fill=(50, 50, 50), spacing=s.mm(1.2))
    ty += s.mm(30)

    s.d.text((tx, ty), 'Значения в границах участка', font=s.F(3.4, True), fill='black')
    ty += s.mm(6.4)
    for L in have:
        st = (stats or {}).get(L['key'])
        if not st:
            continue
        s.d.text((tx, ty), L['title'].split(' в слое')[0].split(',')[0], font=s.F(2.6),
                 fill=(60, 60, 60))
        s.d.text((tx + TXTW, ty), _ru('%s … %s (медиана %s)%s'
                 % (L['fmt'] % st[0], L['fmt'] % st[2], L['fmt'] % st[1],
                    ' ' + L['unit'] if L['unit'] else '')),
                 font=s.F(2.6, True), fill='black', anchor='ra')
        ty += s.mm(4.8)
    ty += s.mm(3)

    s.d.text((tx, ty), 'Точки опробования', font=s.F(3.4, True), fill='black'); ty += s.mm(6.2)
    for i, p in enumerate(points, 1):
        s.d.text((tx, ty), '%d' % i, font=s.F(2.6, True), fill='black')
        s.d.text((tx + s.mm(5), ty), _ru('%.4f°, %.4f°' % (p['lat'], p['lon'])),
                 font=s.F(2.5), fill=(70, 70, 70))
        vals = ', '.join(_ru('%s %s' % (L['short'], L['fmt'] % p['знач'][L['key']]))
                         for L in have if p.get('знач', {}).get(L['key']) is not None)
        s.d.text((tx + TXTW, ty), vals, font=s.F(2.5), fill=(40, 40, 40), anchor='ra')
        ty += s.mm(4.6)
    ty += s.mm(4)

    if table_top:
        s.d.text((tx, ty), 'Сверка с таблицей приложения', font=s.F(3.4, True), fill='black')
        ty += s.mm(6.2)
        s.d.text((tx, ty), 'слой', font=s.F(2.4), fill=(120, 120, 120))
        s.d.text((tx + TXTW - s.mm(30), ty), 'таблица', font=s.F(2.4), fill=(120, 120, 120),
                 anchor='ra')
        s.d.text((tx + TXTW, ty), 'среднее по карте', font=s.F(2.4), fill=(120, 120, 120),
                 anchor='ra')
        ty += s.mm(4.4)
        for L in have:
            v = [p['знач'].get(L['key']) for p in points if p.get('знач', {}).get(L['key'])]
            t = table_top.get(L['key'])
            if not v or t is None:
                continue
            m = sum(v) / len(v)
            s.d.text((tx, ty), L['short'], font=s.F(2.6), fill=(60, 60, 60))
            s.d.text((tx + TXTW - s.mm(30), ty), _ru(L['fmt'] % t), font=s.F(2.6, True),
                     fill='black', anchor='ra')
            s.d.text((tx + TXTW, ty), _ru(L['fmt'] % m), font=s.F(2.6, True),
                     fill=(0, 110, 0) if abs(m - t) <= 0.15 else (170, 90, 0), anchor='ra')
            ty += s.mm(4.6)
        s.d.text((tx, ty + s.mm(1.0)),
                 'Совпадение подтверждает, что заливка привязана к тем же данным,',
                 font=s.F(2.4), fill=(90, 90, 90))
        s.d.text((tx, ty + s.mm(4.4)),
                 'по которым посчитана таблица профиля.', font=s.F(2.4), fill=(90, 90, 90))
        ty += s.mm(11)

    s.d.multiline_text((tx, ty),
        'Ограничения источника\n'
        '· SoilGrids — модельное предсказание по мировой выборке разрезов,\n'
        '  а не полевая съёмка данного участка;\n'
        '· шаг 250 м: контур почвы уже этого размера на карте не появится;\n'
        '· шкалы гумуса и глины растянуты на диапазон кадра — сравнивать\n'
        '  цвета между разными участками нельзя, сравнивайте числа;\n'
        '· для проектной документации нужен отбор проб аккредитованной\n'
        '  лабораторией.',
        font=s.F(2.45), fill=(90, 90, 90), spacing=s.mm(1.15))
    return s
