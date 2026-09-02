# -*- coding: utf-8 -*-
"""Почвенные выводы должны считаться из чисел, а не быть заготовкой.

В прежнем листе и значения, и текст выводов были вписаны руками под два
конкретных участка. Тест проверяет обратный случай: на другой почве лист
обязан сказать другое.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agroscan.sources import soil

def _rows(clay, silt, sand, humus, ph, bd, cec=25.0):
    r = {'clay': clay, 'silt': silt, 'sand': sand, 'humus': humus,
         'phh2o': ph, 'bdod': bd, 'cec': cec, 'nitrogen': 2.0, 'soc': humus / 1.724 * 10}
    return {'0–5 см': dict(r), '15–30 см': dict(r, humus=humus / 2)}

def test_texture_classes():
    assert soil.texture_class(45, 30, 25) == 'глина'
    assert soil.texture_class(30, 39, 31) == 'тяжёлый суглинок'
    assert soil.texture_class(24, 45, 31) == 'средний суглинок'
    assert soil.texture_class(3, 10, 87) == 'песок'

def test_acid_soil_asks_for_liming():
    out = soil.interpret(_rows(30, 39, 31, 3.0, 4.6, 1.2))
    heads = ' | '.join(h for h, _, _ in out)
    assert 'известкование необходимо' in heads, heads

def test_neutral_soil_does_not():
    out = soil.interpret(_rows(30, 39, 31, 5.0, 6.5, 1.2))
    heads = ' | '.join(h for h, _, _ in out)
    assert 'известкование' not in heads and 'нейтральной' in heads, heads

def test_compaction_is_flagged():
    out = soil.interpret(_rows(30, 39, 31, 3.0, 6.5, 1.48))
    heads = ' | '.join(h for h, _, _ in out)
    assert 'переуплотнение' in heads, heads
    normal = ' | '.join(h for h, _, _ in soil.interpret(_rows(30, 39, 31, 3.0, 6.5, 1.25)))
    assert 'переуплотнение' not in normal, normal

def test_poor_humus_recommends_organics():
    out = soil.interpret(_rows(30, 39, 31, 1.4, 6.5, 1.2))
    txt = ' '.join(' '.join(l) for _, l, _ in out)
    assert 'внесение органики' in txt, txt


def test_wrb_mapping():
    """Соответствие WRB даёт русское название, но не выдумывает его."""
    assert soil.WRB_RU['Phaeozems'].startswith('тёмно-серые')
    assert soil.WRB_RU['Chernozems'] == 'чернозёмы'
    assert soil.WRB_RU.get('Andosols') is None, 'для неизвестного класса нет соответствия'

def test_fridland_legend():
    """Легенда почвенной карты читается и отвечает по индексу."""
    r = soil.fridland(index='чв')
    assert r and 'выщелоченные' in r['название'], r
    assert soil.fridland(index='нетакого') is None
    import json as _j, os as _o
    path = _o.path.join(_o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))),
                        'data', 'fridland_legend.json')
    rows = _j.load(open(path, encoding='utf-8'))
    assert len(rows) > 250, len(rows)
    assert all({'код', 'индекс', 'название'} <= set(x) for x in rows)

if __name__ == '__main__':
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_'):
            fn(); n += 1; print('  ✓ %s' % name)
    print('\nПОЧВЕННЫЕ ВЫВОДЫ СЧИТАЮТСЯ ИЗ ДАННЫХ (%d проверок)' % n)
