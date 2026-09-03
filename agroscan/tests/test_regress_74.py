# -*- coding: utf-8 -*-
"""Регресс на 58:28:0500401:74 — участок с действующей пашней.

Здесь части покрывают участок не целиком: 75 га заняты обрабатываемой
пашней, она не является ЧЗУ. Эталон — расчёт, выпущенный до сборки
конвейера; допуск шире, чем на 1173, потому что новое правило класса
«открытая почва» переносит часть площади между пашней и залежью.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agroscan.pipeline import run

ЭТАЛОН = {'1': 49.61, '2': 4.50}
ДОПУСК = {'1': 1.0, '2': 3.0}
EGRN = 132.4146

def test_74():
    cfg = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       'parcels', '58-28-0500401-74.yaml')
    with tempfile.TemporaryDirectory() as tmp:
        res, qa = run(cfg, out_dir=tmp, sheets=False, formats=False)
    assert qa['пройдено'], 'проверки не пройдены: %s' % qa['провалено']
    assert set(res) == {'1', '2'}, 'лишние части: %s' % sorted(res)
    s = sum(v['areaHa'] for v in res.values())
    assert s < EGRN, 'части не могут занимать весь участок: %.2f из %.2f' % (s, EGRN)
    for k, ref in ЭТАЛОН.items():
        got = res[k]['areaHa']
        assert abs(got - ref) <= ДОПУСК[k], 'ЧЗУ/%s: %.2f га против эталонных %.2f' % (k, got, ref)

if __name__ == '__main__':
    test_74()
    print('\nРЕГРЕСС :74 ПРОЙДЕН')
