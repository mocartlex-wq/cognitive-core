# -*- coding: utf-8 -*-
"""Карта высот полога Meta/WRI (Canopy Height, ~1 м).

Ради чего: на ортофото взрослая полезащитная полоса и молодой самосев одного
цвета — по RGB их не разделить, шесть попыток это подтвердили. Высота
разделяет их тривиально: полоса 12-20 м, самосев 2-6 м.

Данные глобальные, лежат тайлами COG на S3. Тайл 65536² пикселей, поэтому
читаем через /vsicurl окном — целиком он не качается.
"""
import json
import os
import subprocess

BASE = 'https://dataforgood-fb-data.s3.amazonaws.com/forests/v1/alsgedi_global_v6_float'
INDEX = BASE + '/tiles.geojson'

def _cache_dir():
    d = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'cache')
    os.makedirs(d, exist_ok=True)
    return d

def _fetch(url, path, timeout=180):
    if os.path.exists(path) and os.path.getsize(path) > 1000:
        return path
    cmd = ['curl', '-sS', '--max-time', str(timeout), '-o', path]
    ca = '/root/.ccr/ca-bundle.crt'
    if os.path.exists(ca):
        cmd += ['--cacert', ca]
    subprocess.run(cmd + [url], check=True)
    return path

def tiles_for(lon, lat):
    """Имена тайлов, покрывающих точку. Индекс 15 МБ, кэшируется."""
    idx = _fetch(INDEX, os.path.join(_cache_dir(), 'chm_tiles.geojson'))
    d = json.load(open(idx))
    out = []
    for f in d['features']:
        g = f.get('geometry')
        if not g:
            continue
        for ring in g['coordinates']:
            pts = ring[0] if isinstance(ring[0][0], (list, tuple)) else ring
            xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
            if min(xs) <= lon <= max(xs) and min(ys) <= lat <= max(ys):
                out.append(f['properties']['tile']); break
    return out

def url_for(lon, lat):
    """URL для чтения через rasterio (/vsicurl). None, если покрытия нет."""
    t = tiles_for(lon, lat)
    return '/vsicurl/%s/chm/%s.tif' % (BASE, t[0]) if t else None

def sample(grid):
    """Высота полога в метрах на сетке участка. NaN там, где нет покрытия."""
    import numpy as np
    lon = float(np.nanmedian(grid.lon)); lat = float(np.nanmedian(grid.lat))
    u = url_for(lon, lat)
    if u is None:
        return None
    os.environ.setdefault('GDAL_DISABLE_READDIR_ON_OPEN', 'EMPTY_DIR')
    h = grid.sample(u)
    h[h > 100] = np.nan          # 255 — служебное значение, не высота
    return h
