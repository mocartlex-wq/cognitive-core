# -*- coding: utf-8 -*-
"""Схема расположения ЗУ и контуров частей — основной лист комплекта.

A4 альбомная, 400 dpi. Состав частей, их назначение и подписи берутся из
конфига участка, поэтому лист собирается для любого набора ЧЗУ, а не только
для четырёх частей 1173.
"""
import math
import numpy as np

from .. import areas
from ..sheet import Sheet, PART_COLOR, PART_HATCH, ZUG, RED, fmt_ha, fmt_int, fmt_m2
from ..geo import Local

LEGEND = {
    '1': 'Контур части ЗУ с/х назначения, покрытый\nкустарниковой и древесной растительностью,\nна котором планируется проведение\nкультуртехнических мероприятий',
    '2': 'Контур части ЗУ, не обработанный, покрытый\nтравяной и кустарниковой растительностью\nбез древесной (залежь)',
    '3': 'Зона с особыми условиями использования\nтерритории (мероприятия не проводятся)',
    '4': 'Защитные лесные насаждения (полезащитные\nлесные полосы) — раскорчёвке не подлежат,\nиз площади мероприятий исключены',
}

def _ring_area(r):
    a = np.asarray(r, float)
    return abs(float(np.sum(a[:, 0] * np.roll(a[:, 1], -1) - np.roll(a[:, 0], -1) * a[:, 1]) / 2))

def _anchor(r):
    from shapely.geometry import Polygon
    try:
        p = Polygon(r).buffer(0).representative_point()
        return p.x, p.y
    except Exception:
        return sum(q[0] for q in r) / len(r), sum(q[1] for q in r) / len(r)

def _compass(s, cx, cy):
    R, r2 = s.mm(7), s.mm(2.6)
    s.d.ellipse([cx - R, cy - R, cx + R, cy + R], outline=(120, 120, 120), width=s.W(0.26))
    for k in range(4):
        a = math.pi / 4 + k * math.pi / 2
        s.d.polygon([(cx + R * .6 * math.cos(a), cy + R * .6 * math.sin(a)),
                     (cx + r2 * .45 * math.cos(a + math.pi / 2), cy + r2 * .45 * math.sin(a + math.pi / 2)),
                     (cx + r2 * .45 * math.cos(a - math.pi / 2), cy + r2 * .45 * math.sin(a - math.pi / 2))],
                    fill=(150, 150, 150))
    s.d.polygon([(cx, cy - R), (cx - r2 * .5, cy), (cx, cy - r2 * .3)], fill=(210, 0, 0))
    s.d.polygon([(cx, cy - R), (cx + r2 * .5, cy), (cx, cy - r2 * .3)], fill=(120, 0, 0))
    s.d.polygon([(cx, cy + R), (cx - r2 * .5, cy), (cx, cy + r2 * .3)], fill=(70, 70, 70))
    s.d.polygon([(cx, cy + R), (cx + r2 * .5, cy), (cx, cy + r2 * .3)], fill=(30, 30, 30))
    for t, (a_, b_) in [('С', (cx, cy - R - s.mm(2.9))), ('Ю', (cx, cy + R + s.mm(2.9))),
                        ('З', (cx - R - s.mm(2.9), cy)), ('В', (cx + R + s.mm(2.9), cy))]:
        s.d.text((a_, b_), t, font=s.F(2.7, True), fill='black', anchor='mm')

def extreme_points(ring):
    """Индексы четырёх крайних вершин: север, юг, запад, восток.

    Одна вершина бывает крайней сразу в двух направлениях (на
    58:17:0130701:29 юго-восточный угол был и самым южным, и самым
    восточным) — тогда подпись координат печаталась дважды. Берём
    следующую по этому направлению, чтобы точек было ровно четыре.
    """
    orders = (sorted(range(len(ring)), key=lambda i: -ring[i][1]),   # север
              sorted(range(len(ring)), key=lambda i: ring[i][1]),    # юг
              sorted(range(len(ring)), key=lambda i: ring[i][0]),    # запад
              sorted(range(len(ring)), key=lambda i: -ring[i][0]))   # восток
    used, idx = set(), []
    for order in orders:
        for i in order:
            if i not in used:
                used.add(i); idx.append(i); break
    return idx


