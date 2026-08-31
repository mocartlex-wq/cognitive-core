# -*- coding: utf-8 -*-
"""Рельеф и овражно-балочная сеть.

Овраг исключается из оборота, а пологая ложбина — нет: техника её проходит.
Разделяет их крутизна бортов, поэтому каждая найденная форма приходит с
измерениями и вердиктом, а не просто попадает или не попадает в маску.

Считается на сетке 10 м: собственное разрешение Copernicus DEM около 30 м,
мельче дробить нечего.
"""
import numpy as np
from scipy import ndimage

from .rings import disk

def _downsample(a, k, how='mean'):
    h, w = a.shape[0] // k, a.shape[1] // k
    b = a[:h * k, :w * k].reshape(h, k, w, k)
    return b.mean((1, 3)) if how == 'mean' else b.mean((1, 3)) > 0.5

def fill_pits(z, rounds=60):
    """Залить локальные ямы, иначе сток обрывается на артефактах DEM."""
    f = z.copy()
    nb8 = np.array([[1, 1, 1], [1, 0, 1], [1, 1, 1]], bool)
    for _ in range(rounds):
        mn = ndimage.minimum_filter(f, size=3)
        new = np.where(f <= mn + 1e-9,
                       np.minimum(ndimage.minimum_filter(f, footprint=nb8) + 0.001, f + 5), f)
        if np.allclose(new, f, atol=1e-6):
            break
        f = np.maximum(f, new)
    return f

def flow(z, cell_m):
    """D8-направления и накопление водосбора, га."""
    off = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
    dist = [np.sqrt(2), 1, np.sqrt(2), 1, 1, np.sqrt(2), 1, np.sqrt(2)]
    best = np.full(z.shape, -1, np.int8); drop_best = np.zeros(z.shape)
    for i, ((dy, dx), dd) in enumerate(zip(off, dist)):
        nb = np.roll(np.roll(z, -dy, 0), -dx, 1)
        d = (z - nb) / (dd * cell_m)
        if dy < 0: d[0, :] = -9
        if dy > 0: d[-1, :] = -9
        if dx < 0: d[:, 0] = -9
        if dx > 0: d[:, -1] = -9
        upd = d > drop_best
        best[upd] = i; drop_best[upd] = d[upd]
    acc = np.ones(z.shape)
    ys, xs = np.unravel_index(np.argsort(z.ravel())[::-1], z.shape)
    for y, x in zip(ys, xs):
        b = best[y, x]
        if b < 0:
            continue
        dy, dx = off[b]
        acc[y + dy, x + dx] += acc[y, x]
    return acc * cell_m * cell_m / 10000

def analyze(dem, mask, mpp, grid_m=10.0, watershed_ha=5.0, min_ha=0.30,
            min_len_m=150, min_elong=1.6, gully_bank_deg=8.0):
    """Метрики рельефа и перечень овражно-балочных форм с вердиктом.

    gully_bank_deg — крутизна борта, начиная с которой форма считается
    оврагом. Ниже — ложбина стока: обрабатываемости не мешает.
    """
    k = max(1, int(round(grid_m / mpp)))
    z = ndimage.gaussian_filter(_downsample(dem.astype(np.float64), k), sigma=1.2)
    msk = _downsample(mask.astype(float), k) > 0.5
    z = fill_pits(z)
    acc = flow(z, grid_m)
    tpi = z - ndimage.uniform_filter(z, size=int(200 / grid_m))
    gy, gx = np.gradient(z, grid_m)
    slope = np.degrees(np.arctan(np.hypot(gx, gy)))

    tal = ndimage.binary_dilation((acc >= watershed_ha) & (tpi < -0.4), np.ones((3, 3)))
    valley = ndimage.binary_propagation(tal, mask=(tpi < -0.4))
    valley = ndimage.binary_closing(valley, np.ones((5, 5))) & msk

    lb, n = ndimage.label(valley, structure=np.ones((3, 3)))
    objs = ndimage.find_objects(lb)
    forms = []
    for i in range(1, n + 1):
        sl = objs[i - 1]; sub = (lb[sl] == i)
        a = sub.sum() * grid_m * grid_m / 10000
        if a < min_ha:
            continue
        yy, xx = np.nonzero(sub)
        yy = (yy + sl[0].start) * grid_m; xx = (xx + sl[1].start) * grid_m
        c = np.cov(np.stack([xx - xx.mean(), yy - yy.mean()]))
        ev = np.sort(np.linalg.eigvalsh(c))[::-1]
        if ev[1] <= 1e-9:
            continue
        ln = 4 * np.sqrt(ev[0]); wid = 4 * np.sqrt(ev[1])
        if ln < min_len_m or ln / wid < min_elong:
            continue
        bank = float(np.percentile(slope[sl][sub], 90))
        forms.append({'id': i, 'га': round(a, 2), 'длина_м': round(ln),
                      'ширина_м': round(wid), 'врез_м': round(float(-tpi[sl][sub].min()), 1),
                      'борт_град': round(bank, 1),
                      'макс_уклон_град': round(float(slope[sl][sub].max()), 1),
                      'водосбор_га': round(float(acc[sl][sub].max()), 1),
                      'овраг': bank >= gully_bank_deg})
    forms.sort(key=lambda f: -f['га'])

    inside = slope[msk]
    stats = {'перепад_м': round(float(z[msk].max() - z[msk].min()), 1),
             'уклон_медиана': round(float(np.median(inside)), 1),
             'уклон_p90': round(float(np.percentile(inside, 90)), 1),
             'уклон_макс': round(float(inside.max()), 1),
             'tpi_мин': round(float(tpi[msk].min()), 2),
             'tpi_макс': round(float(tpi[msk].max()), 2),
             'водосбор_макс_га': round(float(acc[msk].max()), 1),
             'форм_найдено': len(forms), 'оврагов': sum(1 for f in forms if f['овраг']),
             'исключается_га': round(sum(f['га'] for f in forms if f['овраг']), 2)}

    gul = np.isin(lb, [f['id'] for f in forms if f['овраг']])
    full = np.zeros(mask.shape, bool)
    up = np.repeat(np.repeat(gul, k, 0), k, 1)
    full[:up.shape[0], :up.shape[1]] = up
    full &= mask
    if full.any():
        full = ndimage.binary_closing(full, disk(max(1, int(8 / mpp))))
        full = ndimage.binary_opening(full, disk(max(1, int(5 / mpp)))) & mask
    return {'stats': stats, 'формы': forms, 'маска': full,
            'z': z, 'slope': slope, 'tpi': tpi, 'acc': acc, 'msk': msk, 'grid_m': grid_m}
