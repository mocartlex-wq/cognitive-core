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
KPT29 = os.path.join(ROOT, 'data', 'kpt', '58_17_0130701_2026-07-15_kpt11.xml')

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
def test_forests_are_separate_from_zouit():
    """Лесничество (код 15) — не ЗОУИТ: это чужая категория земель."""
    zones = [{'код': '6', 'реестровый_номер': '58:17-6.22', 'наименование': 'ЛЭП',
              'тип': '', 'кольца': []},
             {'код': '15', 'реестровый_номер': '58:00-15.12',
              'наименование': 'Лопатинское лесничество', 'тип': '', 'кольца': []},
             {'код': '7', 'реестровый_номер': '58:17-7.164', 'наименование': 'угодья',
              'тип': '', 'кольца': []}]
    assert [z['код'] for z in kpt.zouit(zones)] == ['6']
    fr = kpt.forests(zones)
    assert len(fr) == 1 and fr[0]['реестровый_номер'] == '58:00-15.12', fr

def test_address_and_landmark_come_from_kpt():
    """Адрес и ориентир берутся из КПТ, а не с рук.

    В конфиге :29 был вписан чужой район (Лопатинский вместо
    Малосердобинского), поэтому адрес теперь читается из выгрузки.
    """
    P, _, _ = kpt.parse(KPT29)
    r = kpt.find(P, '58:17:0130701:29')
    a = kpt.address_of(r)
    assert a == 'Пензенская область, Малосердобинский район, Старославкинский сельсовет', a
    # readable_address здесь короче ФИАС-блока — берётся сборка
    assert r['адрес'] == 'обл. Пензенская, р-н Малосердобинский', r['адрес']
    lm = kpt.landmark_of(r)
    assert lm.startswith('с. Новое Славкино'), lm
    assert '4,9 км' in lm and 'юго-восток' in lm, lm

def test_address_keeps_descriptive_form():
    """Описательный адрес выписки длиннее ФИАС и отдаётся как есть."""
    P, _, _ = kpt.parse(VYP)
    a = kpt.address_of(P[0])
    assert a.startswith('Местоположение установлено'), a
    assert 'с.Верхозим' in a, a
    assert kpt.landmark_of(P[0]) == ''          # rel_position в выписке нет

    P, _, _ = kpt.parse(KPT)
    r = kpt.find(P, '58:24:0341802:1173')
    assert kpt.address_of(r) == 'Пензенская область, Пензенский район, Мичуринский сельсовет'


if __name__ == '__main__':
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_'):
            fn(); n += 1; print('  ✓ %s' % name)
    print('\nПАРСЕР КПТ ПРОВЕРЕН (%d теста)' % n)
