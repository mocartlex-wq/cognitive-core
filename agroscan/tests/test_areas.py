# -*- coding: utf-8 -*-
"""Сведение площадей частей с площадью по сведениям ЕГРН.

На :29 площадь ЧЗУ/1 по координатам — 21 911,61 м², в ЕГРН записано
21 911. Округление давало 21 912, и в одном документе стояли две разные
площади одного контура. Здесь проверяется и прямой случай (сумма сошлась),
и обратный: расхождение больше допуска сводить нельзя — его ловит qa.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agroscan import areas

def test_single_part_matches_egrn():
    p = {'1': {'areaHa': 2.191161}}
    info = areas.fit(p, 2.1911)
    assert info['сведено'] and areas.m2(p['1']) == 21911, (info, p)
    assert areas.ha(p['1']) == 2.1911

def test_sum_equals_egrn_and_shift_is_small():
    p = {'1': {'areaHa': 81.39502}, '2': {'areaHa': 14.10834},
         '3': {'areaHa': 41.65364}, '4': {'areaHa': 4.45862}}
    info = areas.fit(p, 141.6154)
    assert sum(areas.m2(v) for v in p.values()) == 1416154, info
    for k, v in p.items():
        assert abs(areas.m2(v) - v['areaHa'] * 10000) <= 2.0, (k, info['сдвиг_м2'])

def test_no_fit_when_parts_do_not_cover_parcel():
    """:74 — части покрывают участок не целиком, подгонять нечего."""
    p = {'1': {'areaHa': 50.0611}, '2': {'areaHa': 7.0097}}
    info = areas.fit(p, 132.4146, cover_all=False)
    assert not info['сведено'] and 'м2' not in p['1']
    assert areas.m2(p['1']) == 500611          # запасной путь — обычное округление

def test_big_gap_is_not_hidden():
    """Расхождение в 3 % — ошибка геометрии, а не округление: не сводим."""
    p = {'1': {'areaHa': 2.0}}
    info = areas.fit(p, 2.1911)
    assert not info['сведено'], info
    assert areas.m2(p['1']) == 20000

def test_forest_area_stays_out_of_balance():
    """Лесничество вычтено из рабочей площади — цель уменьшается на него."""
    p = {'1': {'areaHa': 2.0911}}
    info = areas.fit(p, 2.1911, extra_ha=0.1)
    assert info['цель_м2'] == 20911 and areas.m2(p['1']) == 20911, info


if __name__ == '__main__':
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_'):
            fn(); n += 1; print('  ✓ %s' % name)
    print('\nСВЕДЕНИЕ ПЛОЩАДЕЙ ПРОВЕРЕНО (%d теста)' % n)
