# -*- coding: utf-8 -*-
"""Проверочная карта: контуры частей без заливки поверх снимка.

Лист для сверки специалистом на местности: границы показаны только линиями,
подложка не закрыта штриховкой, поэтому видно, по чему именно проведена
каждая граница. Две подложки разных дат и разрешения — один и тот же контур
можно сравнить на обеих.
"""
import numpy as np
from PIL import Image

from ..sheet import Sheet, fmt_ha

Image.MAX_IMAGE_PIXELS = None
# на снимке нужны яркие цвета, печатные из schema.py на тёмной подложке не видны
COL = {'1': (30, 90, 255), '2': (255, 220, 50), '3': (255, 60, 220), '4': (255, 70, 20)}
ZUG = (0, 255, 120)
HINT = {
    '1': 'под раскорчёвку. Граница проведена по краю\nсомкнутого полога: внутри кроны смыкаются,\nснаружи видны просветы и травяной покров.',
    '2': 'вовлекается без раскорчёвки. Внутри контура\nна снимке травяной покров и одиночный\nкустарник, крон нет.',
    '3': 'мероприятия не проводятся. Границы взяты\nиз КПТ, а не со снимка: сверять по выписке,\nа не по местности.',
    '4': 'раскорчёвке не подлежат. Контуры обведены\nправообладателем вручную; автопоиск по высоте\nполога идёт кандидатами и в состав не входит.',
}
NAME = {'1': 'ЧЗУ/%s — древесно-кустарниковая', '2': 'ЧЗУ/%s — залежь без древесной',
        '3': 'ЧЗУ/%s — ЗОУИТ', '4': 'ЧЗУ/%s — защитные лесные полосы'}

