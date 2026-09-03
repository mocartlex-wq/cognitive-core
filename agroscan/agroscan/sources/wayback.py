# -*- coding: utf-8 -*-
"""Архив снимков ESRI Wayback: то же, что временная шкала в Google Планете.

У Google историческая съёмка живёт только в настольной Google Earth Pro и
наружу никаким интерфейсом не отдаётся (Maps Platform отдаёт текущий снимок,
Earth Engine — чужие спутники, но не съёмку Google). Wayback — открытый
архив World Imagery: около двухсот датированных срезов с 2014 года, ключей
не требует, тайлы те же, что у рабочей подложки.

Срезов много, но на конкретный участок съёмка обновлялась считаные разы:
чтобы не показывать девяносто одинаковых картинок, версии просеиваются по
одному тайлу — остаются только те, где снимок действительно менялся.
"""
import concurrent.futures
import hashlib
import math
import re

from .. import cache as cache_mod
from . import basemap

CONFIG = 'https://s3-us-west-2.amazonaws.com/config.maptiles.arcgis.com/waybackconfig.json'
DATE_RE = re.compile(r'(\d{4})-(\d{2})-(\d{2})')

def _tile(lon, lat, z):
    n = 2 ** z
    return (int((lon + 180) / 360 * n),
            int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n))

def _tpl(url):
    """Шаблон Wayback → шаблон basemap.fetch."""
    return url.replace('{level}', '{z}').replace('{row}', '{y}').replace('{col}', '{x}')

def releases():
    """Все срезы архива: [(дата, шаблон тайлов)], новые в конце."""
    k = cache_mod.key('wayback_config')
    cfg = cache_mod.get_json(k)
    if cfg is None:
        raw = basemap._get(CONFIG, timeout=60)
        if not raw:
            return []
        import json
        cfg = json.loads(raw)
        cache_mod.put_json(k, cfg)
    out = []
    for v in cfg.values():
        m = DATE_RE.search(v.get('itemTitle', ''))
        if m:
            out.append((m.group(0), _tpl(v['itemURL'])))
    return sorted(out)

def versions(lon, lat, zoom=16, workers=12, verbose=False):
    """Срезы, на которых снимок этой точки менялся: [(дата, шаблон)].

    Проверяется один тайл на срез: две сотни лёгких запросов вместо двух
    сотен подложек. Результат кэшируется по тайлу — соседние участки
    квартала переиспользуют его целиком.
    """
    rel = releases()
    if not rel:
        return []
    x, y = _tile(lon, lat, zoom)
    k = cache_mod.key('wayback_versions', z=zoom, x=x, y=y, n=len(rel))
    dates = cache_mod.get_json(k)
    if dates is None:
        hs = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(basemap._get, t.format(z=zoom, x=x, y=y)): d for d, t in rel}
            for f in concurrent.futures.as_completed(futs):
                b = f.result()
                hs[futs[f]] = hashlib.md5(b).hexdigest() if b else None
        dates, seen = [], None
        for d, _ in rel:
            h = hs.get(d)
            if h and h != seen:
                dates.append(d); seen = h
        cache_mod.put_json(k, dates)
    if verbose:
        print('   архив Wayback: срезов %d, съёмка менялась %d раз' % (len(rel), len(dates)),
              flush=True)
    tpl = dict(rel)
    return [(d, tpl[d]) for d in dates if d in tpl]

def snapshot(bbox, loc, tpl, zoom=17, mpp=0.6, verbose=False):
    """Подложка одного среза архива в местной СК участка."""
    return basemap.fetch(bbox, loc, zoom=zoom, mpp=mpp, verbose=verbose, tpl=tpl)
