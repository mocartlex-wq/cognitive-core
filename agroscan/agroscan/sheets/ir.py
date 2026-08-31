# -*- coding: utf-8 -*-
"""Приложение: материалы съёмки в ближнем и коротковолновом ИК.

Показывает, по каким признакам проведены границы. Композиты собираются из
того же композита Sentinel-2, на котором работает классификация: дата и
маска облаков известны, отдельный внешний источник не нужен.
"""
import numpy as np
from PIL import Image, ImageDraw

from ..sheet import Sheet, fmt_ha

COMPOSITES = [
    {'key': 'cir', 'name': 'Ближний ИК (CIR)', 'bands': ('nir', 'red', 'green'),
     'short': 'красный — живая листва',
     'why': 'Хлорофилл почти не отражает красный свет и сильно отражает ближний ИК.\n'
            'Поэтому живая растительность на этом сочетании ярко-красная, голая\n'
            'почва — серо-голубая, вода — чёрная. Древесная растительность даёт\n'
            'более тёмный и насыщенный красный, чем травяной покров: крона\n'
            'многослойна и отражает ИК сильнее.',
     'read': 'Граница ЧЗУ/1 проводится там, где насыщенный тёмно-красный полог\n'
             'сменяется светло-красной травой с просветами почвы.'},
    {'key': 'agri', 'name': 'Сельхоз-композит', 'bands': ('swir16', 'nir', 'red'),
     'short': 'зелёный — древесная, розовый — пашня',
     'why': 'Коротковолновый ИК падает с ростом влагосодержания. У древесной\n'
            'биомассы воды кратно больше, чем в траве и тем более в почве,\n'
            'поэтому древостой уходит в зелёный, залежь — в салатовый,\n'
            'а пашня и голая почва — в розово-сиреневый.',
     'read': 'Этот композит отделяет обрабатываемую пашню от залежи надёжнее\n'
             'натурального цвета: летом и то и другое зелёное, а по влаге\n'
             'они расходятся.'},
    {'key': 'swir', 'name': 'Коротковолновый ИК', 'bands': ('swir22', 'swir16', 'red'),
     'short': 'сухое — светлое, влажное — тёмное',
     'why': 'Оба канала SWIR поглощаются водой, поэтому лист показывает\n'
            'распределение влаги: свежая вспашка и сухая стерня светлые,\n'
            'сомкнутый полог и понижения с застоем влаги — тёмные.',
     'read': 'Тёмные полосы вдоль тальвегов — ложбины стока; на схеме они\n'
             'не выделяются в отдельную часть, но объясняют, почему в этих\n'
             'местах зарастание плотнее.'},
]

def composite(bands, names, lo_pct=2, hi_pct=98, gamma=0.85):
    """RGB из трёх каналов с ОБЩЕЙ растяжкой.

    Растягивать каналы по отдельности нельзя: у растительности ближний ИК
    в разы выше красного, и независимая нормировка это различие стирает —
    CIR-композит выходил бирюзовым вместо красного. Общая шкала сохраняет
    соотношение яркостей, ради которого композит и собирается.
    """
    if any(n not in bands for n in names):
        return None
    stack = np.stack([np.asarray(bands[n], np.float32) for n in names], -1)
    v = stack[np.isfinite(stack)]
    if len(v) < 30:
        return None
    hi = np.percentile(v, hi_pct)                 # общий верх — сохраняет соотношение каналов
    out = np.zeros(stack.shape, np.float32)
    for i in range(3):
        ch = stack[..., i]
        good = ch[np.isfinite(ch)]
        lo = np.percentile(good, lo_pct) if len(good) else 0.0
        out[..., i] = np.clip((np.nan_to_num(ch, nan=lo) - lo) / max(hi - lo, 1e-6), 0, 1)
    return ((out ** gamma) * 255).astype(np.uint8)

