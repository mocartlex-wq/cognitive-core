# -*- coding: utf-8 -*-
"""Ближайшие населённые пункты: название, расстояние, направление.

Схему смотрят отдельно от остального комплекта, и вопрос «где это вообще»
на ней ничем не закрыт: компас есть, привязки к местности нет. КПТ помогает
не всегда — `rel_position` заполнен у одного участка из трёх, а границы
населённых пунктов в выгрузке идут без названий.

Направление считается **от участка к селу**. В КПТ оно записано наоборот
(«участок находится в 4,9 км по направлению на юго-восток от ориентира»),
поэтому румб на схеме противоположен румбу из выписки — это надо подписывать,
иначе выглядит как противоречие.
"""
import json
import math
import os
import subprocess

from .. import cache as cache_mod

# POST через прокси висит до таймаута, GET отвечает; зеркала перебираем
# по очереди — kumi отдавал 504, private.coffee молчал, и наоборот.
MIRRORS = ('https://overpass.kumi.systems/api/interpreter',
           'https://maps.mail.ru/osm/tools/overpass/api/interpreter',
           'https://overpass-api.de/api/interpreter',
           'https://overpass.private.coffee/api/interpreter')
# overpass.osm.ch и подобные региональные выгрузки в список не берём: они
# отвечают 200 и пустым списком, и это неотличимо от «вокруг ничего нет»
QUERY = ('[out:json][timeout:25];'
         '(node(around:%d,%.5f,%.5f)["place"~"^(city|town|village|hamlet)$"];);'
         'out body 40;')
TYPE_RU = {'city': 'г.', 'town': 'пгт', 'village': 'с.', 'hamlet': 'д.'}
RHUMB = ('север', 'северо-восток', 'восток', 'юго-восток',
         'юг', 'юго-запад', 'запад', 'северо-запад')
SHORT = ('С', 'СВ', 'В', 'ЮВ', 'Ю', 'ЮЗ', 'З', 'СЗ')

def rhumb(az):
    """Азимут в градусах → румб словом («северо-запад»)."""
    return RHUMB[int((az % 360) / 45 + 0.5) % 8]

def short(az):
    return SHORT[int((az % 360) / 45 + 0.5) % 8]

def name_of(p):
    """«с. Новое Славкино» — сокращение по типу пункта."""
    return ('%s %s' % (TYPE_RU.get(p.get('тип'), ''), p['название'])).strip()

def geo(lon0, lat0, lon, lat):
    """Расстояние в километрах и азимут от точки к точке (плоское приближение)."""
    x = (lon - lon0) * math.cos(math.radians(lat0)) * 111.320
    y = (lat - lat0) * 110.574
    return math.hypot(x, y), math.degrees(math.atan2(x, y)) % 360

def _get(url, data, timeout=60):
    cmd = ['curl', '-sS', '--max-time', str(timeout), '-G',
           '--data-urlencode', 'data=' + data, url]
    ca = '/root/.ccr/ca-bundle.crt'
    if os.path.exists(ca):
        cmd[1:1] = ['--cacert', ca]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0 or not r.stdout:
        return None
    try:
        return json.loads(r.stdout.decode('utf-8', 'replace'))
    except ValueError:
        return None                      # 504 отдаётся страницей, а не JSON

def nearest(lon, lat, radius_m=15000, no_cache=False, verbose=False):
    """Населённые пункты вокруг точки, ближайший первым.

    Возвращает [{'название','тип','lon','lat','км','азимут'}]. Нет сети или
    в радиусе пусто — пустой список: лист собирается без подписи, а причина
    видна в логе.
    """
    # ключ огрублён до сотых градуса (~1 км): центр габарита участка и центр
    # кадра снимка отличаются на десятки метров, а ответ для них один и тот же
    k = cache_mod.key('places', lon=round(lon, 2), lat=round(lat, 2), r=radius_m)
    raw = None if no_cache else cache_mod.get_json(k)
    if raw is None:
        # зеркала перегружены неровно: то одно отдаёт 504, то другое молчит,
        # поэтому обходим список дважды, прежде чем сдаться
        for url in MIRRORS * 2:
            raw = _get(url, QUERY % (radius_m, lat, lon))
            # пустой ответ не кэшируем и не считаем ответом: у зеркала может
            # не быть этого региона, а пустота молча превратится в «нет сёл»
            if raw and raw.get('elements'):
                cache_mod.put_json(k, raw)
                break
            if verbose:
                print('   населённые пункты: %s не ответил' % url.split('/')[2], flush=True)
        else:
            return []
    out = []
    for e in raw.get('elements', []):
        t = e.get('tags') or {}
        if not t.get('name'):
            continue
        km, az = geo(lon, lat, e['lon'], e['lat'])
        out.append({'название': t['name'], 'тип': t.get('place'),
                    'lon': e['lon'], 'lat': e['lat'],
                    'км': round(km, 2), 'азимут': round(az)})
    return sorted(out, key=lambda p: p['км'])

def line(p, full=True):
    """Строка для листа: «с. Новое Славкино — 4,8 км на северо-запад»."""
    if not p:
        return ''
    km = ('%.1f' % p['км']).replace('.', ',')
    return '%s — %s км%s' % (name_of(p), km,
                             ' на ' + rhumb(p['азимут']) if full else '')
