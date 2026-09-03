# -*- coding: utf-8 -*-
"""Приложение: архив снимков высокого разрешения по датам.

То, ради чего в Google Планете двигают шкалу времени: посмотреть, как
выглядел участок в прежние годы, и увидеть, когда на пашне появились
кроны. Ряд Landsat отвечает на тот же вопрос числом, но с пикселем 30 м —
отдельное дерево на нём неразличимо, а здесь видно каждое.

Источник — ESRI Wayback (архив World Imagery, срезы с 2014 года); съёмка
Google исторически наружу не отдаётся ни одним интерфейсом. Если владелец
задал ключ Maps Platform, к ряду добавляется текущий снимок Google —
сравнивать по нему полезно: съёмки разных операторов сделаны в разные дни.
"""
import math

from PIL import Image

from ..sheet import Sheet, ZUG, draw_rings, fmt_ha

Image.MAX_IMAGE_PIXELS = None
SEASON = {12: 'зима', 1: 'зима', 2: 'зима', 3: 'весна', 4: 'весна', 5: 'весна',
          6: 'лето', 7: 'лето', 8: 'лето', 9: 'осень', 10: 'осень', 11: 'осень'}

def _season(date):
    try:
        return SEASON.get(int(date[5:7]), '')
    except (ValueError, IndexError):
        return ''

def _grid(n, gw, gh, cap_h, gap):
    """Сетка, при которой панель выходит самой крупной: (колонок, строк, сторона)."""
    best = (1, n, 0)
    for cols in range(1, max(n, 1) + 1):
        rows = math.ceil(n / cols)
        side = min((gw - gap * (cols - 1)) / cols, (gh - (cap_h + gap) * rows) / rows)
        if side > best[2]:
            best = (cols, rows, side)
    return best

def build(path, kn, rings, frames, meta, egrn_ha, note='', pad=0.35):
    """frames — [(подпись, путь к снимку, пояснение), ...] по возрастанию даты.

    Все снимки сняты в одном кадре meta, поэтому контур ложится на них
    одинаково и панели сравнимы пиксель в пиксель.

    Ориентация листа выбирается по числу панелей: девять квадратных снимков
    на альбомном А3 занимали половину листа и мельчали вдвое против того,
    что помещается на книжном.
    """
    n = max(1, len(frames))
    probe = Sheet(420, 297, dpi=400, ss=2, margin_mm=8, title_mm=17)
    MM = probe.MM
    pick = None
    for w_mm, h_mm in ((420, 297), (297, 420)):
        gw = (w_mm - 2 * 8 - 8) * MM
        gh = (h_mm - 2 * 8 - 17 - 6 - 16) * MM
        cols, rows, side = _grid(n, gw, gh, 7.0 * MM, 3.0 * MM)
        if pick is None or side > pick[3]:
            pick = (w_mm, h_mm, (cols, rows), side)
    W_MM, H_MM, (cols, rows), _ = pick

    s = Sheet(W_MM, H_MM, dpi=400, ss=2, margin_mm=8, title_mm=17)
    s.frame(outer_mm=5)
    s.d.text((s.PW / 2, s.margin + s.mm(2.2)),
             'Архив снимков высокого разрешения по датам · ЗУ %s' % kn,
             font=s.F(4.0, True), fill='black', anchor='ma')
    s.d.text((s.PW / 2, s.margin + s.mm(7.8)),
             'ретроспектива для визуальной проверки: когда на участке появились кроны',
             font=s.F(2.8, True), fill=(120, 0, 0), anchor='ma')
    s.d.text((s.PW / 2, s.margin + s.mm(12.2)),
             'площадь по сведениям ЕГРН %s га · срезов %d' % (fmt_ha(egrn_ha), len(frames)),
             font=s.F(2.5), fill=(90, 90, 90), anchor='ma')
    s.d.rectangle([s.IN0, s.margin, s.IN1, s.margin + s.title_h], outline='black', width=s.W(0.5))

    # кадр обрезки: габарит участка с полями, чтобы он занимал панель целиком
    E = [p[0] for r in rings for p in r]; N = [p[1] for r in rings for p in r]
    side_m = max(max(E) - min(E), max(N) - min(N)) * (1 + 2 * pad)
    ce, cn = (max(E) + min(E)) / 2, (max(N) + min(N)) / 2
    px = lambda e, n_: (int((e - meta['e0']) / (meta['e1'] - meta['e0']) * meta['W']),
                        int((meta['n1'] - n_) / (meta['n1'] - meta['n0']) * meta['H']))
    CAP = s.mm(7.0); GAP = s.mm(3.0)
    GY0 = s.margin + s.title_h + s.mm(6)
    GW = s.IN1 - s.mm(4) - (s.IN0 + s.mm(4))
    GH = s.PH - s.margin - s.mm(16) - GY0
    # ячейки растягиваются на весь лист, а окно обрезки берёт их пропорции:
    # квадратные панели оставляли внизу пустую четверть страницы
    tw = int((GW - GAP * (cols - 1)) / cols)
    th = int((GH - (CAP + GAP) * rows) / rows)
    GX0 = int((s.PW - (cols * tw + GAP * (cols - 1))) / 2)
    asp = tw / max(th, 1)
    win_w = side_m * max(asp, 1.0)
    win_h = side_m / min(asp, 1.0)
    # окно не может выйти за снимок: иначе панель получает чёрные поля
    win_w = min(win_w, meta['e1'] - meta['e0'])
    win_h = min(win_h, meta['n1'] - meta['n0'])
    ce = min(max(ce, meta['e0'] + win_w / 2), meta['e1'] - win_w / 2)
    cn = min(max(cn, meta['n0'] + win_h / 2), meta['n1'] - win_h / 2)
    # пропорции панели — по фактическому окну, а не по желаемому
    th = min(th, int(tw * win_h / win_w))
    tw = min(tw, int(th * win_w / win_h))
    GX0 = int((s.PW - (cols * tw + GAP * (cols - 1))) / 2)
    x0, y1 = px(ce - win_w / 2, cn - win_h / 2)
    x1, y0 = px(ce + win_w / 2, cn + win_h / 2)

    for i, (cap, img_path, sub) in enumerate(frames):
        cx = GX0 + (i % cols) * (tw + GAP)
        cy = GY0 + (i // cols) * (th + CAP + GAP)
        im = Image.open(img_path).convert('RGB').resize((meta['W'], meta['H']))
        draw_rings(im, rings, meta, color=ZUG, width=max(3, meta['W'] // 220))
        im = im.crop((x0, y0, x1, y1)).resize((tw, th), Image.LANCZOS)
        s.page.paste(im, (int(cx), int(cy + CAP)))
        s.d.rectangle([cx, cy + CAP, cx + tw, cy + CAP + th], outline=(60, 60, 60),
                      width=s.W(0.3))
        s.d.text((cx, cy), cap, font=s.F(3.2, True), fill='black')
        if sub:
            s.d.text((cx, cy + s.mm(4.2)), sub, font=s.F(2.4), fill=(90, 90, 90))

    # пояснение сразу под сеткой, а не у нижней кромки: на книжном листе
    # оно иначе отрывалось от панелей и упиралось в рамку
    ny = GY0 + rows * (th + CAP + GAP) + s.mm(2)
    ny = min(ny, s.PH - s.margin - s.mm(15))
    s.d.multiline_text((GX0, ny),
                       note or 'Контур зелёным — граница ЗУ по сведениям ЕГРН.',
                       font=s.F(2.5), fill=(50, 50, 50), spacing=s.mm(1.2))
    s.save(path)
    return path
