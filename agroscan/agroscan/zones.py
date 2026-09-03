# -*- coding: utf-8 -*-
"""Разведение частей земельного участка: обрезка, приоритет, добор, чистка.

Порядок операций выстрадан на 1173 и менять его местами нельзя:
  1. merge_touching — сомкнуть ареалы одного вида, между которыми щель от
     морфологического размыкания (иначе один массив выходит двумя частями);
  2. clip + resolve — обрезать по границе ЕГРН и развести зоны по приоритету,
     чтобы они не накладывались;
  3. fill_remainder — раздать непокрытый остаток по существу (что там на
     растре), а не по близости;
  4. despeckle — убрать нити уже 3 м: вынести такую часть в натуру нельзя,
     а на схеме она рисуется второй линией и читается как наложение зон.
"""
import numpy as np
from scipy import ndimage
from shapely.geometry import Polygon, MultiPolygon
from shapely.ops import unary_union

from .rings import disk

# ── растровый уровень ───────────────────────────────────────────────────
def merge_touching(m, allow, mpp, gap=5.0):
    """Сомкнуть ареалы, между которыми щель не шире gap метров.

    allow возвращает маску в её законную область: перемычка не должна
    залезть в ЗОУИТ или в лесополосу — там разделение настоящее.
    """
    r = max(1, int(round(gap / 2 / mpp)))
    p = np.pad(m, r, constant_values=True)   # иначе эрозия съедает край кадра
    return ndimage.binary_closing(p, disk(r))[r:-r, r:-r] & allow

def smooth(m, allow, mpp, close_m=8.0, open_m=4.0):
    """Замкнуть просветы внутри массива и убрать нитевидные выступы."""
    m = ndimage.binary_closing(m, disk(max(1, int(close_m / mpp))))
    m = ndimage.binary_opening(m, disk(max(1, int(open_m / mpp)))) & allow
    return m

# ── векторный уровень ───────────────────────────────────────────────────
def to_poly(outer, inner):
    g = unary_union([Polygon(r).buffer(0) for r in outer if len(r) >= 4])
    h = [Polygon(r).buffer(0) for r in inner if len(r) >= 4]
    if h:
        g = g.difference(unary_union(h))
    return g.buffer(0)

def parcel_poly(rings, min_area=10.0):
    """Полигон участка из колец КПТ: крупнейшее кольцо внешнее, прочие — вырезы.
    Вырожденные кольца (нулевой площади) в выгрузках встречаются — отбрасываем."""
    a = lambda r: abs(float(np.sum(np.asarray(r)[:, 0] * np.roll(np.asarray(r)[:, 1], -1)
                                   - np.roll(np.asarray(r)[:, 0], -1) * np.asarray(r)[:, 1]) / 2))
    rs = sorted([np.asarray(r, float) for r in rings if a(r) > min_area], key=a, reverse=True)
    return Polygon(rs[0], [r for r in rs[1:]]).buffer(0)

def resolve(zones, parcel, order):
    """Обрезать по границе участка и развести зоны по приоритету.

    zones — {ключ: полигон}, order — ключи от старшей зоны к младшей.
    Старшая забирает пересечение себе: лесополоса внутри ЗОУИТ уходит в ЗОУИТ,
    потому что мероприятия там не проводятся в любом случае.
    """
    done = []
    for k in order:
        g = zones[k].intersection(parcel)
        for d in done:
            g = g.difference(zones[d])
        zones[k] = g.buffer(0); done.append(k)
    return zones

def _cells(p, grid):
    """Индексы ячеек сетки внутри полигона (для решения «что там на растре»)."""
    from shapely import contains_xy
    M = grid.M; b = p.bounds
    c0 = max(0, int((b[0] - M['e0']) / (M['e1'] - M['e0']) * M['W']))
    c1 = min(M['W'], int((b[2] - M['e0']) / (M['e1'] - M['e0']) * M['W']) + 1)
    r0 = max(0, int((M['n1'] - b[3]) / (M['n1'] - M['n0']) * M['H']))
    r1 = min(M['H'], int((M['n1'] - b[1]) / (M['n1'] - M['n0']) * M['H']) + 1)
    if c1 <= c0 or r1 <= r0:
        return None, None
    xs = M['e0'] + (np.arange(c0, c1) + 0.5) / M['W'] * (M['e1'] - M['e0'])
    ys = M['n1'] - (np.arange(r0, r1) + 0.5) / M['H'] * (M['n1'] - M['n0'])
    XX, YY = np.meshgrid(xs, ys)
    return contains_xy(p, XX, YY), (slice(r0, r1), slice(c0, c1))

def fill_remainder(zones, parcel, grid, decide, order, thin=3.0, min_piece=0.2):
    """Раздать непокрытую площадь участка так, чтобы части покрывали его без зазоров.

    decide(sel, sub) -> ключ зоны: решает по растру, чем кусок является.
    Нити тоньше thin метров классов почти не содержат — их отдаём соседу
    с самой длинной общей границей.
    """
    rest = parcel.difference(unary_union(list(zones.values()))).buffer(0)
    pieces = [p for p in (rest.geoms if isinstance(rest, MultiPolygon) else [rest])
              if p.area > min_piece]
    add = {k: [] for k in zones}
    for p in pieces:
        if 4 * p.area / max(p.length, 1e-9) < thin:
            cand = {k: p.buffer(0.6).intersection(g).area for k, g in zones.items()}
            k = max(cand, key=cand.get) if max(cand.values()) > 0 else order[-1]
        else:
            sel, sub = _cells(p, grid)
            k = decide(sel, sub) if sel is not None and sel.any() else order[-1]
        add[k].append(p)
    # напуск 0,5 м приклеивает микроостатки к своей зоне, иначе они остаются
    # самостоятельными точечными контурами и мусорят схему
    for k in zones:
        if add[k]:
            zones[k] = unary_union([zones[k]] + [p.buffer(0.5) for p in add[k]]).buffer(0)
    moved = {k: sum(p.area for p in add[k]) / 1e4 for k in add}
    return resolve(zones, parcel, order), rest.area / 1e4, moved

