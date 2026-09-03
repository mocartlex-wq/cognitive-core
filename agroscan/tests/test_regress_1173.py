# -*- coding: utf-8 -*-
"""Регресс на 58:24:0341802:1173.

Эталон — комплект, выпущенный вручную до сборки конвейера. Задача теста не
«совпасть в ноль», а не дать площадям уехать незаметно: допуск 0,5 га на
часть, при этом сумма частей обязана сходиться с ЕГРН и все проверки qa
должны проходить. Отличия от эталона объясняются в CHANGES.md.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agroscan.pipeline import run

ЭТАЛОН = {'1': 81.63, '2': 13.97, '3': 41.56, '4': 4.46}
ДОПУСК = 0.5
EGRN = 141.6154

def test_1173():
    cfg = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       'parcels', '58-24-0341802-1173.yaml')
    res, qa = run(cfg)          # намеренно в out/: полный прогон обновляет комплект
    assert qa['пройдено'], 'проверки не пройдены: %s' % qa['провалено']
    s = sum(v['areaHa'] for v in res.values())
    assert abs(s - EGRN) / EGRN * 100 <= 0.01, 'сумма частей %.4f вместо %.4f' % (s, EGRN)
    for k, ref in ЭТАЛОН.items():
        got = res[k]['areaHa']
        assert abs(got - ref) <= ДОПУСК, 'ЧЗУ/%s: %.2f га против эталонных %.2f' % (k, got, ref)

if __name__ == '__main__':
    test_1173()
    print('\nРЕГРЕСС ПРОЙДЕН')
