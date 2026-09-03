# -*- coding: utf-8 -*-
"""Ближайший населённый пункт: румбы, сокращения, азимут и отказ источника.

Проверяется то, на чём легко ошибиться: направление считается ОТ участка
К селу (в КПТ оно записано наоборот), а недоступный Overpass не должен
превращаться в «вокруг ничего нет».
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agroscan.sources import places

LON, LAT = 45.1975, 52.5095          # центр участка 58:17:0130701:29

def test_rhumb_and_short():
    assert places.rhumb(0) == 'север' and places.rhumb(359) == 'север'
    assert places.rhumb(300) == 'северо-запад' and places.short(300) == 'СЗ'
    assert places.rhumb(127) == 'юго-восток' and places.rhumb(201) == 'юг'
    assert places.rhumb(45) == 'северо-восток' and places.rhumb(46) == 'северо-восток'

def test_name_by_place_type():
    assert places.name_of({'название': 'Новое Славкино', 'тип': 'village'}) == 'с. Новое Славкино'
    assert places.name_of({'название': 'Круглое', 'тип': 'hamlet'}) == 'д. Круглое'
    assert places.name_of({'название': 'Пенза', 'тип': 'city'}) == 'г. Пенза'
    assert places.name_of({'название': 'Без типа', 'тип': None}) == 'Без типа'

def test_geo_direction_and_distance():
    """Азимут и расстояние: восток — 90°, север — 0°, юг — 180°."""
    km, az = places.geo(LON, LAT, LON + 0.1, LAT)
    assert abs(az - 90) < 1 and 6 < km < 8, (km, az)
    km, az = places.geo(LON, LAT, LON, LAT + 0.1)
    assert abs(az) < 1 and 10 < km < 12, (km, az)
    _, az = places.geo(LON, LAT, LON, LAT - 0.1)
    assert abs(az - 180) < 1, az

def test_line_format():
    p = {'название': 'Новое Славкино', 'тип': 'village', 'км': 4.75, 'азимут': 300}
    assert places.line(p) == 'с. Новое Славкино — 4,8 км на северо-запад'
    assert places.line(None) == ''

def test_broken_source_returns_empty():
    """Источник не отвечает — пустой список, а не выдуманный пункт."""
    saved = places.MIRRORS
    try:
        places.MIRRORS = ('https://overpass.invalid.example/api/interpreter',)
        assert places.nearest(LON, LAT, radius_m=15001, no_cache=True) == []
    finally:
        places.MIRRORS = saved

def test_nearest_matches_kpt_landmark():
    """Измеренное совпадает с ориентиром из КПТ: с. Новое Славкино, ~4,9 км.

    Направление в КПТ — на юго-восток ОТ ориентира, значит от участка к селу
    это северо-запад; азимут обязан быть около 300°.
    """
    got = places.nearest(LON, LAT)
    if not got:
        print('    (Overpass не ответил — сетевая часть пропущена)')
        return
    p = got[0]
    assert p['название'] == 'Новое Славкино', p
    assert 4.0 < p['км'] < 5.5, p
    assert abs(p['азимут'] - 300) <= 3, p
    assert places.rhumb(p['азимут']) == 'северо-запад'
    assert got == sorted(got, key=lambda q: q['км'])


if __name__ == '__main__':
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_'):
            fn(); n += 1; print('  ✓ %s' % name)
    print('\nБЛИЖАЙШИЙ НАСЕЛЁННЫЙ ПУНКТ ПРОВЕРЕН (%d проверок)' % n)