def _draw(img, rings, meta, parts=None):
    d = ImageDraw.Draw(img)
    pr = lambda e, n: ((e - meta['e0']) / (meta['e1'] - meta['e0']) * img.width,
                       (meta['n1'] - n) / (meta['n1'] - meta['n0']) * img.height)
    w = max(2, img.width // 400)
    for k, rr in (parts or {}).items():
        for r in rr:
            d.line([pr(*p) for p in r] + [pr(*r[0])], fill=(255, 255, 0), width=w)
    for r in rings:
        d.line([pr(*p) for p in r] + [pr(*r[0])], fill=(0, 255, 130), width=w + 1)
    return img

def build(path, kn, rings, meta, bands, egrn_ha, parts=None, scenes=None):
    """bands — {канал: массив} из sentinel.composite; parts — контуры ЧЗУ."""
    made = [c for c in COMPOSITES if all(b in bands for b in c['bands'])]
    if not made:
        return None
    imgs = {c['key']: composite(bands, c['bands']) for c in made}

    pages = []
    s = Sheet(420, 297, dpi=400, ss=2, margin_mm=8, title_mm=16)
    s.frame(outer_mm=5)
    s.d.text((s.PW / 2, s.margin + s.mm(2.2)),
             'Материалы съёмки в ближнем и коротковолновом инфракрасном диапазоне — %s' % kn,
             font=s.F(4.0, True), fill='black', anchor='ma')
    sub = 'Sentinel-2 L2A · медианный композит'
    if scenes:
        sub += ' из %d сцен (%s)' % (len(scenes), ', '.join(sorted({x[1][:7] for x in scenes})))
    s.d.text((s.PW / 2, s.margin + s.mm(8.2)), sub, font=s.F(2.8, True), fill=(120, 0, 0), anchor='ma')
    s.d.rectangle([s.IN0, s.margin, s.IN1, s.margin + s.title_h], outline='black', width=s.W(0.5))

    GX0, GY0 = s.IN0 + s.mm(4), s.margin + s.title_h + s.mm(8)
    GW = s.IN1 - s.mm(4) - GX0
    n = len(made)
    tw = int((GW - s.mm(5) * (n - 1)) / n); th = tw
    x = GX0
    for c in made:
        im = Image.fromarray(imgs[c['key']]).resize((tw, th), Image.LANCZOS)
        s.page.paste(_draw(im, rings, meta), (x, GY0 + s.mm(7)))
        s.d.rectangle([x, GY0 + s.mm(7), x + tw, GY0 + s.mm(7) + th], outline=(60, 60, 60),
                      width=s.W(0.3))
        s.d.text((x, GY0), c['name'], font=s.F(3.2, True), fill='black')
        s.d.text((x, GY0 + s.mm(4.2)), '%s · %s' % (' / '.join(c['bands']), c['short']),
                 font=s.F(2.4), fill=(90, 90, 90))
        s.d.multiline_text((x, GY0 + s.mm(9) + th), c['why'], font=s.F(2.4), fill=(60, 60, 60),
                           spacing=s.mm(1.2))
        x += tw + s.mm(5)
    pages.append(s)

    # по странице на композит: тот же кадр крупно, с контурами частей
    for c in made:
        p = Sheet(420, 297, dpi=400, ss=2, margin_mm=8, title_mm=14)
        p.frame(outer_mm=5)
        p.d.text((p.PW / 2, p.margin + s.mm(2.0)), '%s — %s' % (c['name'], kn),
                 font=p.F(3.8, True), fill='black', anchor='ma')
        p.d.text((p.PW / 2, p.margin + s.mm(7.4)),
                 'каналы %s · зелёным — граница ЗУ по ЕГРН, жёлтым — контуры частей'
                 % ' / '.join(c['bands']), font=p.F(2.7, True), fill=(120, 0, 0), anchor='ma')
        p.d.rectangle([p.IN0, p.margin, p.IN1, p.margin + p.title_h], outline='black', width=p.W(0.5))
        COLW = p.mm(96)
        X0, Y0 = p.IN0 + p.mm(4), p.margin + p.title_h + p.mm(6)
        W_ = p.IN1 - p.mm(6) - COLW - X0
        H_ = p.PH - p.margin - p.mm(6) - Y0
        side = min(W_, H_)
        im = Image.fromarray(imgs[c['key']]).resize((side, side), Image.LANCZOS)
        p.page.paste(_draw(im, rings, meta, parts), (X0, Y0))
        p.d.rectangle([X0, Y0, X0 + side, Y0 + side], outline=(60, 60, 60), width=p.W(0.3))
        tx, ty = p.IN1 - p.mm(4) - COLW, Y0
        p.d.text((tx, ty), 'Что видно', font=p.F(3.8, True), fill='black'); ty += p.mm(8)
        p.d.multiline_text((tx, ty), c['why'], font=p.F(2.6), fill=(50, 50, 50), spacing=p.mm(1.3))
        ty += p.mm(4.4) * (c['why'].count('\n') + 2)
        p.d.line([tx, ty, tx + COLW - p.mm(4), ty], fill='black', width=p.W(0.4)); ty += p.mm(5)
        p.d.text((tx, ty), 'Как читать границу', font=p.F(3.2, True), fill='black'); ty += p.mm(6)
        p.d.multiline_text((tx, ty), c['read'], font=p.F(2.6), fill=(50, 50, 50), spacing=p.mm(1.3))
        ty += p.mm(4.4) * (c['read'].count('\n') + 2)
        if parts:
            p.d.line([tx, ty, tx + COLW - p.mm(4), ty], fill='black', width=p.W(0.4)); ty += p.mm(5)
            p.d.text((tx, ty), 'Площади частей', font=p.F(3.2, True), fill='black'); ty += p.mm(6)
            for k in sorted(parts):
                p.d.text((tx, ty), 'ЧЗУ/%s' % k, font=p.F(2.6), fill='black')
                ty += p.mm(4.2)
        pages.append(p)

    pages[0].save(path, extra_pages=pages[1:])
    return path
