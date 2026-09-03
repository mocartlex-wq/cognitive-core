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


def _wrap(s, text, font, width, limit=2):
    """Разбить строку по ширине колонки: мельчить шрифт до нечитаемого нельзя."""
    words, lines, cur = text.split(), [], ''
    for w in words:
        t = (cur + ' ' + w).strip()
        if cur and s.d.textlength(t, font=font) > width:
            lines.append(cur); cur = w
            if len(lines) == limit:
                break
        else:
            cur = t
    if cur and len(lines) < limit:
        lines.append(cur)
    return lines

def _sources(s, x0, y, width, wrb, soil_map, rows, points):
    """Таблица «карта против модели»: что говорит каждый источник и в чём спор.

    Владелец просил показывать оба источника разом: данные у них рознятся,
    и выбирать за специалиста, какому верить, здесь неправильно.
    """
    from ..sources.soil import texture_class
    fr = soil_map or {}
    w = wrb or {}
    prob = dict((k, v) for k, v in (w.get('вероятности') or []))
    top = list(rows.values())[0] if rows else {}
    tex = texture_class(top.get('clay', 0), top.get('silt', 0), top.get('sand', 0)) \
        if top else '—'
    nxt = [tuple(q) for q in (w.get('вероятности') or []) if q[0] != w.get('wrb')][:2]
    grain = ('%s: глина %.0f %%, пыль %.0f %%, песок %.0f %%'
             % (tex, top['clay'], top['silt'], top['sand'])) if top else '—'
    lines = [
        ('Тип почвы',
         '%s%s' % (fr.get('название', '—'),
                   ' (%s, код %s)' % (fr.get('индекс'), fr.get('код'))
                   if fr.get('индекс') else ''),
         '%s%s%s' % (w.get('wrb', '—'),
                     ' — %d %%' % prob[w['wrb']] if prob.get(w.get('wrb')) else '',
                     '; ' + w['русское_соответствие'] if w.get('русское_соответствие') else '')),
        ('Гранулометрия',
         '%s — почвообразующая порода' % (fr.get('порода') or '—').lower(),
         '%s — слой 0–5 см' % grain),
        ('Что рядом',
         ', '.join(fr.get('сопутствующие') or ['—']).lower(),
         ', '.join('%s %d %%' % q for q in nxt) or '—'),
        ('Что описывает',
         'массив: контур %s, %s км², масштаб 1:2 500 000'
         % (fr.get('id', '—'), ('%.0f' % fr['площадь_км2']) if fr.get('площадь_км2') else '—'),
         'ячейка 250 м, среднее по %d точкам в границах ЗУ' % points),
    ]
    s.d.text((x0, y), 'Тип почвы: два источника рядом', font=s.F(3.4, True), fill='black')
    y += s.mm(5.6)
    LW = s.mm(26)
    cw = (width - LW) / 2 - s.mm(2)
    c1, c2 = x0 + LW, x0 + LW + cw + s.mm(4)
    s.d.line([x0, y - s.mm(0.8), x0 + width, y - s.mm(0.8)], fill='black', width=s.W(0.5))
    for xx, t in ((c1, 'Почвенная карта РСФСР (Фридланд, 1988)'),
                  (c2, 'SoilGrids v2.0 (ISRIC)')):
        s.d.text((xx, y), t, font=s.F(2.45, True), fill=(60, 60, 60))
    y += s.mm(4.2)
    s.d.line([x0, y - s.mm(0.8), x0 + width, y - s.mm(0.8)], fill='black', width=s.W(0.3))
    f = s.F(2.4)
    for name, a, b in lines:
        la, lb = _wrap(s, a, f, cw), _wrap(s, b, f, cw)
        s.d.text((x0, y), name, font=s.F(2.4), fill=(90, 90, 90))
        for xx, ll in ((c1, la), (c2, lb)):
            yy = y
            for t in ll:
                s.d.text((xx, yy), t, font=f, fill='black')
                yy += s.mm(3.2)
        y += s.mm(3.2) * max(len(la), len(lb)) + s.mm(1.2)
    s.d.line([x0, y - s.mm(0.6), x0 + width, y - s.mm(0.6)], fill='black', width=s.W(0.5))
    y += s.mm(1.6)

    # расхождения считаются, а не пишутся заготовкой
    diff = []
    key = (fr.get('название') or '').split()[0].lower().replace('ё', 'е')[:7]
    ru = (w.get('русское_соответствие') or '').lower().replace('ё', 'е')
    if key and ru:
        diff.append('ряд почв — источники %s (карта «%s», модель «%s»)'
                    % ('согласуются' if key in ru else 'расходятся',
                       (fr.get('название') or '').lower(), w.get('wrb', '—')))
    pr = (fr.get('порода') or '').lower()
    if pr and top:
        same = pr.split()[0][:5] in tex.lower().replace('ё', 'е')
        diff.append('гранулометрия %s — карта «%s», модель «%s»%s'
                    % ('совпадает' if same else 'расходится', pr, tex.lower(),
                       '' if same else
                       ' (порода против верхнего слоя — величины разной природы)'))
    txt = ('Сверка источников: ' + '; '.join(diff) + '.') if diff \
        else 'Сверка источников: сравнивать нечего — один из источников не ответил.'
    for t in _wrap(s, txt, s.F(2.4), width, limit=3):
        s.d.text((x0, y), t, font=s.F(2.4), fill=(120, 0, 0))
        y += s.mm(3.2)
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
             'два источника: SoilGrids v2.0 (ISRIC, 250 м, %d точек) и Почвенная карта '
             'РСФСР 1:2 500 000 (Фридланд, 1988)%s'
             % (points, ' · ' + place if place else ''),
             font=s.F(2.9, True), fill=(120, 0, 0), anchor='ma')
    s.d.rectangle([s.IN0, s.margin, s.IN1, s.margin + s.title_h], outline='black', width=s.W(0.5))

    TW = sum(s.mm(w) for _, w in COLS)
    x0 = s.IN0 + s.mm(4)
    y = s.margin + s.title_h + s.mm(6)

    # Тип почвы: то, чего в свойствах нет, а агроном спрашивает первым.
    # Два источника разной природы — бумажная карта и модельная
    # классификация; показываем их рядом, строка в строку, и отдельной
    # строкой то, где они расходятся: сглаживать расхождение нельзя.
    if wrb or soil_map:
        y = _sources(s, x0, y, TW, wrb, soil_map, rows, points)

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
    # высота диаграммы — по тому, что осталось до сноски внизу: с таблицей
    # сопоставления источников фиксированные 34 мм наезжали на неё
    gh = max(s.mm(20), min(s.mm(34), int(s.PH - s.margin - s.mm(15) - y)))
    gw = TW - s.mm(34)
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
