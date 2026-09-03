# -*- coding: utf-8 -*-
"""Приложение: рельеф и овражно-балочная сеть.

Отвечает на один вопрос заказчика — почему из площади мероприятий не
исключены овраги. Поэтому лист показывает не только вывод, но и каждую
найденную форму с измерениями и причиной, по которой она отклонена.
"""
import numpy as np
from PIL import Image

from ..sheet import Sheet, draw_rings, fmt_ha

def _hillshade(z, cell_m, az=315.0, alt=45.0):
    gy, gx = np.gradient(z.astype(float), cell_m)
    slope = np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gx, gy)
    a, z0 = np.radians(az), np.radians(alt)
    v = np.sin(z0) * np.cos(slope) + np.cos(z0) * np.sin(slope) * np.cos(a - aspect)
    return np.clip(v, 0, 1)

def _relief_image(res, size):
    """Отмывка с гипсометрической окраской и тальвегами."""
    z, msk = res['z'], res['msk']
    lo, hi = np.percentile(z, 2), np.percentile(z, 98)
    t = np.clip((z - lo) / max(hi - lo, 1e-6), 0, 1)
    rgb = np.stack([0.35 + 0.6 * t, 0.75 - 0.25 * t, 0.45 - 0.3 * t], -1)
    sh = _hillshade(z, res['grid_m'])[..., None]
    rgb = np.clip(rgb * (0.55 + 0.75 * sh), 0, 1)
    water = res['acc'] >= 3.0
    rgb[water] = np.array([0.20, 0.45, 0.85])
    rgb[~msk] *= 0.55
    im = Image.fromarray((rgb * 255).astype(np.uint8))
    return im.resize(size, Image.LANCZOS)

