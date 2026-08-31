# -*- coding: utf-8 -*-
"""Ряд NDVI: когда участок вышел из оборота и как зарастал.

Пар и свежая вспашка дают летом NDVI ниже 0,45 — голая почва. Сомкнутый
древостой держит выше 0,70. Год выбытия — последний, в котором пар ещё
занимает заметную долю участка; дальше он не появляется ни на одном снимке.

Логика была написана по месту и файлом не сохранилась; здесь восстановлена
и закреплена тестом на известном ответе по 1173: пар 23,5 % площади в 2010
и 0,0 % с 2011 года.
"""
import numpy as np
from scipy import ndimage

FALLOW = 0.45      # ниже — голая почва
CANOPY = 0.70      # выше — сомкнутый полог
MIN_FALLOW_SHARE = 0.05   # доля участка, ниже которой пар считаем шумом

def shares(series, mask, cellHa=None):
    """{год: доли и медиана} по ряду {год: массив NDVI}."""
    out = {}
    for y in sorted(series):
        a = series[y]
        v = a[mask & np.isfinite(a)]
        if len(v) < 20:
            continue
        row = {'медиана': round(float(np.median(v)), 3),
               'пар_доля': round(float((v < FALLOW).mean()), 4),
               'полог_доля': round(float((v > CANOPY).mean()), 4)}
        if cellHa:
            n = mask.sum()
            row['пар_га'] = round(row['пар_доля'] * n * cellHa, 2)
            row['полог_га'] = round(row['полог_доля'] * n * cellHa, 2)
        out[y] = row
    return out

def abandonment_year(rows, min_share=MIN_FALLOW_SHARE):
    """Год прекращения обработки: последний с паром выше порога, плюс один.

    Возвращает (год_выбытия, последний_год_пара, доля_в_нём) или (None, ...),
    если пар не фиксируется вовсе — тогда участок вышел из оборота раньше
    начала ряда, и по оптике дату не установить.
    """
    with_fallow = [y for y, r in rows.items() if r['пар_доля'] >= min_share]
    if not with_fallow:
        return None, None, 0.0
    last = max(with_fallow)
    after = sorted(y for y in rows if y > last)
    if not after:
        return None, last, rows[last]['пар_доля']
    # если следующего года в ряду нет, дата известна лишь до промежутка —
    # так и возвращаем, вместо того чтобы выдавать соседнюю сцену за истину
    return (last + 1 if after[0] == last + 1 else (last + 1, after[0])), last, rows[last]['пар_доля']

def erode(mask, cell_m, source_m=30.0):
    """Убрать краевую полосу шириной с пиксель источника.

    Пиксель Landsat 30 м на границе контура смешивает зарастающую часть с
    соседней пашней. На :74 из-за этого в 2024-м «появился пар» тонкой каймой
    по краю, и дата выбытия уехала с 2011 на 2025.
    """
    r = max(1, int(round(source_m / cell_m)))
    y, x = np.mgrid[-r:r + 1, -r:r + 1]
    return ndimage.binary_erosion(mask, x * x + y * y <= r * r)

def summary(series, mask, cellHa, this_year=None, edge_m=30.0, cell_m=None):
    if edge_m and cell_m:
        inner = erode(mask, cell_m, edge_m)
        if inner.sum() > 0.3 * mask.sum():      # у узких контуров эрозия съест всё
            mask = inner
    rows = shares(series, mask, cellHa)
    year, last, share = abandonment_year(rows)
    точно = not isinstance(year, tuple)
    res = {'ряд': rows, 'год_выбытия': year if точно else year[0],
           'год_выбытия_точный': точно,
           'интервал_выбытия': None if точно else list(year),
           'последний_год_пара': last,
           'пропуски_в_ряду': [y for y in range(min(rows), max(rows) + 1) if y not in rows][:40]
           if rows else [],
           'пар_в_последний_год_га': round(share * mask.sum() * cellHa, 2) if last else 0.0}
    if year and this_year:
        res['возраст_зарастания_лет'] = this_year - res['год_выбытия']
    if rows:
        yy = sorted(rows)
        res['полог_первый_год'] = rows[yy[0]]['полог_доля']
        res['полог_последний_год'] = rows[yy[-1]]['полог_доля']
    return res
