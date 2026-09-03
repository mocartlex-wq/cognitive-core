# -*- coding: utf-8 -*-
"""Инструмент разметки: собирается по конфигу и открывается как страница.

Проверяется то, из-за чего он однажды выглядел сломанным: незамещённые
плейсхолдеры шаблона, слои разного размера (высота полога занимала четверть
поля) и вес страницы — артефакт больше 16 МБ не публикуется.
"""
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agroscan import markup_tool, tool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = os.path.join(ROOT, 'parcels', '58-17-0130701-29.yaml')
KN = '58:17:0130701:29'

def _build(tmp):
    return markup_tool.build(CFG, out_path=os.path.join(tmp, 'tool.html'), verbose=False)

def test_tool_builds_and_fits_artifact_limit():
    with tempfile.TemporaryDirectory() as tmp:
        path, size, info = _build(tmp)
        html = open(path, encoding='utf-8').read()
        assert KN in html
        assert not re.search(r'__[A-Z_]+__', html), re.findall(r'__[A-Z_]+__', html)[:3]
        assert size < 16 * 1024 * 1024, size          # предел артефакта
        assert info['подложек'] >= 3, info            # два снимка + полог (+ NDVI)
        assert info['частей'] >= 1, info

def test_raster_layers_match_backdrop_size():
    """Слои полога и NDVI растягиваются до кадра.

    Они считаются на прореженной сетке, а инструмент рисует тайл в его
    собственных пикселях: без растяжения полог занимал четверть поля.
    """
    import base64
    from io import BytesIO
    from PIL import Image
    with tempfile.TemporaryDirectory() as tmp:
        path, _, _ = _build(tmp)
        html = open(path, encoding='utf-8').read()
        i = html.index('TILES=') + len('TILES=')
        tiles, _ = json.JSONDecoder().raw_decode(html[i:])   # дальше идёт SAVED и прочее
        sizes = {k: Image.open(BytesIO(base64.b64decode(v['data']))).size
                 for k, v in tiles.items()}
        assert len(set(sizes.values())) == 1, sizes

def test_markup_round_trip():
    """Кольца и вырезы разметки читаются обратно тем же форматом."""
    payload = {'objects': [
        {'n': 1, 'type': 'belt', 'ring': [[0, 0], [10, 0], [10, 10], [0, 10]],
         'holes': [[[2, 2], [4, 2], [4, 4], [2, 4]]]},
        {'n': 2, 'type': 'gully', 'ring': [[20, 0], [30, 0], [30, 10]], 'holes': []}]}
    rings, holes = tool.read_markup(payload)
    assert len(rings) == 1 and len(holes) == 1          # овраг в лесополосы не идёт
    rings, _ = tool.read_markup(payload, kinds=('belt', 'gully'))
    assert len(rings) == 2

def test_saved_markup_is_loaded_back():
    """Принятая разметка возвращается в инструмент, а не теряется."""
    with tempfile.TemporaryDirectory() as tmp:
        mk = os.path.join(tmp, 'markup.json')
        json.dump({'objects': [{'n': 1, 'type': 'belt',
                                'ring': [[2242150, 305700], [2242190, 305760],
                                         [2242205, 305750]], 'holes': []}],
                   'saved_at': '2026-09-03T10:00:00'}, open(mk, 'w'))
        objs, ts = markup_tool._saved_markup(mk)
        assert len(objs) == 1 and ts > 0
        assert markup_tool._saved_markup(os.path.join(tmp, 'нет.json')) == (None, 0)


if __name__ == '__main__':
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_'):
            fn(); n += 1; print('  ✓ %s' % name)
    print('\nИНСТРУМЕНТ РАЗМЕТКИ ПРОВЕРЕН (%d проверки)' % n)
