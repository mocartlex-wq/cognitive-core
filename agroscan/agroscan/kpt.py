# -*- coding: utf-8 -*-
"""Кадастровый план территории: участки, зоны, система координат.

Разбор потоковый: КПТ на квартал — это 30+ МБ XML, и класть его в память
целиком незачем. Прежний парсер (34 строки регулярок) доставал только
контуры участков; здесь берутся ещё площадь по ЕГРН для контроля, категория
земель, тип зоны и идентификатор местной СК — то, что раньше приходилось
задавать руками в конфиге.

Поддерживаются две схемы: КПТ (`land_record`) и выписка на участок
(`land_record` внутри `extract_base_params_land`).
"""
import re
import xml.etree.ElementTree as ET

# sk_id вида «58.2» — местная СК зона 2; реестр зон живёт в geo.MSK_ZONES
SK_RE = re.compile(r'^(\d+)\.(\d+)$')
ZOUIT_CODE = '6'          # зона с особыми условиями использования территории

def zone_of(sk_id, kn=None, rings=None, prefix='msk'):
    """«58.2» → «msk58-2».

    В выписках на участок вместо кода стоит текст «СК кадастрового округа»
    (проверено на vypiska74.xml), поэтому есть запасной путь: регион берётся
    из кадастрового номера, а номер зоны — из первой цифры восточной
    координаты (в МСК вынос на восток равен номеру зоны, умноженному
    на миллион).
    """
    m = SK_RE.match((sk_id or '').strip())
    if m:
        return '%s%s-%s' % (prefix, m.group(1), m.group(2))
    reg = kn.split(':')[0].lstrip('0') if kn and ':' in kn else None
    zn = None
    if rings:
        for r in rings:
            if r:
                zn = int(abs(r[0][0]) // 1e6) or None
                break
    return '%s%s-%d' % (prefix, reg, zn) if reg and zn else (sk_id or None)

def _text(el, *path):
    for p in path:
        node = el.find('.//' + p)
        if node is not None and node.text:
            return node.text.strip()
    return None

def _contours(el):
    """Все контуры записи в порядке следования; каждый — список [E, N].

    В кадастровых XML x — север, y — восток; наружу отдаём привычные [E, N].
    Внутренние контуры (вырезы) идут отдельными spatial_element, поэтому
    сохраняем их как есть, а разделение внешний/вырез делает zones.parcel_poly.
    """
    out = []
    for sp in el.iter('spatial_element'):
        ring = []
        for o in sp.iter('ordinate'):
            x = o.find('x'); y = o.find('y')
            if x is None or y is None or not x.text or not y.text:
                continue
            ring.append([float(y.text), float(x.text)])
        if len(ring) >= 3:
            if ring[0] == ring[-1]:
                ring = ring[:-1]
            out.append(ring)
    return out

def _area(el):
    node = el.find('.//area/value')
    if node is None:
        node = el.find('.//area')
    try:
        return float(node.text)
    except Exception:
        return None

def parse(path, want=('land_record', 'zones_and_territories_record')):
    """Потоково разобрать КПТ. Возвращает (участки, зоны, sk_id большинства)."""
    parcels, zones = [], []
    sk_seen = {}
    for _, el in ET.iterparse(path, events=('end',)):
        tag = el.tag.split('}')[-1]
        if tag not in want:
            continue
        rings = _contours(el)
        sk = _text(el, 'sk_id')
        if sk:
            sk_seen[sk] = sk_seen.get(sk, 0) + 1
        if tag == 'land_record':
            kn = _text(el, 'cad_number')
            if kn and rings:
                parcels.append({'кн': kn, 'кольца': rings, 'площадь_егрн': _area(el),
                                'категория': _text(el, 'category/type/value'),
                                'разрешённое': _text(el, 'permitted_use/permitted_use_established/by_document'),
                                'sk_id': sk})
        else:
            code = _text(el, 'type_boundary/code')
            if rings:
                zones.append({'реестровый_номер': _text(el, 'reg_numb_border'),
                              'код': code, 'тип': _text(el, 'type_boundary/value'),
                              'наименование': _text(el, 'name_by_doc'),
                              'кольца': rings, 'sk_id': sk})
        el.clear()
    sk_main = max(sk_seen, key=sk_seen.get) if sk_seen else None
    return parcels, zones, sk_main

def find(parcels, kn):
    """Запись участка по кадастровому номеру."""
    for p in parcels:
        if p['кн'] == kn:
            return p
    return None

def ring_area(ring):
    """Площадь кольца по формуле Гаусса, м²."""
    s = 0.0
    for i in range(len(ring)):
        j = (i + 1) % len(ring)
        s += ring[i][0] * ring[j][1] - ring[j][0] * ring[i][1]
    return abs(s / 2)

def area_of(rings):
    """Площадь многоконтурной записи: крупнейшее кольцо минус вложенные."""
    if not rings:
        return 0.0
    areas = sorted((ring_area(r) for r in rings), reverse=True)
    return areas[0] - sum(areas[1:]) if len(areas) > 1 and areas[0] > 2 * sum(areas[1:]) \
        else sum(areas)

def neighbours(parcels, bbox, kn=None, margin=0.0):
    """Смежные участки, попадающие в габарит (E0, E1, N0, N1)."""
    e0, e1, n0, n1 = bbox
    e0 -= margin; e1 += margin; n0 -= margin; n1 += margin
    out = []
    for p in parcels:
        if kn and p['кн'] == kn:
            continue
        for r in p['кольца']:
            xs = [q[0] for q in r]; ys = [q[1] for q in r]
            if max(xs) >= e0 and min(xs) <= e1 and max(ys) >= n0 and min(ys) <= n1:
                out.append({'kn': p['кн'], 'rings': p['кольца']})
                break
    return out

def zouit(zones, codes=(ZOUIT_CODE,)):
    """Только зоны с особыми условиями: территориальные зоны и лесничества
    ограничивают другое и в площадь «мероприятия не проводятся» не входят."""
    return [z for z in zones if z['код'] in codes]