def _place_arrow(s, P, rings, zone, place, layout, used):
    """Стрелка от участка в сторону ближайшего села с названием и расстоянием.

    Направление считается по местным координатам самого пункта, а не по
    азимуту на глаз: сближение меридианов в МСК небольшое, но врать на листе,
    который сверяют с картой, нельзя.

    В КПТ направление записано от ориентира к участку, здесь — наоборот,
    поэтому румб подписан явно словами «от участка».
    """
    from ..sources.places import name_of, rhumb
    E = [q[0] for r in rings for q in r]; N = [q[1] for r in rings for q in r]
    ce, cn = (max(E) + min(E)) / 2, (max(N) + min(N)) / 2
    te, tn = Local(zone).from_wgs([(place['lon'], place['lat'])])[0]
    dx, dy = te - ce, tn - cn
    ln = math.hypot(dx, dy)
    if ln < 1e-6:
        return
    dx, dy = dx / ln, dy / ln
    x0, y0 = P(ce, cn)
    # старт — за границей участка, конец — не доходя рамки карты
    r0 = max(math.hypot(P(q[0], q[1])[0] - x0, P(q[0], q[1])[1] - y0)
             for r in rings for q in r) + s.mm(4)
    px, py = dx, -dy                       # на листе север вверх
    lim = s.mm(9)
    t_edge = min((s.MW - lim - x0) / px if px > 1e-6 else 1e9,
                 (lim - x0) / px if px < -1e-6 else 1e9,
                 (s.MH - lim - y0) / py if py > 1e-6 else 1e9,
                 (lim - y0) / py if py < -1e-6 else 1e9)
    # стрелка не должна уходить под легенду и штамп: на 1173 она целиком
    # оказалась под таблицей условных обозначений и на листе её не было
    step = s.mm(2)
    t_max, t = r0, r0
    while t <= t_edge:
        xx, yy = x0 + px * t, y0 + py * t
        if any(b[0] <= xx <= b[2] and b[1] <= yy <= b[3] for b in s.blocks):
            break
        t_max = t; t += step
    t1 = min(max(t_max, r0 + s.mm(5)), t_edge)
    ray_hidden = t_max - r0 < s.mm(6)      # луч упёрся в легенду или в рамку
    if ray_hidden:                         # места снаружи нет — начинаем от центра
        r0 = max(s.mm(2), t1 - s.mm(22))
    ax, ay = x0 + px * r0, y0 + py * r0
    bx, by = x0 + px * t1, y0 + py * t1
    cap = '%s — %s км' % (name_of(place), ('%.1f' % place['км']).replace('.', ','))
    sub = 'от участка на %s' % rhumb(place['азимут'])
    f_cap, f_sub = s.F(2.6, True), s.F(2.2)
    w = max(s.md.textlength(cap, font=f_cap), s.md.textlength(sub, font=f_sub)) / 2 + s.mm(1.6)
    h = s.mm(5.6)
    box = lambda cx, cy: (cx - w, cy - h / 2, cx + w, cy + h / 2)

    tx = ty = None
    if layout.get('place'):
        tx, ty = s.mm(layout['place'][0]) - s.MX0, s.mm(layout['place'][1]) - s.MY0
    else:
        # подпись ищет свободное место вдоль стрелки: у компаса, легенды и
        # штампа свои зоны, и на 58:17:0130701:29 подпись садилась прямо на компас
        for t in (t1 + s.mm(7), t1 + s.mm(3), t1 - s.mm(6), t1 - s.mm(14), t1 - s.mm(22)):
            for side in (0, 1, -1, 2, -2):
                cx = x0 + px * t - py * side * s.mm(6)
                cy = y0 + py * t + px * side * s.mm(6)
                cx = min(max(cx, w + s.mm(2)), s.MW - w - s.mm(2))
                cy = min(max(cy, s.mm(5)), s.MH - s.mm(6))
                if s.free(*box(cx, cy)):
                    tx, ty = cx, cy
                    break
            if tx is not None:
                break
        if tx is None:
            # вдоль стрелки места нет (на 1173 она упирается в легенду) —
            # ищем ближайшее свободное место на всём поле карты
            best = None
            gx = int(w + s.mm(2)); gy = s.mm(5)
            while gx < s.MW - w - s.mm(2):
                gy = s.mm(5)
                while gy < s.MH - s.mm(6):
                    if s.free(*box(gx, gy)):
                        d = math.hypot(gx - bx, gy - by)
                        if best is None or d < best[0]:
                            best = (d, gx, gy)
                    gy += s.mm(5)
                gx += s.mm(5)
            if best:
                tx, ty = best[1], best[2]
            else:                          # свободного места нет вовсе
                tx = min(max(bx + px * s.mm(7), w + s.mm(2)), s.MW - w - s.mm(2))
                ty = min(max(by + py * s.mm(4), s.mm(5)), s.MH - s.mm(6))
    used['place'] = [round((tx + s.MX0) / s.MM, 2), round((ty + s.MY0) / s.MM, 2)]
    # луч рисуется после того, как подпись встала: наконечник не должен
    # въезжать в текст, поэтому укорачиваем его до края рамки подписи
    if not ray_hidden:
        lb = box(tx, ty)
        t_stop = t1
        while t_stop > r0 + s.mm(5):
            xx, yy = x0 + px * t_stop, y0 + py * t_stop
            if not (lb[0] - s.mm(2) <= xx <= lb[2] + s.mm(2)
                    and lb[1] - s.mm(2) <= yy <= lb[3] + s.mm(2)):
                break
            t_stop -= s.mm(1)
        bx, by = x0 + px * t_stop, y0 + py * t_stop
        s.md.line([(ax, ay), (bx, by)], fill=(60, 60, 60), width=s.W(0.35))
        a = math.atan2(py, px); h = s.mm(2.6)
        s.md.polygon([(bx, by),
                      (bx - h * math.cos(a - 0.38), by - h * math.sin(a - 0.38)),
                      (bx - h * math.cos(a + 0.38), by - h * math.sin(a + 0.38))],
                     fill=(60, 60, 60))
    # выноска нужна, только если подпись встала далеко от наконечника
    far = math.hypot(tx - bx, ty - by)
    if far > s.mm(8):
        s.md.line([(bx, by), (tx, ty)], fill=(150, 150, 150), width=s.W(0.2))
    # стрелка на листе ровно одна: длинный луч, когда он виден, иначе
    # короткая у подписи. Две стрелки в одну сторону читаются как ошибка
    for sign in ((1, -1) if ray_hidden else ()):
        off = w + s.mm(2)
        if sign > 0:                       # стрелка за подписью, по ходу направления
            ax0, ay0 = tx + px * off, ty + py * off
            ax1, ay1 = ax0 + px * s.mm(9), ay0 + py * s.mm(9)
        else:                              # или перед ней, но смотрит туда же
            ax1, ay1 = tx - px * off, ty - py * off
            ax0, ay0 = ax1 - px * s.mm(9), ay1 - py * s.mm(9)
        lo = (min(ax0, ax1), min(ay0, ay1), max(ax0, ax1), max(ay0, ay1))
        if lo[0] > s.mm(1) and lo[1] > s.mm(1) and lo[2] < s.MW - s.mm(1) \
                and lo[3] < s.MH - s.mm(1) \
                and not any(b[0] <= lo[2] and lo[0] <= b[2] and b[1] <= lo[3] and lo[1] <= b[3]
                            for b in s.blocks):
            s.md.line([(ax0, ay0), (ax1, ay1)], fill=(60, 60, 60), width=s.W(0.35))
            aa = math.atan2(ay1 - ay0, ax1 - ax0); hh = s.mm(2.4)
            s.md.polygon([(ax1, ay1),
                          (ax1 - hh * math.cos(aa - 0.38), ay1 - hh * math.sin(aa - 0.38)),
                          (ax1 - hh * math.cos(aa + 0.38), ay1 - hh * math.sin(aa + 0.38))],
                         fill=(60, 60, 60))
            break
    for t, f, dyy in ((cap, f_cap, -s.mm(1.5)), (sub, f_sub, s.mm(1.9))):
        s.md.text((tx, ty + dyy), t, font=f, fill=(30, 30, 30), anchor='mm',
                  stroke_width=s.W(0.6), stroke_fill='white')
    s.reserve(box(tx, ty))

