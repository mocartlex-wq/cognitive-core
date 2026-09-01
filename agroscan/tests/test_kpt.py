# -*- coding: utf-8 -*-
"""Парсер КПТ на реальных файлах.

Проверяет то, ради чего он писался: границы совпадают с прежними, готовыми
вручную; площадь по координатам сходится с площадью ЕГРН из того же файла;
зоны отбираются по коду, а накрывающие участок целиком не попадают в ЧЗУ/3.
"""
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agroscan import kpt
from agroscan.geo import Grid
from agroscan.rings import rasterize

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KPT = os.path.join(ROOT, 'data', 'kpt', '58_24_0341802_2025-08-23_kpt11.xml')
VYP = os.path.join(ROOT, 'data', 'kpt', 'vypiska74.xml')

def test_kpt_parses_and_area_matches():
    P, Z, sk = kpt.parse(KPT)
    assert len(P) > 300 and len(Z) > 40, (len(P), len(Z))
    assert kpt.zone_of(sk) == 'msk58-2', sk
    p = kpt.find(P, '58:24:0341802:1173')
    assert p and len(p['кольца']) == 3
    # площадь по координатам обязана сходиться с площадью ЕГРН из того же файла
    assert abs(kpt.area_of(p['кольца']) - p['площадь_егрн']) < 5, (
        kpt.area_of(p['кольца']), p['площадь_егрн'])

def test_rings_match_manual_file():
    """Границы из КПТ совпадают с теми, что готовились вручную."""
    P, _, _ = kpt.parse(KPT)
    p = kpt.find(P, '58:24:0341802:1173')
    old = json.load(open(os.path.join(ROOT, 'data', '1173', 'c1173.json')))
    assert len(p['кольца']) == len(old)
    for a, b in zip(p['кольца'], old):
        b = b[:-1] if b[0] == b[-1] else b        # в старом файле кольца замкнуты
        assert len(a) == len(b)
        assert np.allclose(np.array(sorted(map(tuple, a))), np.array(sorted(map(tuple, b))))

def test_zouit_excludes_wide_zones():
    """Приаэродромные подзоны накрывают участок целиком и в ЧЗУ/3 не идут."""
    P, Z, _ = kpt.parse(KPT)
    p = kpt.find(P, '58:24:0341802:1173')
    meta = json.load(open(os.path.join(ROOT, 'data', '1173', 'bgmeta.json')))
    g = Grid(meta, 'msk58-2')
    mask = rasterize([p['кольца'][0]], g, p['кольца'][1:])
    cell = meta['mpp'] ** 2 / 1e4
    total = mask.sum() * cell
    zn = np.zeros_like(mask); wide = 0
    for z in kpt.zouit(Z):
        m = rasterize(z['кольца'], g) & mask
        if not m.any():
            continue
        if m.sum() * cell / total >= 0.95:
            wide += 1
        else:
            zn |= m
    assert wide >= 1, 'зоны, накрывающие участок целиком, не обнаружены'
    old = np.load(os.path.join(ROOT, 'data', '1173', 'zouit_narrow.npy')) & mask
    inter = (zn & old).sum(); union = (zn | old).sum()
    assert inter / max(union, 1) > 0.99, 'IoU с прежней маской %.3f' % (inter / max(union, 1))

def test_vypiska_zone_fallback():
    """В выписке вместо кода СК стоит текст — зона берётся из координат."""
    P, _, sk = kpt.parse(VYP)
    assert len(P) == 1
    p = P[0]
    assert not kpt.SK_RE.match(sk or ''), sk
    assert kpt.zone_of(sk, kn=p['кн'], rings=p['кольца']) == 'msk58-2'
    assert abs(kpt.area_of(p['кольца']) - 1324146) < 5

if __name__ == '__main__':
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_'):
            fn(); n += 1; print('  ✓ %s' % name)
    print('\nПАРСЕР КПТ ПРОВЕРЕН (%d теста)' % n)
