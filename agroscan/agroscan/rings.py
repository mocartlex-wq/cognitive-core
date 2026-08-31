# -*- coding: utf-8 -*-
"""Растр → полигоны: генерализация, векторизация, площади.

Собрано из копий, разъехавшихся по kpt1173/ и zu74/: disk() был скопирован
в 12 файлов, dp() — в 4. Здесь один экземпляр каждой функции.
"""
import numpy as np
from scipy import ndimage
from skimage import measure

def disk(r):
    """Круглый структурный элемент радиуса r пикселей."""
    y, x = np.mgrid[-r:r + 1, -r:r + 1]
    return x * x + y * y <= r * r

def dp(p, eps):
    """Дуглас-Пейкер для незамкнутой ломаной; eps в единицах координат p."""
    if len(p) < 5:
        return p
    keep = np.zeros(len(p), bool); keep[0] = keep[-1] = True
    st = [(0, len(p) - 1)]
    while st:
        a, b = st.pop()
        if b - a < 2:
            continue
        A, B = p[a], p[b]
        d = np.abs((p[a + 1:b, 0] - A[0]) * (B[1] - A[1])
                   - (p[a + 1:b, 1] - A[1]) * (B[0] - A[0])) / (np.hypot(*(B - A)) + 1e-9)
        if len(d) == 0:
            continue
        i = int(d.argmax())
        if d[i] > eps:
            keep[a + 1 + i] = True
            st.append((a, a + 1 + i)); st.append((a + 1 + i, b))
    return p[keep]

def dpc(p, eps):
    """То же для замкнутого контура: режем в самой дальней от старта точке,
    иначе упрощение «съедает» участок вокруг первой вершины."""
    if len(p) > 1 and np.allclose(p[0], p[-1]):
        p = p[:-1]
    if len(p) < 5:
        return p
    far = int(np.hypot(*(p - p[0]).T).argmax())
    return np.vstack([dp(p[:far + 1], eps)[:-1], dp(np.vstack([p[far:], p[:1]]), eps)[:-1]])

def ring_area(r):
    """Площадь кольца по формуле Гаусса, га (знак отброшен)."""
    a = np.asarray(r, float)
    return abs(float(np.sum(a[:, 0] * np.roll(a[:, 1], -1)
                            - np.roll(a[:, 0], -1) * a[:, 1]) / 2)) / 10000

def drop_small(m, cellHa, minha):
    """Растворить ареалы мельче minha в окружающем классе."""
    lb, n = ndimage.label(m)
    if not n:
        return m
    sz = ndimage.sum(m, lb, range(1, n + 1)) * cellHa
    return np.isin(lb, [i + 1 for i, s in enumerate(sz) if s >= minha])

def vectorize(m, grid, eps=3.0, minha=0.10):
    """Бинарная маска → (внешние кольца, вырезы, площадь га) в местной СК.

    eps в метрах; знак площади кольца различает контур и вырез.
    """
    M = grid.M; mpp = M['mpp']
    m = drop_small(m, mpp * mpp / 10000, minha)
    outer, inner = [], []
    for c in measure.find_contours(m.astype(float), 0.5):
        xy = dpc(np.stack([c[:, 1], c[:, 0]], 1), eps / mpp)
        if len(xy) < 4:
            continue
        ring = [list(grid.xy(x, y)) for x, y in xy]
        s = sum(ring[i][0] * ring[(i + 1) % len(ring)][1]
                - ring[(i + 1) % len(ring)][0] * ring[i][1] for i in range(len(ring))) / 2
        (outer if s < 0 else inner).append(ring)
    area = sum(ring_area(r) for r in outer) - sum(ring_area(r) for r in inner)
    return outer, inner, area

def rasterize(rings, grid, holes=None):
    """Кольца местной СК → бинарная маска в сетке снимка (чётно-нечётное правило)."""
    from PIL import Image, ImageDraw
    M = grid.M
    im = Image.new('L', (M['W'], M['H']), 0); d = ImageDraw.Draw(im)
    for r in rings:
        d.polygon([grid.px(*p) for p in r], fill=255)
    for h in (holes or []):
        d.polygon([grid.px(*p) for p in h], fill=0)
    return np.array(im) > 127