def build(path, kn, rings, parts, egrn_ha, zone, neighbors=None, title=None, layout=None,
          place=None):
    """parts — {ключ: {'outer','inner','areaHa','название'}} в порядке отрисовки.

    layout — сохранённые правообладателем позиции подвижных подписей в
    миллиметрах от левого верхнего угла листа: {'kn': [x, y], 'coord0': …,
    'legend': …, 'stamp': …}. Чего нет — ставится автоматически, как раньше.
    Итоговые позиции возвращаются третьим значением, чтобы редактор
    открывался ровно там, где сейчас стоят подписи.
    """
    layout = layout or {}
    used = {}
    keys = sorted(parts)
    s = Sheet(297, 210)
    # Поле карты подстраивается под размер участка: постоянные 260 м вокруг
    # границы на участке 2 га оставляли контур пятном в углу листа, а вокруг —
    # пустоту. Отступ считаем от габарита, но не мельче 60 м: смежники и
    # подписи координат должны помещаться.
    E = [p[0] for r in rings for p in r]; N = [p[1] for r in rings for p in r]
    span = max(max(E) - min(E), max(N) - min(N))
    P = s.map_field(rings, pad_m=max(60.0, min(260.0, span * 0.35)), shift_mm=28)
    # Таблица условных обозначений занимала 103 × 68 мм — четверть листа при
    # одной части. Высота строки считается по числу строк описания, а не
    # берётся с запасом: та же таблица выходит вдвое меньше по площади.
    TW = s.mm(74)
    # описания переносятся по фактической ширине колонки: в узкой таблице
    # заготовленные переносы вылезали в колонку «Площадь»
    DESC_W = TW - s.mm(19 + 16 + 3)
    def _wrap(t, font):
        out, cur = [], ''
        for w in t.replace('\n', ' ').split():
            probe = (cur + ' ' + w).strip()
            if cur and s.d.textlength(probe, font=font) > DESC_W:
                out.append(cur); cur = w
            else:
                cur = probe
        if cur:
            out.append(cur)
        return '\n'.join(out)
    LEG_TEXT = [_wrap(LEGEND.get(k, parts[k].get('название', '')), s.F(2.0)) for k in keys] + \
               [_wrap('Контур и кадастровый номер земельного участка '
                      'согласно сведений ЕГРН', s.F(2.0))]
    ROW_H = [max(8.6, 2.7 * (t.count('\n') + 1) + 3.0) for t in LEG_TEXT]
    TBL = s.mm(8.5 + sum(ROW_H))
    SW, SH = s.mm(62), s.mm(30)
    s.block(s.MW - TW, s.MH - TBL, s.MW, s.MH)
    s.block(0, s.MH - SH, SW, s.MH)
    # угол компаса: со строкой о населённом пункте он шире и выше
    s.block(0, 0, s.mm(25), s.mm(27))

    # смежные участки — красная сеть; подписи только тем, что крупнее 0,9 га
    f_adj = s.F(2.05)
    for o in (neighbors or []):
        if o.get('kn') == kn:
            continue
        for r in o['rings']:
            s.md.line([P(*p) for p in r] + [P(*r[0])], fill=RED, width=s.W(0.22))
        r = max(o['rings'], key=len)
        if _ring_area(r) > 9000:
            cx = sum(p[0] for p in r) / len(r); cy = sum(p[1] for p in r) / len(r)
            s.label(*P(cx, cy), o['kn'], f_adj, fill=RED, halo=None,
                    offsets=((0, 0), (0, -3.4), (0, 3.4), (0, -6.8), (0, 6.8)))

    for k in keys:                                   # штриховка
        if k in PART_HATCH:
            s.hatch(parts[k]['outer'], parts[k]['inner'], PART_COLOR[k], PART_HATCH[k])
    for k in keys:                                   # контуры частей, кроме ЗОУИТ
        if k == '3':
            continue
        s.polyline(parts[k]['outer'], PART_COLOR[k], 0.40)
        s.polyline(parts[k]['inner'], PART_COLOR[k], 0.32)
    # граница ЗУ под ЗОУИТ: на западной стороне они совпадают, и магента
    # поверх зелёной читается как «розовая жила в зелёной кайме»
    s.polyline(rings[:1], ZUG, 0.70)
    s.polyline(rings[1:], ZUG, 0.56)
    if '3' in parts:
        s.polyline(parts['3']['outer'], PART_COLOR['3'], 0.38)
        s.polyline(parts['3']['inner'], PART_COLOR['3'], 0.32)

    # кадастровый номер: рисуется до подписей частей и сразу занимает своё место
    E = [p[0] for r in rings for p in r]; N = [p[1] for r in rings for p in r]
    x, y = P(sum(E) / len(E), sum(N) / len(N))
    if layout.get('kn'):                       # позиция от правообладателя
        x = s.mm(layout['kn'][0]) - s.MX0; y = s.mm(layout['kn'][1]) - s.MY0
    used['kn'] = [round((x + s.MX0) / s.MM, 2), round((y + s.MY0) / s.MM, 2)]
    f_kn = s.F(2.9)
    bb = s.md.textbbox((x, y), kn, font=f_kn, anchor='mm')
    box = [bb[0] - s.mm(1.4), bb[1] - s.mm(1.0), bb[2] + s.mm(1.4), bb[3] + s.mm(1.0)]
    s.md.rectangle(box, fill='white', outline=ZUG, width=s.W(0.5))
    s.md.text((x, y), kn, font=f_kn, fill='black', anchor='mm')
    s.reserve(box)

    f_ch = s.F(2.7)
    for k in keys:
        for r in sorted(parts[k]['outer'], key=lambda r: -_ring_area(r))[:4]:
            if _ring_area(r) < 3000:
                continue
            s.label(*P(*_anchor(r)), 'ЧЗУ/%s' % k, f_ch)

    # ближайший населённый пункт: стрелка и подпись ставятся до координат,
    # чтобы подписи точек обходили их, а не наоборот
    if place:
        _place_arrow(s, P, rings, zone, place, layout, used)

    # географические координаты характерных точек
    loc = Local(zone); f_geo = s.F(2.5); out = rings[0]
    idx = extreme_points(out)
    for j, (i, (ox, oy)) in enumerate(zip(idx, [(0, -1), (0, 1), (-1, 0), (0.4, -1)])):
        e, n = out[i]; lon, lat = loc.to_wgs([(e, n)])[0]
        x, y = P(e, n)
        tx = ty = None
        saved = layout.get('coord%d' % j)
        if saved:
            tx, ty = s.mm(saved[0]) - s.MX0, s.mm(saved[1]) - s.MY0
        for cx_, cy_ in ([] if tx is not None else
                         [(ox, oy), (1.1, .6), (1.1, -.6), (-1.1, .6),
                          (-1.1, -.6), (0, -1.4), (0, 1.4)]):
            a, b = x + cx_ * s.mm(13), y + cy_ * s.mm(8)
            if s.free(a - s.mm(13), b - s.mm(4.5), a + s.mm(13), b + s.mm(4.5)):
                tx, ty = a, b; break
        if tx is None:
            tx = min(max(x + ox * s.mm(13), s.mm(12)), s.MW - s.mm(12))
            ty = min(max(y + oy * s.mm(8), s.mm(5)), s.MH - s.mm(5))
        used['coord%d' % j] = [round((tx + s.MX0) / s.MM, 2), round((ty + s.MY0) / s.MM, 2)]
        s.md.line([(x, y), (tx, ty)], fill='black', width=s.W(0.24))
        s.md.text((tx, ty - s.mm(1.9)), '%.7f' % lat, font=f_geo, fill='black', anchor='mm',
                  stroke_width=s.W(0.6), stroke_fill='white')
        s.md.line([(tx - s.mm(9), ty), (tx + s.mm(9), ty)], fill='white', width=s.W(0.8))
        s.md.line([(tx - s.mm(9), ty), (tx + s.mm(9), ty)], fill='black', width=s.W(0.26))
        s.md.text((tx, ty + s.mm(1.9)), '%.7f' % lon, font=f_geo, fill='black', anchor='mm',
                  stroke_width=s.W(0.6), stroke_fill='white')
        s.md.ellipse([x - s.mm(.5), y - s.mm(.5), x + s.mm(.5), y + s.mm(.5)], fill='black')

    s.paste_map()
    s.frame(outer_mm=4)
    s.title(title or [
        'Схема расположения земельного участка сельскохозяйственного назначения '
        'с кадастровым номером  %s  и контуров частей' % kn,
        'земельного участка, покрытых древесной и кустарниковой растительностью, '
        'на которых планируется проведение культуртехнических мероприятий'], size=3.0)
    _compass(s, s.MX0 + s.mm(11), s.MY0 + s.mm(13))
    # таблица условных обозначений
    tx0 = s.IN1 - s.mm(2) - TW; ty0 = s.PH - s.margin - s.mm(2) - TBL
    if layout.get('legend'):
        tx0, ty0 = s.mm(layout['legend'][0]), s.mm(layout['legend'][1])
    used['legend'] = [round(tx0 / s.MM, 2), round(ty0 / s.MM, 2)]
    s.d.rectangle([tx0, ty0, tx0 + TW, ty0 + TBL], fill='white', outline='black', width=s.W(0.55))
    s.d.text((tx0 + TW / 2, ty0 + s.mm(0.8)), 'Условные обозначения:', font=s.F(2.6),
             fill='black', anchor='ma')
    hdr = ty0 + s.mm(4.6); c1 = tx0 + s.mm(19); c2 = tx0 + TW - s.mm(16)
    s.d.line([tx0, hdr, tx0 + TW, hdr], fill='black', width=s.W(0.4))
    for xx, t in ((tx0 + s.mm(9.5), 'Графика'), ((c1 + c2) / 2, 'Описание'),
                  ((c2 + tx0 + TW) / 2, 'Площадь')):
        s.d.text((xx, hdr + s.mm(0.8)), t, font=s.F(2.3), fill='black', anchor='ma')
    s.d.line([tx0, hdr + s.mm(3.9), tx0 + TW, hdr + s.mm(3.9)], fill='black', width=s.W(0.4))
    for cx_ in (c1, c2):
        s.d.line([cx_, hdr, cx_, ty0 + TBL], fill='black', width=s.W(0.3))
    f_c = s.F(2.0)
    # площадь берём сведённую с ЕГРН: на :29 по координатам выходило
    # 21 911,61 м², и в легенде рядом стояли 21 912 и 21 911 у одного контура
    order = sorted(keys, key=lambda q: -parts[q]['areaHa'])
    txt = dict(zip(list(keys) + [None], LEG_TEXT))
    rows = [(k, txt[k], areas.m2(parts[k])) for k in order]
    rows.append((None, txt[None], int(round(egrn_ha * 10000))))
    y = hdr + s.mm(3.9)
    heights = [s.mm(h) for h in ROW_H]
    for (k, desc, m2), hgt in zip(rows, heights):
        gy = y + hgt / 2
        gx0, gx1 = tx0 + s.mm(2.4), c1 - s.mm(2.4)
        if k is None:
            s.d.rectangle([gx0, gy - s.mm(2.6), gx1, gy + s.mm(2.6)], fill='white',
                          outline=ZUG, width=s.W(0.6))
            # номер подгоняется по ширине образца: 58:28:0500401:74 не влезал
            fs = 2.2
            while fs > 1.4 and s.d.textlength(kn, font=s.F(fs)) > (gx1 - gx0) - s.mm(1.4):
                fs -= 0.1
            s.d.text(((gx0 + gx1) / 2, gy), kn, font=s.F(fs), fill='black', anchor='mm')
        else:
            col = PART_COLOR.get(k, (0, 0, 0))
            s.d.rectangle([gx0, gy - s.mm(2.6), gx1, gy + s.mm(2.6)], outline=col, width=s.W(0.55))
            if k in PART_HATCH:
                # штриховка образца рисуется в отдельный кусок и вставляется:
                # иначе диагонали вылезают за рамку образца
                from PIL import Image as _I, ImageDraw as _D
                w_, h_ = int(gx1 - gx0) - 2, s.mm(5.2) - 2
                sw = _I.new('RGB', (w_, h_), 'white'); sd = _D.Draw(sw)
                st = s.W(2.2)
                for c in range(-h_, w_ + h_, st):
                    if PART_HATCH[k] > 0:
                        sd.line([(c, h_), (c + h_, 0)], fill=col, width=s.W(0.22))
                    elif PART_HATCH[k] < 0:
                        sd.line([(c, 0), (c + h_, h_)], fill=col, width=s.W(0.22))
                    else:
                        sd.line([(c, 0), (c, h_)], fill=col, width=s.W(0.22))
                s.page.paste(sw, (int(gx0) + 1, int(gy - s.mm(2.6)) + 1))
                s.d.rectangle([gx0, gy - s.mm(2.6), gx1, gy + s.mm(2.6)], outline=col, width=s.W(0.55))
            s.d.text(((gx0 + gx1) / 2, gy), 'ЧЗУ/%s' % k, font=s.F(2.3), fill='black', anchor='mm',
                     stroke_width=s.W(0.5), stroke_fill='white')
        s.d.multiline_text((c1 + s.mm(1.6), gy), desc, font=f_c, fill='black', anchor='lm',
                           spacing=s.mm(0.7))
        s.d.multiline_text(((c2 + tx0 + TW) / 2, gy),
                           '(%s м²)\n(%s га)' % (fmt_int(m2), fmt_ha(m2 / 10000.0)),
                           font=f_c, fill='black', anchor='mm', align='center', spacing=s.mm(0.9))
        y += hgt
        if y < ty0 + TBL - 1:
            s.d.line([tx0, y, tx0 + TW, y], fill='black', width=s.W(0.3))

    # штамп «Утверждаю»
    sx0, sy0 = s.IN0 + s.mm(2), s.PH - s.margin - s.mm(2) - SH
    if layout.get('stamp'):
        sx0, sy0 = s.mm(layout['stamp'][0]), s.mm(layout['stamp'][1])
    used['stamp'] = [round(sx0 / s.MM, 2), round(sy0 / s.MM, 2)]
    s.d.rectangle([sx0, sy0, sx0 + SW, sy0 + SH], fill='white', outline='black', width=s.W(0.55))
    s.d.text((sx0 + SW / 2, sy0 + s.mm(2.0)), 'Утверждаю:', font=s.F(2.9), fill='black', anchor='ma')
    s.d.text((sx0 + SW / 2, sy0 + s.mm(6.4)), '_________________________', font=s.F(2.4),
             fill='black', anchor='ma')
    s.d.text((sx0 + SW / 2, sy0 + s.mm(9.2)), '(должность, фамилия, инициалы)', font=s.F(2.1),
             fill=(90, 90, 90), anchor='ma')
    s.d.text((sx0 + s.mm(6), sy0 + s.mm(14.2)), '__________________', font=s.F(2.4), fill='black')
    s.d.text((sx0 + s.mm(19), sy0 + s.mm(17.4)), '(подпись)', font=s.F(2.1), fill='black', anchor='ma')
    s.d.text((sx0 + s.mm(38), sy0 + s.mm(15.4)), '/______________/', font=s.F(2.1), fill='black')
    s.d.text((sx0 + s.mm(5), sy0 + s.mm(23)), '«____» ______________ 20____ г.', font=s.F(2.1), fill='black')
    s.save(path)
    return path, s.denominator(), used
