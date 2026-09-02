# -*- coding: utf-8 -*-
"""Разбор ответа soil-db.ru: контур карты Фридланда под точкой участка.

Сеть здесь не нужна — проверяется то, что ломается молча: попадание точки
в полигон и разбор таблицы атрибутов, где значения приходят с разметкой.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agroscan.sources import fridland_map as fm

SQUARE = {'type': 'Polygon', 'coordinates': [
    [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],
    [[4, 4], [6, 4], [6, 6], [4, 6], [4, 4]]]}          # с вырезом

# фрагмент реального ответа showFeatureData для контура 22087 (участок 1173)
ROWS = [
    {'label': 'Id', 'value': 12121},
    {'label': 'Почва (почвенный комплекс) основная',
     'value': '117 — <strong>Ч<sup>в</sup></strong><br />Черноземы выщелоченные'},
    {'label': 'Почва (почвенный комплекс) сопутствующая 1',
     'value': '116 — <strong>Ч<sup>оп</sup></strong><br />Черноземы оподзоленные'},
    {'label': 'Почва (почвенный комплекс) сопутствующая 2', 'value': '—'},
    {'label': 'Порода основная', 'value': 'Среднесуглинистые'},
    {'label': 'Порода сопутствующая', 'value': '—'},
    {'label': 'Площадь, км<sup>2</sup>', 'value': '620.965'},
]

def test_point_in_polygon():
    assert fm.point_in((1, 1), SQUARE)
    assert not fm.point_in((11, 1), SQUARE), 'снаружи'
    assert not fm.point_in((5, 5), SQUARE), 'в вырезе контур не считается'

def test_point_in_multipolygon():
    multi = {'type': 'MultiPolygon', 'coordinates': [
        [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
        [[[5, 5], [7, 5], [7, 7], [5, 7], [5, 5]]]]}
    assert fm.point_in((6, 6), multi)
    assert not fm.point_in((3, 3), multi)

def test_rows_parsed():
    r = fm.parse_rows(ROWS)
    assert r['индекс'] == 'чв' and r['код'] == 117, r
    assert r['название'] == 'Черноземы выщелоченные', r
    assert r['порода'] == 'Среднесуглинистые', r
    assert r['площадь_км2'] == 621.0, r
    assert r['сопутствующие'] == ['Черноземы оподзоленные (чоп)'], r
    assert 'Id' not in str(r), 'служебные строки в результат не идут'

def test_empty_rows_give_nothing():
    assert fm.parse_rows([]) == {}
    assert fm.parse_rows([{'label': 'Почва основная', 'value': '—'}]) == {}

def test_lookup_survives_dead_host():
    """Сайт сменил протокол или не ответил — None, а не исключение."""
    base = fm.BASE
    try:
        fm.BASE = 'https://soil-db.invalid'
        assert fm.lookup(44.9, 53.125, timeout=5) is None
    finally:
        fm.BASE = base

# ── карточка разреза и обзорная карта ───────────────────────────────────
PROFILE_ROWS = [{'data': [
    ['Идентификатор разреза в БД', 757],
    ['Код разреза', 'Alb-594-423'],
    ['Источник данных', 'Лебедева И.И., Семина Е.В. Почвы лесостепи. — М.: Колос, 1974.'],
    ['Авторское название почвы', 'не указано'],
    ['Название почвы по ПК РФ', 'Серые лесные'],
    ['Название почвы по WRB 2006', 'Greyic Phaeozems Albic'],
    ['Административный регион РФ', 'Пензенская область'],
    ['Генетический тип почвообразующей породы', 'покровные суглинки'],
    ['Хозяйственное использование', 'пашня'],
]}]

def test_profile_card_parsed():
    r = fm.parse_profile(PROFILE_ROWS)
    assert r['тип'] == 'Серые лесные' and r['использование'] == 'пашня', r
    assert r['код'] == 'Alb-594-423' and 'Лебедева' in r['источник'], r
    assert 'Авторское название почвы' not in str(r), '«не указано» в карточку не идёт'

def test_index_from_title():
    assert fm.index_of('Ч<sup>в</sup>') == 'чв'
    assert fm.index_of('А<sup>н</sup>') == 'ан'
    assert fm.index_of(None) == ''

def test_distance_is_real_kilometres():
    """От 1173 до ближайшего разреза базы — 48 км: цифра из отчёта."""
    d = fm.km_between(53.125, 44.900, 53.48, 44.48)
    assert 47 < d < 50, d
    assert fm.km_between(53.0, 45.0, 53.0, 45.0) == 0

def test_map_frame_keeps_aspect_and_covers_parcel():
    from agroscan.sheets import fridland as sf
    rings = [[(44.89, 53.12), (44.91, 53.12), (44.91, 53.13), (44.89, 53.13)]]
    b = sf.bbox_of(rings, pad_km=11.0, aspect=1.45)
    assert b[0] < 44.89 and b[2] > 44.91 and b[1] < 53.12 and b[3] > 53.13
    import math
    w = (b[2] - b[0]) * 111.3 * math.cos(math.radians(53.125))
    h = (b[3] - b[1]) * 111.3
    assert abs(w / h - 1.45) < 0.02, (w, h)
    assert 20 < h < 35, 'кадр порядка 25 км, а не области целиком'

def test_projection_puts_point_where_expected():
    from agroscan.sheets import fridland as sf
    pr = sf.projector((44.8, 53.0, 45.0, 53.2), (1000, 1000))
    assert pr(44.9, 53.1) == (500.0, 500.0)
    x, y = pr(44.8, 53.2)
    assert (x, y) == (0.0, 0.0), 'северо-запад — левый верхний угол'
    assert sf.grid_step(0.2) == 0.05 and sf.grid_step(2.0) == 0.5

def test_same_type_picks_matching_profile():
    from agroscan.pipeline import _same_type
    prof = [{'id': 1, 'км': 48, 'тип': 'Серые лесные'},
            {'id': 2, 'км': 98, 'тип': 'Черноземы выщелоченные'}]
    assert _same_type(prof, 'Черноземы выщелоченные')['id'] == 2
    assert _same_type(prof, 'Солонцы') is None
    assert _same_type([], 'Черноземы выщелоченные') is None

def test_scale_bar_fits_the_frame():
    """Линейка занимает до трети кадра: раньше выходило то полкадра, то 50 км."""
    from agroscan.sheets import fridland as sf
    for span in (2.0, 8.0, 16.3, 35.0, 120.0):
        km = sf.scale_bar_km(span)
        assert km / span <= 0.34, (span, km)
        assert km / span >= 0.10, (span, km)
    assert sf.scale_bar_km(16.3) == 5 and sf.scale_bar_km(10.4) == 2

def test_zoom_frame_sits_inside_the_overview():
    """Рамка крупного плана на врезке обязана лежать внутри обзорного кадра."""
    from agroscan.sheets import fridland as sf
    rings = [[(44.89, 53.12), (44.91, 53.12), (44.91, 53.13), (44.89, 53.13)]]
    near = sf.bbox_of(rings, pad_km=3.5, aspect=1.88)
    wide = sf.bbox_of(rings, pad_km=16.0, aspect=1.5)
    assert wide[0] < near[0] and near[2] < wide[2], (near, wide)
    assert wide[1] < near[1] and near[3] < wide[3], (near, wide)
    import math
    near_km = (near[2] - near[0]) * 111.3 * math.cos(math.radians(53.125))
    wide_km = (wide[2] - wide[0]) * 111.3 * math.cos(math.radians(53.125))
    assert near_km < wide_km / 2, 'крупный план обязан быть заметно крупнее обзора'

def test_frame_zooms_to_parcel_size():
    """Кадр считается от участка: он должен занимать пятую часть ширины.

    Раньше отступ был постоянный (3,5 км), и участок 1,6 км занимал 10 %
    кадра — владелец попросил вдвое крупнее.
    """
    import math
    from agroscan.sheets import fridland as sf
    rings = [[(44.888, 53.118), (44.912, 53.118), (44.912, 53.133), (44.888, 53.133)]]
    aspect = 1.88
    w, h, lat = sf.size_km(rings)
    b = sf.bbox_of(rings, pad_km=sf.frame_pad_km(w, h, aspect), aspect=aspect)
    fw = (b[2] - b[0]) * 111.3 * math.cos(math.radians(lat))
    assert 0.18 <= w / fw <= 0.22, (w, fw, w / fw)

def test_frame_survives_odd_parcels():
    """Крошечный участок не прилипает к рамке, вытянутый — не вылезает."""
    import math
    from agroscan.sheets import fridland as sf
    tiny = [[(45.0, 53.0), (45.0025, 53.0), (45.0025, 53.0018), (45.0, 53.0018)]]
    w, h, lat = sf.size_km(tiny)
    pad = sf.frame_pad_km(w, h, 1.88)
    assert pad >= 0.35, pad
    b = sf.bbox_of(tiny, pad_km=pad, aspect=1.88)
    assert (b[3] - b[1]) * 111.3 > 0.7, 'кадр не схлопывается'

    tall = [[(45.0, 52.97), (45.014, 52.97), (45.014, 53.024), (45.0, 53.024)]]
    w, h, lat = sf.size_km(tall)
    b = sf.bbox_of(tall, pad_km=sf.frame_pad_km(w, h, 1.88), aspect=1.88)
    assert b[1] < 52.97 and b[3] > 53.024, 'вытянутый участок целиком в кадре'
    assert h / ((b[3] - b[1]) * 111.3) <= 0.8, 'по высоте остаётся поле'


if __name__ == '__main__':
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_'):
            fn(); n += 1; print('  ✓ %s' % name)
    print('\nКАРТА ФРИДЛАНДА: РАЗБОР ПРОВЕРЕН (%d проверок)' % n)
