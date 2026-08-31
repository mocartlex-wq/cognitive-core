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

if __name__ == '__main__':
    ok = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_'):
            fn(); ok += 1
            print('  ✓ %s' % name)
    print('пройдено %d проверок' % ok)
