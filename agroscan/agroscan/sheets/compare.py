# -*- coding: utf-8 -*-
"""Лист сверки: наш расчёт против работы специалиста.

Нужен, когда по участку уже есть чужая схема. Задача листа — не доказать
свою правоту, а свести расхождения построчно и назвать причину каждого:
с таким листом можно идти к специалисту и обсуждать по существу, а не
сравнивать два набора цифр.
"""
from ..sheet import Sheet, fmt_ha

def build(path, kn, rows, notes=(), zouit_variants=(), egrn_ha=None, ref_name='расчёт специалиста'):
    """rows — [(часть, эталон_га|None, наш_га|None, причина)]."""
    s = Sheet(297, 210, dpi=400, ss=2, margin_mm=8, title_mm=14)
    s.frame(outer_mm=5)
    s.d.text((s.PW / 2, s.margin + s.mm(2.2)),
             'Сверка расчёта частей земельного участка %s' % kn,
             font=s.F(4.0, True), fill='black', anchor='ma')
    s.d.text((s.PW / 2, s.margin + s.mm(7.8)),
             'слева — %s, справа — расчёт по данным ДЗЗ · причина расхождения по каждой строке'
             % ref_name, font=s.F(2.8, True), fill=(120, 0, 0), anchor='ma')
    s.d.rectangle([s.IN0, s.margin, s.IN1, s.margin + s.title_h], outline='black', width=s.W(0.5))

    x0 = s.IN0 + s.mm(5)
    W = s.IN1 - s.mm(5) - x0
    cols = [x0, x0 + s.mm(22), x0 + s.mm(44), x0 + s.mm(66), x0 + s.mm(88), x0 + W]
    y = s.margin + s.title_h + s.mm(7)
    s.d.line([x0, y - s.mm(1), x0 + W, y - s.mm(1)], fill='black', width=s.W(0.55))
    for i, t in enumerate(('Часть', 'Специалист,\nга', 'Наш расчёт,\nга', 'Расхождение,\nга',
                           'Причина и что нужно, чтобы сойтись')):
        anchor = 'ma' if i < 4 else 'la'
        px = (cols[i] + cols[i + 1]) / 2 if i < 4 else cols[i] + s.mm(2)
        s.d.multiline_text((px, y), t, font=s.F(2.6, True), fill='black', anchor=anchor,
                           align='center' if i < 4 else 'left', spacing=s.mm(0.7))
    y += s.mm(8)
    s.d.line([x0, y - s.mm(1.2), x0 + W, y - s.mm(1.2)], fill='black', width=s.W(0.42))
    f = s.F(2.55)
    for part, ref, own, why in rows:
        h = s.mm(4.6) * max(1, why.count('\n') + 1) + s.mm(2)
        s.d.text((cols[0] + s.mm(2), y + s.mm(1)), part, font=s.F(2.7, True), fill='black')
        for i, v in enumerate((ref, own)):
            s.d.text(((cols[i + 1] + cols[i + 2]) / 2, y + s.mm(1)),
                     fmt_ha(v) if v is not None else '—', font=f, fill='black', anchor='ma')
        if ref is not None and own is not None:
            d = own - ref
            col = (0, 110, 0) if abs(d) < 0.5 else (170, 90, 0) if abs(d) < 3 else (185, 0, 0)
            s.d.text(((cols[3] + cols[4]) / 2, y + s.mm(1)), ('%+.2f' % d).replace('.', ','),
                     font=s.F(2.55, abs(d) >= 3), fill=col, anchor='ma')
        else:
            s.d.text(((cols[3] + cols[4]) / 2, y + s.mm(1)), '—', font=f, fill=(120, 120, 120),
                     anchor='ma')
        s.d.multiline_text((cols[4] + s.mm(2), y + s.mm(1)), why, font=f, fill=(50, 50, 50),
                           spacing=s.mm(1.1))
        y += h
        s.d.line([x0, y - s.mm(1), x0 + W, y - s.mm(1)], fill=(190, 190, 190), width=s.W(0.25))
    s.d.line([x0, y - s.mm(1), x0 + W, y - s.mm(1)], fill='black', width=s.W(0.55))
    y += s.mm(4)

    if zouit_variants:
        s.d.text((x0, y), 'Из чего складывается ЧЗУ/3: перебор составов зон из КПТ',
                 font=s.F(3.2, True), fill='black')
        y += s.mm(6)
        s.d.multiline_text((x0, y),
            'В КПТ на участок приходится 49 зон с кодом «зона с особыми условиями использования\n'
            'территории». Пять из них — приаэродромная территория и её подзоны — накрывают участок\n'
            'целиком и ограничивают высотное строительство, а не сельхозработы: они исключены сразу.\n'
            'Остальные восемь дают 41,44 га. Ниже — какие составы к каким площадям приводят.',
            font=s.F(2.5), fill=(60, 60, 60), spacing=s.mm(1.2))
        y += s.mm(15)
        for lab, val, note, col in zouit_variants:
            s.d.text((x0 + s.mm(2), y), lab, font=s.F(2.6, col is not None), fill=col or (0, 0, 0))
            s.d.text((x0 + s.mm(96), y), fmt_ha(val) + ' га', font=s.F(2.6, col is not None),
                     fill=col or (0, 0, 0), anchor='ra')
            s.d.text((x0 + s.mm(100), y), note, font=s.F(2.45), fill=(90, 90, 90))
            y += s.mm(4.8)
        y += s.mm(3)

    # примечания не должны налезать на рамку: если места мало, поджимаем шаг
    bottom = s.PH - s.margin - s.mm(4)
    need = sum(s.mm(4.0) * (t.count('\n') + 1) + s.mm(2) for t in notes)
    k = min(1.0, (bottom - y) / max(need, 1)) if need else 1.0
    for t in notes:
        s.d.multiline_text((x0, y), t, font=s.F(2.5), fill=(60, 60, 60), spacing=s.mm(1.15 * k))
        y += s.mm(4.0 * k) * (t.count('\n') + 1) + s.mm(2 * k)
    s.save(path)
    return path
