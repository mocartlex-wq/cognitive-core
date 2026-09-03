# -*- coding: utf-8 -*-
"""Редактор раскладки схемы: правообладатель двигает подписи сам.

Автоматика ставит подписи в свободное место, но «свободное» и «удобное» —
разное: под кадастровым номером может оказаться контур, а таблица условных
обозначений может закрыть смежника. Здесь лист отдаётся как картинка,
подписи — как перетаскиваемые рамки, а на выходе layout.json, который
конвейер применяет при следующей сборке.
"""
import json
import os

TITLES = {'kn': 'Кадастровый номер', 'legend': 'Условные обозначения',
          'stamp': 'Утверждаю', 'coord0': 'Координаты: север',
          'coord1': 'Координаты: юг', 'coord2': 'Координаты: запад',
          'coord3': 'Координаты: восток'}
SIZE_MM = {'kn': (44, 7), 'legend': (103, 40), 'stamp': (62, 30),
           'coord0': (26, 12), 'coord1': (26, 12), 'coord2': (26, 12), 'coord3': (26, 12)}
ANCHOR_CENTER = ('kn', 'coord0', 'coord1', 'coord2', 'coord3')

def build(path_html, png_name, sheet_mm, layout, kn=''):
    """Собрать страницу редактора рядом с картинкой листа."""
    tpl = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates', 'layout.html')
    items = []
    for key, pos in sorted(layout.items()):
        w, h = SIZE_MM.get(key, (30, 8))
        items.append({'key': key, 'title': TITLES.get(key, key),
                      'x': pos[0], 'y': pos[1], 'w': w, 'h': h,
                      'center': key in ANCHOR_CENTER})
    body = open(tpl, encoding='utf-8').read()
    body = (body.replace('/*IMG*/""', json.dumps(png_name))
                .replace('/*ITEMS*/[]', json.dumps(items, ensure_ascii=False))
                .replace('/*SHEET*/[297,210]', json.dumps(list(sheet_mm)))
                .replace('/*KN*/""', json.dumps(kn)))
    open(path_html, 'w', encoding='utf-8').write(body)
    return path_html

def load(path):
    """Прочитать layout.json, присланный правообладателем."""
    if not path or not os.path.exists(path):
        return {}
    d = json.load(open(path, encoding='utf-8'))
    return {k: [float(v[0]), float(v[1])] for k, v in (d.get('позиции') or d).items()
            if isinstance(v, (list, tuple)) and len(v) == 2}
