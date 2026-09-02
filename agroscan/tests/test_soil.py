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


def test_texture_advice_matches_class():
    """Совет по проходимости не должен спорить с названием класса."""
    mid = soil.interpret(_rows(24, 45, 31, 3.0, 6.5, 1.2))[0]
    assert 'средний суглинок' in mid[0], mid[0]
    assert 'лёгкий' not in ' '.join(mid[1]), mid[1]
    light = soil.interpret(_rows(10, 30, 60, 3.0, 6.5, 1.2))[0]
    assert 'Состав лёгкий' in ' '.join(light[1]), light[1]
    heavy = soil.interpret(_rows(35, 35, 30, 3.0, 6.5, 1.2))[0]
    assert 'по сухому' in ' '.join(heavy[1]), heavy[1]

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

# ── карта почвенных условий ─────────────────────────────────────────────
def test_storage_factors_give_table_units():
    """Множители растра переводят единицы хранения в единицы таблицы.

    Проверено сверкой с точечным API в тех же координатах: глина 292 → 29,2 %,
    плотность 111 → 1,11 г/см³. Ошибка здесь дала бы карту в чужих единицах,
    и она молча разошлась бы с таблицей.
    """
    assert 292 / soil.D_FACTOR['clay'] == 29.2
    assert 111 / soil.D_FACTOR['bdod'] == 1.11
    assert 64 / soil.D_FACTOR['phh2o'] == 6.4
    soc = 597 / soil.D_FACTOR['soc']              # → г/кг
    assert abs(soc / 10 * soil.HUMUS_K - 10.29) < 0.01, soc

def test_ramp_is_monotone():
    """Шкала обязана расти от края к краю: иначе цвет не читается как значение."""
    from agroscan.sheets import soil_map as sm
    lum = [sum(sm.ramp_color(i / 20.0, sm.RAMP_HUMUS)) for i in range(21)]
    assert lum[0] > lum[-1], 'гумус: больше — темнее'
    assert all(a >= b - 1 for a, b in zip(lum, lum[1:])), lum
    assert sm.ramp_color(-5, sm.RAMP_CLAY) == sm.RAMP_CLAY[0]
    assert sm.ramp_color(9, sm.RAMP_CLAY) == sm.RAMP_CLAY[-1]

def test_cell_edges_follow_value_change():
    """Границы ячеек рисуются там, где значение меняется, и только там."""
    import numpy as np
    from agroscan.sheets import soil_map as sm
    a = np.array([[1.0, 1.0, 2.0, 2.0]] * 4)
    e = sm.cell_edges(a)
    assert e[:, 2].all(), e                       # линия ровно на стыке, шириной в узел
    assert not e[:, (0, 1, 3)].any(), e
    assert not sm.cell_edges(np.full((4, 4), 3.0)).any(), 'однородный слой — без линий'

def test_raster_survives_dead_source():
    """Источник не ответил — слой None, а не исключение наружу."""
    class _G:
        def sample(self, path, **kw):
            raise OSError('нет сети')
    assert soil.raster(_G(), 'clay') is None

def test_map_page_needs_data():
    """Нет снимка или нет слоёв — страница не строится, а не падает."""
    from agroscan.sheets import soil_map as sm
    assert sm.build('58:00:0000000:1', [], {}, None, {'humus': 1}, []) is None
    assert sm.build('58:00:0000000:1', [], {}, 'нет.jpg', {}, []) is None


if __name__ == '__main__':
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_'):
            fn(); n += 1; print('  ✓ %s' % name)
    print('\nПОЧВЕННЫЕ ВЫВОДЫ СЧИТАЮТСЯ ИЗ ДАННЫХ (%d проверок)' % n)