def despeckle(zones, parcel, order, thin=3.0, rounds=3):
    """Убрать части уже thin метров, отдав их соседу с длиннейшей общей границей."""
    moved = 0.0
    for _ in range(rounds):
        step = 0.0
        for k in list(zones):
            keep, drop = [], []
            g = zones[k]
            for p in (g.geoms if isinstance(g, MultiPolygon) else [g]):
                (drop if p.buffer(-thin / 2).is_empty else keep).append(p)
            if not drop:
                continue
            zones[k] = unary_union(keep).buffer(0) if keep else Polygon()
            for p in drop:
                cand = {j: p.buffer(1.0).intersection(zones[j]).area for j in zones if j != k}
                j = max(cand, key=cand.get)
                if cand[j] <= 0:
                    j = max((q for q in zones if q != k), key=lambda q: zones[q].area)
                zones[j] = unary_union([zones[j], p.buffer(0.4)]).buffer(0)
                step += p.area
        moved += step
        if step == 0:
            break
    return resolve(zones, parcel, order), moved / 1e4

def clean_ring(ring, eps=1e-6):
    """Убрать вершины, которых в геометрии нет: совпадающие и лежащие на прямой.

    На :29 каталог координат выходил на 15 точек вместо 12 по КПТ: две
    вершины совпадали (строка с длиной 0,00 м и углом 180°00\'00"), ещё две
    лежали на прямой — след buffer(0.5) в fill_remainder и вершины от
    пересечения полигонов. В межевом документе таких точек быть не должно.

    Снимаются только вершины, удаление которых не меняет геометрию (допуск
    микрон). Обобщать контур здесь нельзя: Дуглас–Пёкер с допуском 5 мм
    убирал вдвое больше точек, но резал соседние части по-разному, и между
    ними появлялись наложения 0,15 м² и зазоры 1,5 м² — при нулевом допуске
    проверок это брак. Генерализация — отдельная и осознанная операция
    (export.simplify по настройке export.simplify_m).
    """
    a = [[float(x), float(y)] for x, y in ring]
    out = []
    for q in a:
        if not out or abs(q[0] - out[-1][0]) > eps or abs(q[1] - out[-1][1]) > eps:
            out.append(q)
    while len(out) > 3 and abs(out[0][0] - out[-1][0]) <= eps and abs(out[0][1] - out[-1][1]) <= eps:
        out.pop()
    changed = True
    while changed and len(out) > 3:
        changed = False
        i = 0
        while i < len(out) and len(out) > 3:
            p, c, q = out[i - 1], out[i], out[(i + 1) % len(out)]
            dx, dy = q[0] - p[0], q[1] - p[1]
            ln = (dx * dx + dy * dy) ** 0.5
            if ln > 1e-12:
                d = abs(dx * (p[1] - c[1]) - (p[0] - c[0]) * dy) / ln
                t = ((c[0] - p[0]) * dx + (c[1] - p[1]) * dy) / (ln * ln)
            else:
                d, t = ((c[0] - p[0]) ** 2 + (c[1] - p[1]) ** 2) ** 0.5, 0.0
            # только точка НА отрезке: вершина острого выступа тоже даёт
            # нулевое отклонение, но выкидывать её нельзя
            if d <= eps and -1e-9 <= t <= 1 + 1e-9:
                out.pop(i); changed = True
                continue
            i += 1
    return out

def extra_points(rings, eps=1e-6):
    """Сколько вершин в кольцах лишние (для проверки qa)."""
    return sum(len(r) - len(clean_ring(r, eps)) for r in rings)

def to_rings(g, minha=0.0002, clean=True):
    """Полигон → (внешние кольца, вырезы, площадь га) для записи и отрисовки."""
    if g.is_empty:
        return [], [], 0.0
    ps = [p for p in (g.geoms if isinstance(g, MultiPolygon) else [g]) if p.area / 1e4 >= minha]
    keep = (lambda r: clean_ring(r)) if clean else (lambda r: [[float(x), float(y)] for x, y in r])
    outer = [keep(p.exterior.coords[:-1]) for p in ps]
    inner = [keep(r.coords[:-1]) for p in ps for r in p.interiors
             if Polygon(r).area / 1e4 >= minha]
    # площадь считаем по тем же кольцам, что уйдут в каталог координат:
    # иначе ведомость расходится с координатами, которыми она задана
    ha = lambda rs: sum(Polygon(r).area for r in rs if len(r) >= 3) / 1e4
    return outer, inner, ha(outer) - ha(inner)

def drop_small_zouit(zin, mpp, min_ha=0.25):
    """Отсечь язычки ЗОУИТ мельче min_ha: границей ЗУ от охранной зоны
    отрезаются полоски в 3-5 м, которые на схеме читаются как ошибка."""
    lb, n = ndimage.label(zin)
    if not n:
        return zin
    sz = ndimage.sum(zin, lb, range(1, n + 1)) * (mpp * mpp / 10000)
    return np.isin(lb, [i + 1 for i, s in enumerate(sz) if s >= min_ha])