def build(path, kn, rings, res, egrn_ha, meta, image_path=None,
          source='Copernicus DEM GLO-30'):
    s = Sheet(297, 210, dpi=400, ss=2, margin_mm=8, title_mm=15)
    s.frame(outer_mm=5)
    st = res['stats']
    s.d.text((s.PW / 2, s.margin + s.mm(2.2)),
             'Проверка овражно-балочной сети на земельном участке %s' % kn,
             font=s.F(4.0, True), fill='black', anchor='ma')
    s.d.text((s.PW / 2, s.margin + s.mm(7.6)),
             'рельеф — %s · сетка расчёта %.0f м · координаты в местной СК'
             % (source, res['grid_m']), font=s.F(2.7, True), fill=(120, 0, 0), anchor='ma')
    s.d.rectangle([s.IN0, s.margin, s.IN1, s.margin + s.title_h], outline='black', width=s.W(0.5))

    COLW = s.mm(88)
    GX0, GY0 = s.IN0 + s.mm(4), s.margin + s.title_h + s.mm(8)
    GW = s.IN1 - s.mm(6) - COLW - GX0
    GH = s.PH - s.margin - s.mm(6) - GY0
    tw = int((GW - s.mm(5)) / 2); th = min(int(GH * 0.98), tw)

    overlay = lambda img: draw_rings(img, rings, meta)

    x = GX0
    if image_path:
        im = Image.open(image_path).convert('RGB')
        s.page.paste(overlay(im).resize((tw, th), Image.LANCZOS), (x, GY0 + s.mm(8.4)))
        s.d.text((x, GY0), 'Космоснимок', font=s.F(3.0, True), fill='black')
        s.d.text((x, GY0 + s.mm(3.8)), 'кроны закрывают рельеф — по снимку он не читается',
                 font=s.F(2.3), fill=(90, 90, 90))
        s.d.rectangle([x, GY0 + s.mm(8.4), x + tw, GY0 + s.mm(8.4) + th],
                      outline=(60, 60, 60), width=s.W(0.3))
        x += tw + s.mm(5)
    rel = overlay(_relief_image(res, (tw, th)))
    # 8,4 мм — две строки подписи: при 6 мм вторая строка наезжала на картинку
    s.page.paste(rel, (x, GY0 + s.mm(8.4)))
    s.d.rectangle([x, GY0 + s.mm(8.4), x + tw, GY0 + s.mm(8.4) + th],
                  outline=(60, 60, 60), width=s.W(0.3))
    s.d.text((x, GY0), 'Рельеф: отмывка и линии стока', font=s.F(3.0, True), fill='black')
    s.d.text((x, GY0 + s.mm(3.8)), 'перепад %.1f м · синим — линии стока с водосбором от 3 га' % st['перепад_м'],
             font=s.F(2.3), fill=(90, 90, 90))

    tx, ty = s.IN1 - s.mm(4) - COLW, GY0
    s.d.text((tx, ty), 'Результат', font=s.F(4.0, True), fill='black'); ty += s.mm(8)
    verdict = ('в границах ЗУ НЕ ВЫЯВЛЕНО' if not st['оврагов']
               else 'выявлено форм: %d' % st['оврагов'])
    s.d.text((tx, ty), 'Овражно-балочных форм', font=s.F(2.9), fill='black'); ty += s.mm(4.6)
    s.d.text((tx, ty), verdict, font=s.F(3.2, True),
             fill=(160, 0, 0) if not st['оврагов'] else (0, 110, 0)); ty += s.mm(5.2)
    s.d.text((tx, ty), 'площадь под исключение — %s га' % fmt_ha(st['исключается_га']),
             font=s.F(2.5), fill=(90, 90, 90)); ty += s.mm(7)
    s.d.line([tx, ty, tx + COLW - s.mm(4), ty], fill='black', width=s.W(0.5)); ty += s.mm(4)

    s.d.text((tx, ty), 'Измерено', font=s.F(3.4, True), fill='black'); ty += s.mm(6)
    for lab, val in (('Перепад высот в границах ЗУ', '%.1f м' % st['перепад_м']),
                     ('Уклон, медиана', '%.1f°' % st['уклон_медиана']),
                     ('Уклон, 90-й процентиль', '%.1f°' % st['уклон_p90']),
                     ('Уклон, максимум', '%.1f°' % st['уклон_макс']),
                     ('Отклонение от среднего (200 м)', '%.2f … %.2f м' % (st['tpi_мин'], st['tpi_макс'])),
                     ('Наибольший водосбор тальвега', '%.1f га' % st['водосбор_макс_га'])):
        s.d.text((tx, ty), lab, font=s.F(2.5), fill=(60, 60, 60))
        s.d.text((tx + COLW - s.mm(4), ty), val, font=s.F(2.5, True), fill='black', anchor='ra')
        ty += s.mm(4.6)
    ty += s.mm(3)
    s.d.line([tx, ty, tx + COLW - s.mm(4), ty], fill='black', width=s.W(0.5)); ty += s.mm(4)

    s.d.text((tx, ty), 'Что нашлось и почему отклонено', font=s.F(3.4, True), fill='black')
    ty += s.mm(6)
    if not res['формы']:
        s.d.text((tx, ty), 'Формы, отвечающие критериям тальвега,', font=s.F(2.5), fill=(60, 60, 60))
        ty += s.mm(3.8)
        s.d.text((tx, ty), 'в границах участка не обнаружены.', font=s.F(2.5), fill=(60, 60, 60))
        ty += s.mm(5)
    for f in res['формы'][:4]:
        s.d.text((tx, ty), 'Ложбина %d × %d м' % (f['длина_м'], f['ширина_м']),
                 font=s.F(2.6, True), fill='black')
        s.d.text((tx + COLW - s.mm(4), ty), '%s га' % fmt_ha(f['га']), font=s.F(2.6, True),
                 fill='black', anchor='ra')
        ty += s.mm(4.0)
        s.d.text((tx + s.mm(2), ty), 'врез %.1f м → борт %.1f°, водосбор %.1f га'
                 % (f['врез_м'], f['борт_град'], f['водосбор_га']), font=s.F(2.3), fill=(90, 90, 90))
        s.d.text((tx + COLW - s.mm(4), ty), 'ОВРАГ' if f['овраг'] else 'отклонена',
                 font=s.F(2.4, True), fill=(0, 110, 0) if f['овраг'] else (170, 90, 0), anchor='ra')
        ty += s.mm(5.4)
    ty += s.mm(2)
    s.d.multiline_text((tx, ty),
        'Оврагом считается врез с крутыми бортами: техника его не проходит,\n'
        'и площадь исключается из оборота. Форма с бортом положе 8° — ложбина\n'
        'стока, обработке она не мешает и из площади мероприятий не исключается.\n\n'
        'Как искалось: по DEM построены D8-направления стока и накопление\n'
        'водосбора; тальвегом считалась линия с водосбором от 5 га и понижением\n'
        'относительно окружения, телом долины — примыкающее к ней понижение.\n'
        'Дальше отбирались формы длиннее 150 м, площадью от 0,30 га\n'
        'и вытянутостью от 1,6.\n\n'
        'Ограничение: собственная точность DEM около 30 м, промоина уже 30 м\n'
        'на нём не разрешается. Проверить её по снимку нельзя — участок\n'
        'под пологом; детальный рельеф на территорию отсутствует.',
        font=s.F(2.35), fill=(60, 60, 60), spacing=s.mm(1.15))
    # шкала высот под картами: низ листа иначе пустует, а без шкалы
    # гипсометрическая окраска — просто цветные пятна
    import numpy as _np
    z = res['z'][res['msk']]
    lo, hi = float(_np.percentile(res['z'], 2)), float(_np.percentile(res['z'], 98))
    by = GY0 + s.mm(8.4) + th + s.mm(9)
    bx0, bx1 = GX0, GX0 + GW - s.mm(5)
    bh = s.mm(5)
    for i in range(int(bx1 - bx0)):
        t = i / max(bx1 - bx0 - 1, 1)
        col = (int(255 * (0.35 + 0.6 * t)), int(255 * (0.75 - 0.25 * t)),
               int(255 * (0.45 - 0.3 * t)))
        s.d.line([(bx0 + i, by), (bx0 + i, by + bh)], fill=col, width=1)
    s.d.rectangle([bx0, by, bx1, by + bh], outline=(60, 60, 60), width=s.W(0.3))
    s.d.text((bx0, by + bh + s.mm(1.4)), '%.0f м' % lo, font=s.F(2.4), fill=(60, 60, 60))
    s.d.text((bx1, by + bh + s.mm(1.4)), '%.0f м' % hi, font=s.F(2.4), fill=(60, 60, 60), anchor='ra')
    s.d.text(((bx0 + bx1) / 2, by - s.mm(4.4)), 'Абсолютные отметки, м (Copernicus DEM GLO-30)',
             font=s.F(2.6, True), fill='black', anchor='ma')
    s.d.text(((bx0 + bx1) / 2, by + bh + s.mm(1.4)),
             'в границах участка %.0f … %.0f м' % (float(z.min()), float(z.max())),
             font=s.F(2.4), fill=(60, 60, 60), anchor='ma')
    s.save(path)
    return path
