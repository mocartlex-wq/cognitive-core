# -*- coding: utf-8 -*-
"""Приложение: почвенная характеристика участка.

Отвечает на вопрос, что делать с землёй после раскорчёвки. Все выводы
считаются из значений профиля — на другом участке лист скажет другое,
а не повторит заготовленный текст.
"""
from ..sheet import Sheet

COLS = [('Горизонт', 26), ('Глина\n%', 15), ('Пыль\n%', 15), ('Песок\n%', 15),
        ('Гумус*\n%', 16), ('pH\nH₂O', 13), ('ЕКО\nсмоль/кг', 19),
        ('Плотн.\nг/см³', 17), ('Азот\nг/кг', 15)]

def _table(s, x0, y0, title, rows, width):
    s.d.text((x0, y0), title, font=s.F(3.4, True), fill='black')
    y = y0 + s.mm(6.4)
    s.d.line([x0, y - s.mm(1.0), x0 + width, y - s.mm(1.0)], fill='black', width=s.W(0.55))
    cx = x0
    for name, w in COLS:
        s.d.multiline_text((cx + s.mm(w) / 2, y), name, font=s.F(2.5, True), fill='black',
                           anchor='ma', align='center', spacing=s.mm(0.6))
        cx += s.mm(w)
    y += s.mm(8.0)
    s.d.line([x0, y - s.mm(1.2), x0 + width, y - s.mm(1.2)], fill='black', width=s.W(0.42))
    for hz, v in rows.items():
        cx = x0
        n = lambda f, x: (f % x).replace('.', ',')      # десятичный разделитель — запятая
        vals = [hz, n('%.1f', v['clay']), n('%.1f', v['silt']), n('%.1f', v['sand']),
                n('%.1f', v['humus']), n('%.1f', v['phh2o']), '%.0f' % v.get('cec', 0),
                n('%.2f', v.get('bdod', 0)), n('%.2f', v.get('nitrogen', 0))]
        for (name, w), t in zip(COLS, vals):
            s.d.text((cx + s.mm(w) / 2, y), t, font=s.F(2.75, name == 'Горизонт'),
                     fill='black', anchor='ma')
            cx += s.mm(w)
        y += s.mm(5.6)
    s.d.line([x0, y - s.mm(0.6), x0 + width, y - s.mm(0.6)], fill='black', width=s.W(0.55))
    return y + s.mm(3.0)

