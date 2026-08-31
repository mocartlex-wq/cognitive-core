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

def to_rings(g, minha=0.0002):
    """Полигон → (внешние кольца, вырезы, площадь га) для записи и отрисовки."""
    if g.is_empty:
        return [], [], 0.0
    ps = [p for p in (g.geoms if isinstance(g, MultiPolygon) else [g]) if p.area / 1e4 >= minha]
    outer = [[list(map(float, c)) for c in p.exterior.coords[:-1]] for p in ps]
    inner = [[list(map(float, c)) for c in r.coords[:-1]] for p in ps for r in p.interiors
             if Polygon(r).area / 1e4 >= minha]
    return outer, inner, sum(p.area for p in ps) / 1e4

def drop_small_zouit(zin, mpp, min_ha=0.25):
    """Отсечь язычки ЗОУИТ мельче min_ha: границей ЗУ от охранной зоны
    отрезаются полоски в 3-5 м, которые на схеме читаются как ошибка."""
    lb, n = ndimage.label(zin)
    if not n:
        return zin
    sz = ndimage.sum(zin, lb, range(1, n + 1)) * (mpp * mpp / 10000)
    return np.isin(lb, [i + 1 for i, s in enumerate(sz) if s >= min_ha])
