# -*- coding: utf-8 -*-
"""Схема ЧЗУ: подписи характерных точек не должны дублироваться."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agroscan.sheets.schema import extreme_points

def test_four_distinct_points():
    """Угловая вершина бывает крайней сразу в двух направлениях."""
    ring = [(0, 0), (10, 0), (10, 10), (0, 10)]        # квадрат: углы крайние дважды
    idx = extreme_points(ring)
    assert len(idx) == 4 and len(set(idx)) == 4, idx

def test_real_parcel_case():
    """:29 — юго-восточный угол был и самым южным, и самым восточным."""
    ring = [(0, 0), (5, 9), (9, 1), (9.5, 0.5)]        # вершина 3 крайняя дважды
    idx = extreme_points(ring)
    assert len(set(idx)) == 4, idx
    assert idx[0] == 1, 'самая северная — вершина 1'

def test_order_is_n_s_w_e():
    ring = [(0, 5), (5, 10), (10, 5), (5, 0)]
    n, s, w, e = extreme_points(ring)
    assert ring[n][1] == 10 and ring[s][1] == 0
    assert ring[w][0] == 0 and ring[e][0] == 10

if __name__ == '__main__':
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_'):
            fn(); n += 1; print('  ✓ %s' % name)
    print('\nСХЕМА: ПОДПИСИ ТОЧЕК ПРОВЕРЕНЫ (%d проверок)' % n)
