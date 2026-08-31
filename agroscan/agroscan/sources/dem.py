# -*- coding: utf-8 -*-
"""Рельеф: Copernicus DEM GLO-30 (COG на S3).

Имя тайла вычисляется из координат, а не задаётся вручную: в прежних
скриптах оно было зашито под конкретный участок, и на соседнем районе
расчёт молча взял бы чужую территорию. Участок, пересекающий границу
градуса, собирается из нескольких тайлов.
"""
import math
import os

import numpy as np

BASE = 'https://copernicus-dem-30m.s3.amazonaws.com'

def tile_name(lat, lon):
    ns = 'N' if lat >= 0 else 'S'
    ew = 'E' if lon >= 0 else 'W'
    return 'Copernicus_DSM_COG_10_%s%02d_00_%s%03d_00_DEM' % (
        ns, int(math.floor(abs(lat))), ew, int(math.floor(abs(lon))))

def url_for(lat, lon):
    t = tile_name(lat, lon)
    return '/vsicurl/%s/%s/%s.tif' % (BASE, t, t)

def sample(grid):
    """Высота в метрах на сетке участка. Склеивает соседние тайлы по градусам."""
    os.environ.setdefault('GDAL_DISABLE_READDIR_ON_OPEN', 'EMPTY_DIR')
    lats = range(int(math.floor(grid.lat.min())), int(math.floor(grid.lat.max())) + 1)
    lons = range(int(math.floor(grid.lon.min())), int(math.floor(grid.lon.max())) + 1)
    out = np.full(grid.shape, np.nan, np.float32)
    used = []
    for la in lats:
        for lo in lons:
            try:
                a = grid.sample(url_for(la + 0.5, lo + 0.5))
            except Exception:
                continue
            m = np.isfinite(a) & ~np.isfinite(out)
            if m.any():
                out[m] = a[m]; used.append(tile_name(la + 0.5, lo + 0.5))
    return (None, []) if not used else (out, used)
