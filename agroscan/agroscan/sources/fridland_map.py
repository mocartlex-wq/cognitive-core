# -*- coding: utf-8 -*-
"""Тип почвы по Почвенной карте РСФСР 1:2 500 000 (Фридланд и др., 1988).

Карта опубликована на soil-db.ru. Открытого API у неё нет, но её же
клиент ходит за данными обычным способом: страница отдаёт csrf-токен и
снимок Livewire-компонента, после чего `loadFeatures` возвращает контуры
в габарите как обычный GeoJSON, а `showFeatureData` — их атрибуты.
Замер: узкий габарит вокруг участка отвечает за две секунды.

Это разбор клиентского протокола чужого сайта — он может смениться в
любой день. Поэтому любая осечка гасится (возвращается None), а в конфиге
участка остаётся поле `soil_map.индекс` с приоритетом над автоматикой.
"""
import html as _html
import json
import os
import re
import ssl
import urllib.request
import http.cookiejar

BASE = 'https://soil-db.ru'
SOURCE = 'Почвенная карта РСФСР 1:2 500 000 (Фридланд и др., 1988), soil-db.ru'
UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36'
CA = '/root/.ccr/ca-bundle.crt'

def _opener():
    ctx = ssl.create_default_context(cafile=CA if os.path.exists(CA) else None)
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ctx),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

def _clean(v):
    """Значение из таблицы сайта: снимаем разметку, схлопываем пробелы."""
    if not isinstance(v, str):
        return v
    t = re.sub(r'<br\s*/?>', ' · ', v)
    t = _html.unescape(re.sub(r'<[^>]+>', '', t))
    return re.sub(r'\s+', ' ', t).strip()

def point_in(pt, geom):
    """Точка внутри полигона GeoJSON, с учётом вырезов."""
    def ring_hit(ring):
        x, y = pt
        ins = False
        n = len(ring)
        for i in range(n):
            x1, y1 = ring[i][0], ring[i][1]
            x2, y2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
            if (y1 > y) != (y2 > y) and x1 + (y - y1) * (x2 - x1) / (y2 - y1) > x:
                ins = not ins
        return ins
    polys = geom['coordinates'] if geom.get('type') == 'MultiPolygon' else [geom['coordinates']]
    for poly in polys:
        if poly and ring_hit(poly[0]) and not any(ring_hit(r) for r in poly[1:]):
            return True
    return False

def parse_rows(rows):
    """Таблица атрибутов контура → понятные поля.

    Значение основной почвы приходит как «117 — <strong>Ч<sup>в</sup></strong>
    <br />Черноземы выщелоченные»: из него нужны код, индекс и название.
    """
    out, extra = {}, []
    for r in rows or []:
        lab = _clean(r.get('label', ''))
        val = _clean(r.get('value', ''))
        if not isinstance(val, str) or not val or val == '—':
            continue          # «Id» приходит числом, прочерк — пустой ячейкой
        m = re.match(r'^(\d+)\s*—\s*(\S+)\s*·\s*(.+)$', val)
        parsed = {'код': int(m.group(1)), 'индекс': m.group(2).lower(),
                  'название': m.group(3)} if m else None
        if lab.startswith('Почва') and 'основная' in lab and parsed:
            out.update(parsed)
        elif lab.startswith('Почва') and parsed:
            extra.append('%s (%s)' % (parsed['название'], parsed['индекс']))
        elif lab.startswith('Порода') and 'основная' in lab:
            out['порода'] = val
        elif lab.startswith('Площадь'):
            try:
                out['площадь_км2'] = round(float(val), 1)
            except ValueError:
                pass
    if extra:
        out['сопутствующие'] = extra
    return out

def _dispatch(resp, name):
    for c in resp.get('components', []):
        for d in c.get('effects', {}).get('dispatches', []) or []:
            if d.get('name') == name:
                return d.get('params') or {}
    return {}

def lookup(lon, lat, delta=0.01, zoom=14, timeout=120):
    """Контур карты под точкой: индекс, название, сопутствующие, порода.

    Возвращает None, если сайт не ответил, протокол сменился или точка вне
    покрытия карты — вызывающий код обязан это пережить.
    """
    try:
        op = _opener()
        url = '%s/map?lat=%.4f&lng=%.4f&zoom=%d' % (BASE, lat, lon, zoom)
        page = op.open(urllib.request.Request(url, headers={'User-Agent': UA}),
                       timeout=timeout).read().decode('utf-8', 'replace')
        tok = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)
        snap = _html.unescape(re.findall(r'wire:snapshot="([^"]+)"', page)[0])

        def call(method, params):
            body = json.dumps({'_token': tok, 'components': [
                {'snapshot': snap, 'updates': {},
                 'calls': [{'path': '', 'method': method, 'params': params}]}]}).encode()
            req = urllib.request.Request(BASE + '/livewire/update', data=body, headers={
                'User-Agent': UA, 'Content-Type': 'application/json', 'X-Livewire': '1',
                'X-CSRF-TOKEN': tok, 'Referer': url})
            return json.loads(op.open(req, timeout=timeout).read().decode())

        got = call('loadFeatures', [
            {'_southWest': {'lat': lat - delta, 'lng': lon - delta},
             '_northEast': {'lat': lat + delta, 'lng': lon + delta}},
            {'lat': lat, 'lng': lon}, zoom])
        feats = _dispatch(got, 'livewire-map:add-features').get('features') or []
        hit = next((f for f in feats if point_in((lon, lat), f['geometry'])), None)
        if not hit:
            return None
        fid = (hit.get('properties') or {}).get('id') or hit.get('id')
        data = _dispatch(call('showFeatureData', [fid]), 'livewire-map:show-feature-data')
        res = parse_rows((data.get('feature') or {}).get('data'))
        if not res.get('название'):
            return None
        res.update({'id': fid, 'источник': SOURCE,
                    'ссылка': '%s/map?lat=%.4f&lng=%.4f&zoom=13&feature=%s'
                              % (BASE, lat, lon, fid)})
        return res
    except Exception:
        return None
