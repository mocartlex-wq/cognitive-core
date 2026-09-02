# -*- coding: utf-8 -*-
"""Разбор ответа soil-db.ru: контур карты Фридланда под точкой участка.

Сеть здесь не нужна — проверяется то, что ломается молча: попадание точки
в полигон и разбор таблицы атрибутов, где значения приходят с разметкой.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agroscan.sources import fridland_map as fm

SQUARE = {'type': 'Polygon', 'coordinates': [
    [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],
    [[4, 4], [6, 4], [6, 6], [4, 6], [4, 4]]]}          # с вырезом

# фрагмент реального ответа showFeatureData для контура 22087 (участок 1173)
ROWS = [
    {'label': 'Id', 'value': 12121},
    {'label': 'Почва (почвенный комплекс) основная',
     'value': '117 — <strong>Ч<sup>в</sup></strong><br />Черноземы выщелоченные'},
    {'label': 'Почва (почвенный комплекс) сопутствующая 1',
     'value': '116 — <strong>Ч<sup>оп</sup></strong><br />Черноземы оподзоленные'},
    {'label': 'Почва (почвенный комплекс) сопутствующая 2', 'value': '—'},
    {'label': 'Порода основная', 'value': 'Среднесуглинистые'},
    {'label': 'Порода сопутствующая', 'value': '—'},
    {'label': 'Площадь, км<sup>2</sup>', 'value': '620.965'},
]

def test_point_in_polygon():
    assert fm.point_in((1, 1), SQUARE)
    assert not fm.point_in((11, 1), SQUARE), 'снаружи'
    assert not fm.point_in((5, 5), SQUARE), 'в вырезе контур не считается'

def test_point_in_multipolygon():
    multi = {'type': 'MultiPolygon', 'coordinates': [
        [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
        [[[5, 5], [7, 5], [7, 7], [5, 7], [5, 5]]]]}
    assert fm.point_in((6, 6), multi)
    assert not fm.point_in((3, 3), multi)

def test_rows_parsed():
    r = fm.parse_rows(ROWS)
    assert r['индекс'] == 'чв' and r['код'] == 117, r
    assert r['название'] == 'Черноземы выщелоченные', r
    assert r['порода'] == 'Среднесуглинистые', r
    assert r['площадь_км2'] == 621.0, r
    assert r['сопутствующие'] == ['Черноземы оподзоленные (чоп)'], r
    assert 'Id' not in str(r), 'служебные строки в результат не идут'

def test_empty_rows_give_nothing():
    assert fm.parse_rows([]) == {}
    assert fm.parse_rows([{'label': 'Почва основная', 'value': '—'}]) == {}

def test_lookup_survives_dead_host():
    """Сайт сменил протокол или не ответил — None, а не исключение."""
    base = fm.BASE
    try:
        fm.BASE = 'https://soil-db.invalid'
        assert fm.lookup(44.9, 53.125, timeout=5) is None
    finally:
        fm.BASE = base

if __name__ == '__main__':
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_'):
            fn(); n += 1; print('  ✓ %s' % name)
    print('\nКАРТА ФРИДЛАНДА: РАЗБОР ПРОВЕРЕН (%d проверок)' % n)
