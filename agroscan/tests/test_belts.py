# -*- coding: utf-8 -*-
"""Автопоиск лесополос: разветвлённая полоса не должна теряться.

Признак «вытянутость по осям эллипса» ломается на Y- и Г-образных
полосах: на 58:17:0130701:29 полоса шириной 28 м дала вытянутость 1,5
и отбрасывалась. Ширина от формы фигуры не зависит.
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agroscan import belts

MPP = 1.0                 # метр в пикселе — считать проще

def _canvas(n=400):
    return np.zeros((n, n), bool)

def _straight():
    m = _canvas(); m[100:120, 50:350] = True          # 20 × 300 м
    return m

def _branched():
    m = _canvas()
    m[100:120, 50:350] = True                          # ствол буквы Y
    m[120:300, 190:210] = True                         # ножка
    return m

def _grove():
    m = _canvas(); m[150:250, 150:250] = True          # куртина 100 × 100 м
    return m

def _detect(shape):
    chm = np.where(shape, 12.0, 0.0)                   # взрослый древостой
    mask = np.ones_like(shape, bool)
    out, kept = belts.detect(chm, mask, MPP, min_ha=0.05, close_m=3.0, open_m=1.0)
    return out, kept

def test_straight_belt_found_by_elongation():
    out, kept = _detect(_straight())
    assert kept and kept[0]['признак'] == 'вытянутость', kept
    assert out.sum() > 0

def test_branched_belt_found_by_width():
    """Именно этот случай и терялся: вытянутость мала, ширина мала."""
    out, kept = _detect(_branched())
    assert kept, 'разветвлённая полоса не найдена'
    assert kept[0]['признак'] == 'ширина', kept
    assert kept[0]['вытянутость'] < 3.0, kept
    assert kept[0]['ширина_по_скелету_м'] <= 40, kept

def test_grove_is_not_a_belt():
    """Обратный случай: куртина 100 × 100 м полосой быть не должна."""
    out, kept = _detect(_grove())
    assert not kept, kept
    assert out.sum() == 0

def test_width_measure():
    m = _canvas(); m[100:130, 50:350] = True           # ровно 30 м шириной
    assert 26 <= belts.width_m(m, MPP) <= 34, belts.width_m(m, MPP)
    sq = _canvas(); sq[150:250, 150:250] = True        # куртина 100 × 100 м
    assert belts.width_m(sq, MPP) > 50, belts.width_m(sq, MPP)

if __name__ == '__main__':
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_'):
            fn(); n += 1; print('  ✓ %s' % name)
    print('\nАВТОПОИСК ПОЛОС ПРОВЕРЕН (%d проверок)' % n)
