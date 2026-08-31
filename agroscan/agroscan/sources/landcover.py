# -*- coding: utf-8 -*-
"""Независимые наборы покрова: Hansen GFC и ESA WorldCover.

Нужны как контроль своей классификации: совпадение площади древесной
растительности с чужим алгоритмом — единственная проверка, которая не
зависит от моих порогов. Имена тайлов считаются из координат: Hansen
режет мир на квадраты 10° по верхнему левому углу, WorldCover — на 3°
по нижнему левому.
"""
import math
import os

HANSEN = ('https://storage.googleapis.com/earthenginepartners-hansen/'
          'GFC-2023-v1.11/Hansen_GFC-2023-v1.11_%s_%s.tif')
WORLDCOVER = ('https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map/'
              'ESA_WorldCover_10m_2021_v200_%s_Map.tif')

def hansen_tile(lat, lon):
    top = int(math.ceil(lat / 10.0) * 10)
    left = int(math.floor(lon / 10.0) * 10)
    return '%02d%s_%03d%s' % (abs(top), 'N' if top >= 0 else 'S',
                              abs(left), 'E' if left >= 0 else 'W')

def worldcover_tile(lat, lon):
    bot = int(math.floor(lat / 3.0) * 3)
    left = int(math.floor(lon / 3.0) * 3)
    return '%s%02d%s%03d' % ('N' if bot >= 0 else 'S', abs(bot),
                             'E' if left >= 0 else 'W', abs(left))

def sample(grid, layers=('treecover2000', 'lossyear', 'gain'), worldcover=True):
    """{имя: массив} на сетке участка. Отсутствующий слой просто не возвращается."""
    os.environ.setdefault('GDAL_DISABLE_READDIR_ON_OPEN', 'EMPTY_DIR')
    import numpy as np
    lat = float(np.nanmedian(grid.lat)); lon = float(np.nanmedian(grid.lon))
    ht = hansen_tile(lat, lon); wt = worldcover_tile(lat, lon)
    out, names = {}, {'тайл_hansen': ht, 'тайл_worldcover': wt}
    for lay in layers:
        try:
            out['hansen_' + lay] = grid.sample('/vsicurl/' + HANSEN % (lay, ht))
        except Exception:
            pass
    if worldcover:
        try:
            out['worldcover'] = grid.sample('/vsicurl/' + WORLDCOVER % wt)
        except Exception:
            pass
    return out, names
