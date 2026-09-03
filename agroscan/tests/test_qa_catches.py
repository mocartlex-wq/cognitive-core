# -*- coding: utf-8 -*-
"""Проверка проверок: qa обязана падать на испорченной геометрии.

Защита, которая только пропускает хорошее, ничего не защищает. Здесь
геометрия ломается намеренно — каждый случай должен быть пойман.
"""
import json
import os
import sys

from shapely.affinity import translate
from shapely.geometry import Polygon

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agroscan import qa as qa_mod, zones as z

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'out', '58-24-0341802-1173')
EGRN = 141.6154

def _load():
    rings = json.load(open(os.path.join(ROOT, 'data', '1173', 'c1173.json')))
    parcel = z.parcel_poly(rings)
    Z = {}
    for k in '1234':
        d = json.load(open(os.path.join(OUT, 'chzu%s.json' % k)))
        Z[k] = z.to_poly(d['outer'], d['inner'])
    return Z, parcel

def test_clean_passes():
    Z, parcel = _load()
    assert qa_mod.check(Z, parcel, EGRN)['пройдено']

def test_catches_outside_parcel():
    Z, parcel = _load()
    Z['4'] = translate(Z['4'], 3000, 3000)
    q = qa_mod.check(Z, parcel, EGRN)
    assert not q['пройдено'] and 'части внутри границы ЕГРН' in q['провалено']

def test_catches_overlap():
    Z, parcel = _load()
    Z['2'] = Z['2'].buffer(5)
    q = qa_mod.check(Z, parcel, EGRN)
    assert not q['пройдено'] and 'части не накладываются' in q['провалено']

def test_catches_hole():
    Z, parcel = _load()
    Z['1'] = Z['1'].difference(Z['1'].representative_point().buffer(56))
    q = qa_mod.check(Z, parcel, EGRN)
    assert not q['пройдено'] and 'участок покрыт без зазоров' in q['провалено']

def test_catches_sliver():
    Z, parcel = _load()
    b = Z['4'].bounds
    Z['4'] = Z['4'].union(Polygon([(b[0] - 200, b[1] - 200), (b[0] - 100, b[1] - 200),
                                   (b[0] - 100, b[1] - 199), (b[0] - 200, b[1] - 199)]))
    q = qa_mod.check(Z, parcel, EGRN)
    assert not q['пройдено'] and 'нет частей уже 3 м' in q['провалено']

# ── лесничества: чужая категория земель под ногами ──────────────────────
def _box(x0, y0, w, h):
    return Polygon([(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)])

def test_forest_is_excluded_and_checked():
    """Лесничество вычтено из рабочей площади, части в него не заходят."""
    parcel = _box(0, 0, 100, 100)                 # 1 га
    forest = _box(0, 0, 100, 10)                  # полоса 0,1 га вдоль края
    Z = {'1': parcel.difference(forest)}          # часть построена без лесничества
    q = qa_mod.check(Z, parcel.difference(forest), 1.0, forest=forest, forest_ha=0.1)
    assert q['пройдено'], q['провалено']

def test_forest_area_must_be_counted():
    """Если вычтенную площадь не учесть, сходимость с ЕГРН обязана упасть."""
    parcel = _box(0, 0, 100, 100)
    forest = _box(0, 0, 100, 10)
    Z = {'1': parcel.difference(forest)}
    q = qa_mod.check(Z, parcel.difference(forest), 1.0, forest=forest, forest_ha=0.0)
    assert not q['пройдено'] and 'сумма частей = площадь ЕГРН' in q['провалено']

def test_catches_part_inside_forest():
    """Часть, залезшая в лесничество, обязана валить проверку."""
    parcel = _box(0, 0, 100, 100)
    forest = _box(0, 0, 100, 10)
    Z = {'1': parcel}                             # часть построена без вычитания
    q = qa_mod.check(Z, parcel, 1.0, forest=forest, forest_ha=0.1)
    assert not q['пройдено'], q
    assert 'части не накладываются на лесничества' in q['провалено'], q['провалено']

def test_extra_points_are_found_and_removed():
    """Лишние вершины: совпадающая и лежащая на прямой.

    Прямо из :29: каталог координат выходил на 15 точек вместо 12 по КПТ —
    со строкой длиной 0,00 м и углом 180°00\'00". Обратный случай здесь
    первый: испорченное кольцо обязано быть распознано как грязное.
    """
    dirty = [[0, 0], [50, 0], [100, 0],              # средняя точка на прямой
             [100, 100], [100.0000001, 100.0000001],  # совпадающая вершина
             [0, 100]]
    assert z.extra_points([dirty]) == 2, z.clean_ring(dirty)
    clean = z.clean_ring(dirty)
    assert len(clean) == 4 and z.extra_points([clean]) == 0, clean
    assert abs(Polygon(clean).area - Polygon(dirty).area) < 0.01

    # обобщать контур чистка не должна: вершина в 5 мм от прямой — геометрия
    near = [[0, 0], [50, 0.005], [100, 0], [100, 100], [0, 100]]
    assert z.clean_ring(near) == near

    # реальная геометрия чистку переживает: квадрат со срезанным углом
    keep = [[0, 0], [100, 0], [100, 100], [50, 100.5], [0, 100]]
    assert z.clean_ring(keep) == keep

def test_qa_sees_dirty_rings():
    """Проверка «нет лишних точек» падает, если чистку отключить."""
    parcel = _box(0, 0, 100, 100)
    Z = {'1': Polygon([(0, 0), (50, 0), (100, 0), (100, 100), (0, 100)])}  # лишняя середина
    q = qa_mod.check(Z, parcel, 1.0)
    assert q['пройдено'], q['провалено']
    orig = z.to_rings
    try:                                          # чистка выключена — брак виден
        z.to_rings = lambda g, minha=0.0002, clean=True: orig(g, minha, False)
        q = qa_mod.check(Z, parcel, 1.0)
    finally:
        z.to_rings = orig
    assert 'в координатах нет лишних точек' in q['провалено'], q['провалено']


if __name__ == '__main__':
    ok = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_'):
            fn(); ok += 1
            print('  ✓ %s' % name)
    print('пройдено %d проверок' % ok)
