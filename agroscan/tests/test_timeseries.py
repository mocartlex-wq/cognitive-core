# -*- coding: utf-8 -*-
"""Датировка выбытия из оборота на известном ответе.

По 1173 прежний, собранный вручную комплект дал: последний год пара — 2010
(33,35 га), с 2011-го пар не фиксируется. Тест закрывает восстановленную
логику и, главное, выбор лет: шаг в два года однажды пропустил 2010-й
и сдвинул ответ на 2002-й, а лист при этом выглядел совершенно исправным.
"""
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agroscan import timeseries as ts
from agroscan.geo import Grid
from agroscan.sources import landsat

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _series():
    meta = json.load(open(os.path.join(ROOT, 'data', '1173', 'bgmeta.json')))
    g = Grid(meta, 'msk58-2', step=8)
    years = [1977, 1985, 1990, 1995] + list(range(1999, 2027))
    ser, _ = landsat.ndvi_series(g, years, os.path.join(ROOT, 'cache', 'landsat',
                                                        '58-24-0341802-1173'), verbose=False)
    mask = g.submask(np.load(os.path.join(ROOT, 'out', '58-24-0341802-1173', 'mask.npy')))
    return ser, mask, g

def test_abandonment_year_1173():
    ser, mask, g = _series()
    s = ts.summary(ser, mask, g.cellHa, this_year=2026)
    assert s['последний_год_пара'] == 2010, s['последний_год_пара']
    assert s['год_выбытия'] == 2011, s['год_выбытия']
    assert s['год_выбытия_точный'], 'в ряду разрыв: %s' % s['интервал_выбытия']
    assert abs(s['пар_в_последний_год_га'] - 33.35) < 1.0, s['пар_в_последний_год_га']
    assert s['ряд'][2011]['пар_доля'] == 0.0

def test_gap_is_reported_not_hidden():
    """При разрыве в ряду дата не выдаётся за точную."""
    ser, mask, g = _series()
    thin = {y: a for y, a in ser.items() if y % 2 == 1}      # выбрасываем чётные годы
    s = ts.summary(thin, mask, g.cellHa, this_year=2026)
    assert not s['год_выбытия_точный'], 'разрыв в ряду выдан за точную дату'
    assert s['интервал_выбытия'], s

if __name__ == '__main__':
    test_abandonment_year_1173(); print('  ✓ год выбытия 2011 по полному ряду')
    test_gap_is_reported_not_hidden(); print('  ✓ разрыв в ряду не выдаётся за точную дату')
    print('\nДАТИРОВКА ПРОВЕРЕНА')