def build(path, kn, rings, parts, egrn_ha, meta, backdrops, fragment=None):
    """backdrops — [(путь, подпись, пояснение), ...]; fragment — (E, N, размер_м)."""
    s = Sheet(420, 297, dpi=400, ss=2, margin_mm=8, title_mm=17)
    s.frame(outer_mm=5)
    s.d.text((s.PW / 2, s.margin + s.mm(2.4)),
             'Проверочная карта: контуры частей ЗУ %s без заливки' % kn,
             font=s.F(4.4, True), fill='black', anchor='ma')
    s.d.text((s.PW / 2, s.margin + s.mm(8.4)),
             'лист для сверки специалистом · подложка не закрыта штриховкой'
             + (' · %d съёмки разных дат и разрешения' % len(backdrops) if len(backdrops) > 1 else ''),
             font=s.F(2.9, True), fill=(120, 0, 0), anchor='ma')
    s.d.text((s.PW / 2, s.margin + s.mm(12.8)),
             'площадь по сведениям ЕГРН %s га' % fmt_ha(egrn_ha),
             font=s.F(2.6), fill=(90, 90, 90), anchor='ma')
    s.d.rectangle([s.IN0, s.margin, s.IN1, s.margin + s.title_h], outline='black', width=s.W(0.5))

    keys = sorted(parts)
    COLW = s.mm(92)
    GX0, GY0 = s.IN0 + s.mm(3), s.margin + s.title_h + s.mm(9)
    GW = s.IN1 - s.mm(8) - COLW - GX0
    GH = int(s.PH - s.margin - s.mm(3) - GY0)
    n = max(1, len(backdrops))
    tile_w = int((GW - s.mm(6) * (n - 1)) / n)
    tile_h = min(int(GH * 0.72), tile_w)

    # Толщина линий задаётся в миллиметрах листа, но рисуем мы по исходной
    # картинке, которую потом масштабируют в плитку. На мелком участке
    # (909 px кадра против 3500 px плитки) линия раздувалась в пять раз и
    # съедала сам контур — поэтому переводим мм в пиксели картинки.
    def px_of_mm(mm_w, img_w, tile_px, floor=1):
        k = tile_px / max(img_w, 1)
        return max(floor, int(round(s.W(mm_w) / max(k, 1e-6))))

    def draw_on(img, S, Sh, thin=1.0, tile_px=None):
        from PIL import ImageDraw
        od = ImageDraw.Draw(img)
        pr = lambda e, nn: ((e - meta['e0']) / (meta['e1'] - meta['e0']) * S,
                            (meta['n1'] - nn) / (meta['n1'] - meta['n0']) * Sh)
        # граница ЕГРН первой и толще: ЧЗУ/1 почти совпадает с ней по контуру
        # и, нарисованная сверху, полностью прятала синюю линию
        tp = tile_px or S
        for r in rings:
            od.line([pr(*p) for p in r] + [pr(*r[0])], fill=ZUG,
                    width=px_of_mm(0.80 * thin, S, tp, 2))
        for k in keys:
            for key, mult in (('outer', 1.0), ('inner', 0.8)):
                for r in parts[k].get(key, []):
                    od.line([pr(*p) for p in r] + [pr(*r[0])], fill=COL.get(k, (255, 255, 255)),
                            width=px_of_mm(0.28 * mult * thin, S, tp, 1))
        return img

    x = GX0
    for path_img, cap, sub in backdrops:
        im = Image.open(path_img).convert('RGB').resize((meta['W'], meta['H']))
        im = draw_on(im, meta['W'], meta['H'], thin=1.0,
                     tile_px=tile_w).resize((tile_w, tile_h), Image.LANCZOS)
        s.page.paste(im, (x, GY0 + s.mm(9)))
        s.d.rectangle([x, GY0 + s.mm(9), x + tile_w, GY0 + s.mm(9) + tile_h],
                      outline=(60, 60, 60), width=s.W(0.3))
        s.d.text((x, GY0), cap, font=s.F(3.1, True), fill='black')
        s.d.text((x, GY0 + s.mm(4.4)), sub, font=s.F(2.4), fill=(90, 90, 90))
        x += tile_w + s.mm(6)

    # фрагмент крупным планом
    if fragment:
        fe, fn, fsize = fragment
        fy = GY0 + s.mm(9) + tile_h + s.mm(9)
        fh = int(s.PH - s.margin - s.mm(3) - fy)
        fx = GX0
        for path_img, cap, _ in backdrops:
            im = Image.open(path_img).convert('RGB').resize((meta['W'], meta['H']))
            # во фрагменте кадр обрезан: в плитку попадает только окно fsize,
            # поэтому масштаб считаем по нему, а не по всей картинке
            im = draw_on(im, meta['W'], meta['H'],
                         tile_px=fh * (meta['W'] * meta['mpp']) / max(fsize, 1e-6))
            px = lambda e, nn: (int((e - meta['e0']) / (meta['e1'] - meta['e0']) * meta['W']),
                                int((meta['n1'] - nn) / (meta['n1'] - meta['n0']) * meta['H']))
            x0, y1 = px(fe - fsize / 2, fn - fsize / 2)
            x1, y0 = px(fe + fsize / 2, fn + fsize / 2)
            im = im.crop((x0, y0, x1, y1)).resize((fh, fh), Image.LANCZOS)
            s.page.paste(im, (fx, fy))
            s.d.rectangle([fx, fy, fx + fh, fy + fh], outline=(60, 60, 60), width=s.W(0.3))
            s.d.text((fx, fy - s.mm(4.4)), 'Фрагмент %d × %d м — %s' % (fsize, fsize, cap),
                     font=s.F(2.6, True), fill='black')
            fx += fh + s.mm(6)

    # колонка «Что проверять»
    tx, ty = s.IN1 - s.mm(3) - COLW, GY0
    s.d.text((tx, ty), 'Что проверять', font=s.F(4.2, True), fill='black')
    ty += s.mm(8.5)
    rows = [(COL[k], NAME.get(k, 'ЧЗУ/%s') % k, fmt_ha(parts[k]['areaHa']) + ' га', HINT.get(k, ''))
            for k in keys]
    rows.append((ZUG, 'Граница ЗУ по сведениям ЕГРН', fmt_ha(egrn_ha) + ' га',
                 'контрольная: площадь по координатам\nсовпала с ЕГРН.'))
    for col, nm, ar, txt in rows:
        s.d.line([tx, ty + s.mm(1.6), tx + s.mm(9), ty + s.mm(1.6)], fill=col, width=s.W(1.1))
        s.d.text((tx + s.mm(11), ty), nm, font=s.F(2.9, True), fill='black')
        s.d.text((tx + COLW - s.mm(4), ty), ar, font=s.F(2.9, True), fill='black', anchor='ra')
        s.d.multiline_text((tx + s.mm(11), ty + s.mm(4.6)), txt, font=s.F(2.5), fill=(70, 70, 70),
                           spacing=s.mm(1.1))
        ty += s.mm(17.5)
    ty += s.mm(1)
    s.d.line([tx, ty, tx + COLW - s.mm(4), ty], fill='black', width=s.W(0.5))
    ty += s.mm(4)
    tot = sum(parts[k]['areaHa'] for k in keys if k in ('1', '2'))
    s.d.text((tx, ty), 'Итого под вовлечение в оборот', font=s.F(3.0, True), fill='black')
    s.d.text((tx + COLW - s.mm(4), ty), fmt_ha(tot) + ' га', font=s.F(3.0, True),
             fill=(160, 0, 0), anchor='ra')
    ty += s.mm(5.6)
    s.d.text((tx, ty), 'ЧЗУ/1 + ЧЗУ/2, без ЗОУИТ и лесополос', font=s.F(2.5), fill=(110, 110, 110))
    ty += s.mm(7.5)
    # пункты только про те части, что есть на листе: на :74 нет ни ЗОУИТ,
    # ни лесополос, и советы про них сбивают проверяющего с толку
    steps = []
    if '1' in keys and '2' in keys:
        steps.append('Взять точку на границе ЧЗУ/1 и ЧЗУ/2 —\n'
                     '   под ногами должна меняться сомкнутость:\n'
                     '   с одной стороны полог, с другой трава.')
    if '4' in keys:
        steps.append('Пройти вдоль контура ЧЗУ/4 — это должен\n'
                     '   быть ряд взрослых деревьев, а не самосев.')
    if '2' in keys:
        steps.append('Проверить, что внутри ЧЗУ/2 нет деревьев\n'
                     '   выше 2 м: если есть, контур занижен.')
    if '3' in keys:
        steps.append('Границы ЧЗУ/3 сверять по выписке ЕГРН,\n'
                     '   на местности они ничем не обозначены.')
    s.d.multiline_text((tx, ty), 'Как проверять на местности\n'
                       + '\n'.join('%d. %s' % (i, t) for i, t in enumerate(steps, 1)),
                       font=s.F(2.55), fill=(50, 50, 50), spacing=s.mm(1.3))
    s.save(path)
    return path