def build(path, kn, rows, conclusions, egrn_ha, place='', points=0,
          wrb=None, soil_map=None, map_page=None):
    if not rows:
        return None
    s = Sheet(297, 210, dpi=400, ss=2, margin_mm=8, title_mm=13)
    s.frame(outer_mm=5)
    s.d.text((s.PW / 2, s.margin + s.mm(2.4)),
             'Почвенная характеристика земельного участка %s' % kn,
             font=s.F(4.0, True), fill='black', anchor='ma')
    s.d.text((s.PW / 2, s.margin + s.mm(8.4)),
             'SoilGrids v2.0 (ISRIC World Soil Information) · разрешение 250 м · '
             'усреднение по %d точкам%s' % (points, ' · ' + place if place else ''),
             font=s.F(2.9, True), fill=(120, 0, 0), anchor='ma')
    s.d.rectangle([s.IN0, s.margin, s.IN1, s.margin + s.title_h], outline='black', width=s.W(0.5))

    TW = sum(s.mm(w) for _, w in COLS)
    x0 = s.IN0 + s.mm(4)
    y = s.margin + s.title_h + s.mm(6)

    # Тип почвы: то, чего в свойствах нет, а агроном спрашивает первым.
    # Два источника разной природы — модельная классификация и бумажная
    # карта; расхождение между ними не сглаживаем, а показываем.
    if wrb or soil_map:
        s.d.text((x0, y), 'Тип почвы', font=s.F(3.4, True), fill='black')
        y += s.mm(6)
        if wrb:
            p = dict(wrb.get('вероятности') or []).get(wrb.get('wrb'))
            s.d.text((x0, y), 'Классификация WRB (SoilGrids):', font=s.F(2.6), fill=(60, 60, 60))
            s.d.text((x0 + s.mm(52), y), '%s%s' % (wrb['wrb'], ' — %d %%' % p if p else ''),
                     font=s.F(2.7, True), fill='black')
            y += s.mm(4.4)
            if wrb.get('русское_соответствие'):
                s.d.text((x0 + s.mm(52), y), 'ориентировочно: %s' % wrb['русское_соответствие'],
                         font=s.F(2.5), fill=(90, 90, 90))
                y += s.mm(4.2)
            # из кэша вероятности приходят списками, из сети — кортежами:
            # без tuple() форматирование списка валит весь лист
            other = [tuple(x) for x in (wrb.get('вероятности') or []) if x[0] != wrb['wrb']][:3]
            if other:
                s.d.text((x0 + s.mm(52), y), 'далее: ' + ', '.join('%s %d %%' % x for x in other),
                         font=s.F(2.4), fill=(120, 120, 120))
                y += s.mm(4.6)
        if soil_map and soil_map.get('название'):
            s.d.text((x0, y), 'Почвенная карта РСФСР:', font=s.F(2.6), fill=(60, 60, 60))
            s.d.text((x0 + s.mm(52), y), '%s (индекс %s)'
                     % (soil_map['название'], soil_map.get('индекс', '—')),
                     font=s.F(2.7, True), fill='black')
            y += s.mm(4.4)
            s.d.text((x0 + s.mm(52), y),
                     'масштаб 1:2 500 000 — контур измеряется километрами '
                     'и характеризует массив, а не участок',
                     font=s.F(2.4), fill=(120, 120, 120))
            y += s.mm(4.6)
        y += s.mm(3)

    y = _table(s, x0, y, 'Профиль по горизонтам · площадь участка %s га'
               % ('%.2f' % egrn_ha).replace('.', ','), rows, TW)
    s.d.text((x0, y + s.mm(1.0)),
             '* гумус пересчитан из органического углерода коэффициентом 1,724; '
             'ЕКО — ёмкость катионного обмена', font=s.F(2.4), fill=(110, 110, 110))
    y += s.mm(9)

    # профиль в разрезе: состав полосами и кривая гумуса по глубине —
    # таблица одна оставляла половину листа пустой
    CLAY, SILT, SAND = (150, 95, 60), (200, 175, 110), (225, 210, 165)
    s.d.text((x0, y), 'Гранулометрический состав по горизонтам',
             font=s.F(3.2, True), fill='black')
    y += s.mm(7)
    bw = TW - s.mm(34)
    for hz, v in rows.items():
        s.d.text((x0, y + s.mm(2.4)), hz, font=s.F(2.6), fill='black', anchor='lm')
        bx = x0 + s.mm(24)
        tot = v['clay'] + v['silt'] + v['sand']
        for val, col, lab in ((v['clay'], CLAY, 'глина'), (v['silt'], SILT, 'пыль'),
                              (v['sand'], SAND, 'песок')):
            w = int(bw * val / max(tot, 1e-6))
            s.d.rectangle([bx, y, bx + w, y + s.mm(4.8)], fill=col, outline=(90, 90, 90),
                          width=s.W(0.2))
            if w > s.mm(12):
                s.d.text((bx + w / 2, y + s.mm(2.4)), ('%.0f %%' % val),
                         font=s.F(2.4), fill=(30, 30, 30), anchor='mm')
            bx += w
        y += s.mm(6.4)
    y += s.mm(2)
    lx = x0 + s.mm(24)
    for col, lab in ((CLAY, 'глина'), (SILT, 'пыль'), (SAND, 'песок')):
        s.d.rectangle([lx, y, lx + s.mm(4), y + s.mm(3)], fill=col, outline=(90, 90, 90),
                      width=s.W(0.2))
        s.d.text((lx + s.mm(5.4), y + s.mm(1.5)), lab, font=s.F(2.4), fill=(70, 70, 70),
                 anchor='lm')
        lx += s.mm(22)
    y += s.mm(10)

    s.d.text((x0, y), 'Гумус и плотность по глубине', font=s.F(3.2, True), fill='black')
    y += s.mm(7)
    gh = s.mm(34); gw = TW - s.mm(34)
    gx = x0 + s.mm(24)
    hmax = max(max(v['humus'] for v in rows.values()), 4.0)
    s.d.rectangle([gx, y, gx + gw, y + gh], outline=(160, 160, 160), width=s.W(0.25))
    n_h = len(rows)
    for i, (hz, v) in enumerate(rows.items()):
        yy = y + gh * (i + 0.5) / n_h
        w = gw * v['humus'] / hmax
        s.d.rectangle([gx, yy - s.mm(3), gx + w, yy + s.mm(3)], fill=(90, 70, 40))
        s.d.text((gx - s.mm(1.5), yy), hz, font=s.F(2.5), fill='black', anchor='rm')
        lab = ('%.1f %% гумуса · %.2f г/см³' % (v['humus'], v.get('bdod', 0))).replace('.', ',')
        if w > gw * 0.72:            # длинная полоса — подпись внутрь, иначе лезет в колонку
            s.d.text((gx + w - s.mm(2), yy), lab, font=s.F(2.5), fill='white', anchor='rm')
        else:
            s.d.text((gx + w + s.mm(1.5), yy), lab, font=s.F(2.5), fill=(60, 60, 60), anchor='lm')
    y += gh + s.mm(4)

    tx = x0 + TW + s.mm(8)
    COLW = s.IN1 - s.mm(4) - tx
    ty = s.margin + s.title_h + s.mm(6)
    s.d.text((tx, ty), 'Что это значит для вовлечения в оборот',
             font=s.F(3.6, True), fill='black')
    ty += s.mm(7.4)
    for head, lines, col in conclusions:
        s.d.text((tx, ty), head, font=s.F(2.65, True), fill=col or (0, 0, 0))
        ty += s.mm(4.3)
        for ln in lines:
            s.d.text((tx, ty), ln, font=s.F(2.65), fill=(0, 0, 0))
            ty += s.mm(4.3)
        ty += s.mm(2.4)

    s.d.text((x0, s.PH - s.margin - s.mm(12.6)),
             'Ограничение источника: SoilGrids — модельное предсказание с шагом 250 м; '
             'значения характеризуют массив в целом и не заменяют полевое обследование.',
             font=s.F(2.5), fill=(90, 90, 90))
    s.d.text((x0, s.PH - s.margin - s.mm(9.0)),
             'Для проектной документации нужен почвенный анализ аккредитованной лабораторией. '
             'Гумус в слое 0–5 см завышен: модель относит к нему лесную подстилку.',
             font=s.F(2.5), fill=(90, 90, 90))
    # карта идёт второй страницей того же приложения: специалист смотрит
    # цифры и тут же видит, откуда они и как меняются по участку
    s.save(path, extra_pages=[map_page] if map_page is not None else ())
    return path
