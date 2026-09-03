# -*- coding: utf-8 -*-
"""Снимок Google как ещё одна подложка для анализа.

Что у Google можно взять, а что нельзя — важно понимать до того, как ждать
результата:

* **текущий спутниковый снимок** отдаёт Map Tiles API (Google Maps Platform)
  по ключу владельца — он и подключён здесь;
* **историческая съёмка** (та самая шкала времени в Google Планете) наружу
  не отдаётся вообще: она есть только в настольной Google Earth Pro, API к
  ней нет ни в Maps Platform, ни в Earth Engine. Роль архива у нас играет
  ESRI Wayback (sources/wayback.py) — там датированные срезы с 2014 года;
* Earth Engine отдаёт Landsat и Sentinel, а их конвейер и так считает сам.

Ключ вводит владелец: переменная окружения AGROSCAN_GOOGLE_KEY (принимается
и GOOGLE_MAPS_API_KEY). Без ключа модуль молча выключен — конвейер работает
как раньше, а в отчёте появляется строка о том, почему слоя нет.
"""
import json
import os
import time

from . import basemap

SESSION_URL = 'https://tile.googleapis.com/v1/createSession?key=%s'
TILE_TPL = 'https://tile.googleapis.com/v1/2dtiles/{z}/{x}/{y}?session=%s&key=%s'
_SESSION = {}          # ключ → (токен, когда истекает)

def api_key():
    for name in ('AGROSCAN_GOOGLE_KEY', 'GOOGLE_MAPS_API_KEY'):
        v = (os.environ.get(name) or '').strip()
        if v:
            return v
    return None

def _post(url, body, timeout=40):
    import subprocess
    cmd = ['curl', '-sS', '--max-time', str(timeout), '-X', 'POST', url,
           '-H', 'Content-Type: application/json', '-d', json.dumps(body)]
    ca = '/root/.ccr/ca-bundle.crt'
    if os.path.exists(ca):
        cmd[1:1] = ['--cacert', ca]
    r = subprocess.run(cmd, capture_output=True)
    try:
        return json.loads(r.stdout.decode('utf-8', 'replace'))
    except Exception:
        return None

def session(map_type='satellite'):
    """Токен сессии Map Tiles API. None — ключа нет или Google отказал."""
    k = api_key()
    if not k:
        return None, 'ключ не задан (AGROSCAN_GOOGLE_KEY)'
    cached = _SESSION.get((k, map_type))
    if cached and cached[1] > time.time() + 60:
        return cached[0], ''
    r = _post(SESSION_URL % k, {'mapType': map_type, 'language': 'ru-RU', 'region': 'RU'})
    if not r or 'session' not in r:
        err = ((r or {}).get('error') or {}).get('message') or 'ответ без session'
        return None, 'Google отказал: %s' % err
    _SESSION[(k, map_type)] = (r['session'], float(r.get('expiry', time.time() + 3600)))
    return r['session'], ''

def tile_template(map_type='satellite'):
    s, err = session(map_type)
    return (TILE_TPL % (s, api_key()), '') if s else (None, err)

def fetch(bbox, loc, zoom=17, mpp=0.6, map_type='satellite', verbose=False):
    """Подложка Google в местной СК. Возвращает (изображение, meta, причина).

    При отсутствии ключа или отказе сервиса изображение — None, а причина
    печатается в лог и попадает в отчёт: молчаливого пропуска быть не должно.
    """
    tpl, err = tile_template(map_type)
    if not tpl:
        return None, None, err
    img, meta = basemap.fetch(bbox, loc, zoom=zoom, mpp=mpp, verbose=verbose, tpl=tpl)
    meta['layer'] = 'google_' + map_type
    return img, meta, ''
