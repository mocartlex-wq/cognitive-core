# -*- coding: utf-8 -*-
"""Классификация зарастания: проективное покрытие крон + проверка по ДЗЗ.

Кроны выделяются по ортофото 0,6 м (детальность, актуальность), а спорные
места уточняются данными, которых на ортофото нет:
  • Sentinel-2 NDMI — влагосодержание, у древесной биомассы оно кратно выше
    травяного (замер на 1173: трава 0,036, сильное зарастание 0,248);
  • карта высот полога — прямой признак древесной растительности.

Это чинит две конкретные ошибки прежней схемы: зарастание, которое ортофото
не показало из-за освещения, и класс «открытая почва», под который попадала
обычная трава (на 1173 — 3 га, которые пришлось перераспределять руками).
"""
import numpy as np
from scipy import ndimage

GRADES = ((0.10, 1), (0.30, 2), (0.60, 3))   # порог покрытия → класс
NAMES = {0: 'травяно-кустарниковая', 1: 'слабое (10-30 %)', 2: 'среднее (30-60 %)',
         3: 'сильное (>= 60 %)', 7: 'открытая почва без растительности'}

def _integral(a):
    s = np.zeros((a.shape[0] + 1, a.shape[1] + 1), np.float64)
    s[1:, 1:] = a.cumsum(0).cumsum(1)
    return s

def boxmean(a, rad):
    """Среднее в квадратном окне радиуса rad пикселей (через интегральное изображение)."""
    H, W = a.shape
    S = _integral(a)
    y0 = np.clip(np.arange(H) - rad, 0, H); y1 = np.clip(np.arange(H) + rad + 1, 0, H)
    x0 = np.clip(np.arange(W) - rad, 0, W); x1 = np.clip(np.arange(W) + rad + 1, 0, W)
    A = S[np.ix_(y1, x1)] - S[np.ix_(y0, x1)] - S[np.ix_(y1, x0)] + S[np.ix_(y0, x0)]
    return A / ((y1 - y0)[:, None] * (x1 - x0)[None, :])

def crowns(rgb, grn_min=6.0, lum_max=68.0):
    """Кроны по ортофото: зелёный превышает красный при пониженной яркости."""
    R, G, B = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    L = 0.299 * R + 0.587 * G + 0.114 * B
    return ((G - R > grn_min) & (L < lum_max)).astype(np.float32), (G - R)

def cover(crown, mpp, window_m=25.0):
    """Проективное покрытие крон в окне window_m."""
    return boxmean(crown, max(1, int(window_m / 2 / mpp)))

def grade(cov, mask, grn, ndvi=None, ndmi=None, chm=None,
          bare_ndvi=0.50, wood_ndmi=0.15, wood_h=3.0):
    """Классы зарастания с уточнением по ДЗЗ.

    ndvi/ndmi/chm — в той же сетке, что cov, или None. Правила уточнения:
      • cov ниже 10 %, но NDMI древесный и полог выше 3 м → класс 1;
      • «открытая почва» ставится, только если NDVI это подтверждает,
        иначе трава.
    """
    cls = np.full(cov.shape, -1, np.int16)
    bare = mask & (cov < GRADES[0][0]) & (grn <= 2)
    if ndvi is not None:
        bare &= (ndvi < bare_ndvi)                 # трава при высоком NDVI — не почва
    cls[mask & (cov < GRADES[0][0])] = 0
    cls[bare] = 7
    for lo, c in GRADES:
        cls[mask & (cov >= lo)] = c
    if ndmi is not None and chm is not None:
        missed = mask & (cls <= 0) & (ndmi >= wood_ndmi) & (np.nan_to_num(chm) >= wood_h)
        cls[missed] = 1
        return cls, int(missed.sum())
    return cls, 0

def generalize(cls, mask, mpp, min_ha=0.30, rounds=3):
    """Растворить ареалы мельче min_ha в окружающем классе."""
    cellHa = mpp * mpp / 10000
    tmp = cls.copy(); tmp[~mask] = 9
    cls = np.where(mask, ndimage.median_filter(tmp, size=int(15 / mpp) | 1), -1)
    for _ in range(rounds):
        small = np.zeros_like(mask)
        for c in np.unique(cls[mask]):
            m = (cls == c) & mask
            lb, n = ndimage.label(m)
            if not n:
                continue
            sz = ndimage.sum(m, lb, range(1, n + 1)) * cellHa
            bad = [i + 1 for i, s in enumerate(sz) if s < min_ha]
            if bad:
                small |= np.isin(lb, bad)
        if not small.any():
            break
        ind = ndimage.distance_transform_edt(~(mask & ~small), return_distances=False,
                                             return_indices=True)
        cls = np.where(small, cls[tuple(ind)], cls)
        cls = np.where(mask, cls, -1)
    tmp = cls.copy(); tmp[~mask] = 9
    cls = np.where(mask, ndimage.median_filter(tmp, size=int(11 / mpp) | 1), -1)
    # 9 — служебная заливка вне участка; медианный фильтр может занести её
    # внутрь у самой границы. Такие пиксели забирают класс ближайшего соседа.
    bad = mask & (cls == 9)
    if bad.any():
        ind = ndimage.distance_transform_edt(~(mask & ~bad), return_distances=False,
                                             return_indices=True)
        cls = np.where(bad, cls[tuple(ind)], cls)
    return np.where(mask, cls, -1).astype(np.int16)

def areas(cls, mask, mpp, egrn_ha=None):
    """Площади классов, при необходимости откалиброванные к площади ЕГРН."""
    cellHa = mpp * mpp / 10000
    raw = {int(c): float((cls == c).sum() * cellHa) for c in np.unique(cls[mask])}
    if egrn_ha:
        k = egrn_ha / max(sum(raw.values()), 1e-9)
        raw = {c: a * k for c, a in raw.items()}
    return raw
