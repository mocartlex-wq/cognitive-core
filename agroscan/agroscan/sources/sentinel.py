# -*- coding: utf-8 -*-
"""Sentinel-2 L2A через каталог Element84 (STAC) — источник классификации.

Зачем вместо RGB-подложки: у Sentinel есть красный край (rededge1-3) и
коротковолновый ИК, по которым древесная растительность отделяется от травы
устойчивее, чем по признаку «зелёный ярче красного» на ортофото. Плюс
известна дата съёмки и есть маска качества SCL — можно строить сезонные
композиты и сравнивать годы, а не работать с мозаикой неизвестной давности.

Ассеты — COG на публичном S3, читаются окном через /vsicurl: скачивать
сцены целиком не требуется.
"""
import json
import os
import subprocess
import numpy as np

SEARCH = 'https://earth-search.aws.element84.com/v1/search'
# SCL: классы, которые нельзя брать в композит
SCL_BAD = {0, 1, 3, 8, 9, 10, 11}   # нет данных, дефект, тень, облака, сирус, снег

def search(bbox, start, end, max_cloud=25, limit=40):
    """Сцены за период. bbox — (lon0, lat0, lon1, lat1) WGS84."""
    q = {'collections': ['sentinel-2-l2a'], 'bbox': list(bbox),
         'datetime': '%sT00:00:00Z/%sT23:59:59Z' % (start, end), 'limit': limit,
         'query': {'eo:cloud_cover': {'lt': max_cloud}}}
    cmd = ['curl', '-sS', '--max-time', '90', '-X', 'POST', SEARCH,
           '-H', 'Content-Type: application/json', '-d', json.dumps(q)]
    ca = '/root/.ccr/ca-bundle.crt'
    if os.path.exists(ca):
        cmd[1:1] = ['--cacert', ca]
    d = json.loads(subprocess.run(cmd, capture_output=True, check=True).stdout)
    return sorted(d.get('features', []), key=lambda x: x['properties']['eo:cloud_cover'])

def composite(grid, scenes, bands=('red', 'green', 'blue', 'nir', 'rededge1',
                                   'swir16', 'swir22'),
              limit=8, verbose=True):
    """Медианный композит по сценам с маскированием по SCL.

    Медиана, а не среднее: одиночное необнаруженное облако сдвигает среднее,
    но не медиану. Возвращает {канал: массив в сетке grid}.
    """
    os.environ.setdefault('GDAL_DISABLE_READDIR_ON_OPEN', 'EMPTY_DIR')
    stack = {b: [] for b in bands}
    used = []
    for s in scenes[:limit]:
        a = s['assets']
        if 'scl' not in a or any(b not in a for b in bands):
            continue
        try:
            scl = grid.sample('/vsicurl/' + a['scl']['href'])
            good = ~np.isin(np.nan_to_num(scl, nan=0).astype(int), list(SCL_BAD))
            if good.mean() < 0.5:            # сцена накрыта облаком над участком
                continue
            for b in bands:
                v = grid.sample('/vsicurl/' + a[b]['href'])
                v[~good] = np.nan
                stack[b].append(v)
            used.append((s['id'], s['properties']['datetime'][:10],
                         round(s['properties']['eo:cloud_cover'], 1), round(100 * good.mean())))
        except Exception as e:
            if verbose:
                print('   пропуск %s: %s' % (s['id'], str(e)[:60]))
    if not used:
        return None, []
    with np.errstate(all='ignore'):
        out = {b: np.nanmedian(np.stack(stack[b]), 0) for b in bands}
    return out, used

def indices(c):
    """Вегетационные индексы из композита."""
    e = 1e-6
    ix = {}
    ix['ndvi'] = (c['nir'] - c['red']) / (c['nir'] + c['red'] + e)
    if 'rededge1' in c:
        ix['ndre'] = (c['nir'] - c['rededge1']) / (c['nir'] + c['rededge1'] + e)
    if 'swir16' in c:
        ix['ndmi'] = (c['nir'] - c['swir16']) / (c['nir'] + c['swir16'] + e)
    return ix
