# -*- coding: utf-8 -*-
"""Ряд Landsat через ESRI ImageServer — единственный доступ к 1970-м.

Sentinel-2 начинается с 2015 года, а год выбытия участка из оборота
приходится определять по снимкам с 1977-го. Каталог ESRI отдаёт готовый
NDVI по всем поколениям (MSS, TM, ETM+, OLI) — собственная калибровка
сенсоров была бы отдельной работой несопоставимого объёма.

Привязка КАЖДОГО снимка читается из полученного GeoTIFF: ImageServer
расширяет запрошенный bbox под соотношение сторон size, и доверие к
запросу однажды увело контур на 600 м.
"""
import datetime
import json
import os
import subprocess
import urllib.parse

BASE = 'https://landsat2.arcgis.com/arcgis/rest/services/Landsat/MS/ImageServer/exportImage'
SENTINEL = 'https://sentinel.arcgis.com/arcgis/rest/services/Sentinel2/ImageServer/exportImage'

def _ms(y, m, d):
    return int(datetime.datetime(y, m, d, tzinfo=datetime.timezone.utc).timestamp() * 1000)

def _fetch(url, path, timeout=200, min_bytes=50000):
    if os.path.exists(path) and os.path.getsize(path) > min_bytes:
        return 'кэш'
    cmd = ['curl', '-sS', '--max-time', str(timeout), '-A', 'Mozilla/5.0', '-o', path]
    ca = '/root/.ccr/ca-bundle.crt'
    if os.path.exists(ca):
        cmd[1:1] = ['--cacert', ca]
    subprocess.run(cmd + [url], check=True)
    if not os.path.exists(path) or os.path.getsize(path) < min_bytes:
        if os.path.exists(path):
            os.remove(path)
        raise RuntimeError('пустой ответ ImageServer')
    return '%d КБ' % (os.path.getsize(path) // 1024)

def scene(bbox, t0, t1, path, rule='NDVI Raw', size=900, base=BASE):
    """Одна сцена за окно [t0, t1] — наименее облачная. bbox в WGS84."""
    p = {'bbox': '%s,%s,%s,%s' % tuple(bbox), 'bboxSR': '4326', 'imageSR': '4326',
         'size': '%d,%d' % (size, size), 'format': 'tiff', 'f': 'image',
         'time': '%d,%d' % (t0, t1),
         'renderingRule': json.dumps({'rasterFunction': rule}),
         'mosaicRule': json.dumps({'mosaicMethod': 'esriMosaicAttribute',
                                   'sortField': 'cloudcover', 'sortValue': '0',
                                   'ascending': True})}
    return _fetch(base + '?' + urllib.parse.urlencode(p), path)

def ndvi_series(grid, years, cache_dir, month0=6, day0=10, month1=8, day1=31,
                size=900, verbose=True):
    """{год: массив NDVI на сетке участка} за летнее окно каждого года."""
    import numpy as np
    os.makedirs(cache_dir, exist_ok=True)
    bbox = (round(float(grid.lon.min()), 6), round(float(grid.lat.min()), 6),
            round(float(grid.lon.max()), 6), round(float(grid.lat.max()), 6))
    out, meta = {}, {}
    for y in years:
        fn = os.path.join(cache_dir, 'ls_ndvi_%d.tif' % y)
        try:
            st = scene(bbox, _ms(y, month0, day0), _ms(y, month1, day1), fn, size=size)
            a = grid.sample(fn)                      # привязка — из файла
            if not np.isfinite(a).any():
                raise RuntimeError('нет данных в кадре')
            out[y] = a; meta[y] = st
        except Exception as e:
            if verbose:
                print('   %d — %s' % (y, str(e)[:60]))
    return out, meta
