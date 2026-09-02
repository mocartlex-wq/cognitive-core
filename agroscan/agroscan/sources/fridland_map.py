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
import math
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

class _Session:
    """Одна сессия с картой: страница даёт токен и снимок компонента.

    Дальше все вызовы идут в /livewire/update — так же, как их делает
    собственный клиент сайта.
    """

    def __init__(self, lon, lat, zoom=14, timeout=120):
        self.timeout = timeout
        self.op = _opener()
        self.url = '%s/map?lat=%.4f&lng=%.4f&zoom=%d' % (BASE, lat, lon, zoom)
        page = self.op.open(urllib.request.Request(self.url, headers={'User-Agent': UA}),
                            timeout=timeout).read().decode('utf-8', 'replace')
        self.tok = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)
        self.snap = _html.unescape(re.findall(r'wire:snapshot="([^"]+)"', page)[0])

    def call(self, method, params=None):
        body = json.dumps({'_token': self.tok, 'components': [
            {'snapshot': self.snap, 'updates': {},
             'calls': [{'path': '', 'method': method, 'params': params or []}]}]}).encode()
        req = urllib.request.Request(BASE + '/livewire/update', data=body, headers={
            'User-Agent': UA, 'Content-Type': 'application/json', 'X-Livewire': '1',
            'X-CSRF-TOKEN': self.tok, 'Referer': self.url})
        return json.loads(self.op.open(req, timeout=self.timeout).read().decode())

    def load(self, lon, lat, delta, zoom):
        return self.call('loadFeatures', [
            {'_southWest': {'lat': lat - delta, 'lng': lon - delta},
             '_northEast': {'lat': lat + delta, 'lng': lon + delta}},
            {'lat': lat, 'lng': lon}, zoom])

def index_of(title):
    """«Ч<sup>в</sup>» → «чв»: индекс в том виде, в каком он в легенде."""
    return re.sub(r'<[^>]+>', '', _html.unescape(title or '')).strip().lower()

def km_between(lat1, lon1, lat2, lon2):
    """Расстояние по земле, км — на таких дистанциях плоскости достаточно."""
    return math.hypot((lat1 - lat2) * 111.3,
                      (lon1 - lon2) * 111.3 * math.cos(math.radians((lat1 + lat2) / 2)))

def lookup(lon, lat, delta=0.01, zoom=14, timeout=120):
    """Контур карты под точкой: индекс, название, сопутствующие, порода.

    Возвращает None, если сайт не ответил, протокол сменился или точка вне
    покрытия карты — вызывающий код обязан это пережить.
    """
    try:
        s = _Session(lon, lat, zoom, timeout)
        feats = _dispatch(s.load(lon, lat, delta, zoom),
                          'livewire-map:add-features').get('features') or []
        hit = next((f for f in feats if point_in((lon, lat), f['geometry'])), None)
        if not hit:
            return None
        fid = (hit.get('properties') or {}).get('id') or hit.get('id')
        data = _dispatch(s.call('showFeatureData', [fid]), 'livewire-map:show-feature-data')
        res = parse_rows((data.get('feature') or {}).get('data'))
        if not res.get('название'):
            return None
        res.update({'id': fid, 'источник': SOURCE,
                    'ссылка': '%s/map?lat=%.4f&lng=%.4f&zoom=13&feature=%s'
                              % (BASE, lat, lon, fid)})
        return res
    except Exception:
        return None

def contours(lon, lat, delta=0.16, zoom=11, timeout=180):
    """Контуры карты в габарите — для обзорной карты в записке.

    Отдаёт геометрию, индекс и цвета самой карты, чтобы лист выглядел
    так же, как источник, и специалист узнавал раскраску.
    """
    try:
        s = _Session(lon, lat, zoom, timeout)
        feats = _dispatch(s.load(lon, lat, delta, zoom),
                          'livewire-map:add-features').get('features') or []
    except Exception:
        return []
    out = []
    for f in feats:
        pr = f.get('properties') or {}
        col = (pr.get('colors') or {}).get('default') or {}
        out.append({'id': pr.get('id'), 'индекс': index_of(pr.get('title')),
                    'заливка': col.get('fill') or '#dddddd',
                    'обводка': col.get('stroke') or '#888888',
                    'geometry': f.get('geometry')})
    return out

def profiles(lon, lat, delta=1.0, zoom=9, timeout=240):
    """Полевые разрезы в габарите, отсортированные по удалению от точки.

    Координаты в базе округлены до 0,01° — это ±1 км, и дальше эта
    погрешность обязана быть видна в отчёте.
    """
    try:
        s = _Session(lon, lat, zoom, timeout)
        got = _dispatch(s.load(lon, lat, delta, zoom), 'livewire-map:add-profiles')
    except Exception:
        return []
    out = []
    for p in got.get('profiles') or []:
        try:
            a, b = float(p['latitude']), float(p['longitude'])
        except (KeyError, TypeError, ValueError):
            continue
        out.append({'id': p.get('id'), 'lat': a, 'lon': b,
                    'км': round(km_between(lat, lon, a, b), 1)})
    return sorted(out, key=lambda x: x['км'])

PROFILE_FIELDS = {'Название почвы по ПК РФ': 'тип',
                  'Название почвы по WRB 2006': 'wrb',
                  'Генетический тип почвообразующей породы': 'порода',
                  'Хозяйственное использование': 'использование',
                  'Источник данных': 'источник',
                  'Код разреза': 'код',
                  'Административный регион РФ': 'регион'}

def parse_profile(tables):
    """Карточка разреза → поля, которые читает агроном."""
    out = {}
    rows = (tables or [{}])[0].get('data') or []
    for r in rows:
        if len(r) >= 2 and r[0] in PROFILE_FIELDS:
            v = _clean(r[1])
            if v and v != 'не указано':
                out[PROFILE_FIELDS[r[0]]] = v
    return out

def profile(pid, lon=45.0, lat=53.0, timeout=180):
    """Данные разреза по его номеру. None — если не отдался."""
    try:
        s = _Session(lon, lat, 10, timeout)
        d = _dispatch(s.call('showPointData', [pid]), 'livewire-map:show-feature-data')
        f = d.get('feature') or {}
        res = parse_profile(((f.get('data') or {}).get('tables')))
        if not res:
            return None
        res['id'] = pid
        res['ссылка'] = '%s/map?lat=%.4f&lng=%.4f&zoom=12&profile=%s' % (BASE, lat, lon, pid)
        return res
    except Exception:
        return None
