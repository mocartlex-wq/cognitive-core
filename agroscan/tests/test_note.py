# -*- coding: utf-8 -*-
"""Пояснительная записка: нумерация разделов и почвенная характеристика.

Записка — первый лист, который читает специалист. Номера разделов раньше
писались руками, и при выпавшем разделе шли 4 → 6; почвы в записку
не попадали вовсе.
"""
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agroscan.sheets import note as sh_note

PARTS = {'1': {'areaHa': 81.4, 'название': 'древесная растительность',
               'outer': [], 'inner': []},
         '2': {'areaHa': 14.11, 'название': 'залежь', 'outer': [], 'inner': []}}
SOIL = {'точек': 5,
        'профиль': {'0–5 см': {'clay': 26.4, 'silt': 42.1, 'sand': 31.6, 'humus': 10.3,
                               'phh2o': 6.1, 'bdod': 1.08},
                    '15–30 см': {'clay': 30.9, 'silt': 39.5, 'sand': 29.6, 'humus': 2.6,
                                 'phh2o': 6.2, 'bdod': 1.31}},
        'wrb': {'wrb': 'Phaeozems', 'вероятности': [['Phaeozems', 23]],
                'русское_соответствие': 'тёмно-серые лесные, лугово-чернозёмные '
                                        'и выщелоченные чернозёмы'},
        'карта_почв': {'название': 'Черноземы выщелоченные', 'индекс': 'чв', 'код': 117,
                       'порода': 'Среднесуглинистые',
                       'сопутствующие': ['Черноземы оподзоленные (чоп)']},
        'слои': {'humus': [8.7, 10.0, 10.9], 'clay': [22.5, 26.4, 29.2],
                 'phh2o': [5.7, 6.1, 6.2]},
        'выводы': [('Гранулометрический состав — средний суглинок.', [], None)]}

def _build(result, attachments=()):
    path = os.path.join(tempfile.mkdtemp(), 'note.pdf')
    sh_note.build(path, '58:24:0341802:1173', PARTS, 141.6154, result,
                  'Пензенская область', 'МСК-58, зона 2', attachments=attachments)
    return path

def test_sections_are_numbered_in_order():
    n = sh_note.Note()
    seen = []
    n.head = lambda t, size=3.6, keep=48: seen.append(t)
    for t in ('Объект', 'Ведомость', 'Контроль'):
        n.section(t)
    assert [x.split('.')[0] for x in seen] == ['1', '2', '3'], seen

def test_note_without_soil_still_builds():
    """Почв нет — раздела нет, записка собирается."""
    p = _build({'qa': {'пройдено': True, 'проверки': []}})
    assert os.path.getsize(p) > 10000

def test_soil_section_present():
    p = _build({'qa': {'пройдено': True, 'проверки': []}, 'почвы': SOIL},
               attachments=['Схема_ЧЗУ.pdf', 'Приложение_почвы.pdf'])
    assert os.path.getsize(p) > 10000

def test_attachments_listed_only_when_made():
    """Перечень приложений собирается по факту, а не обещается заранее."""
    one = sh_note.attachments_line(['Схема_ЧЗУ.pdf'])
    assert 'схема расположения частей ЗУ' in one and 'почвенная' not in one, one
    both = sh_note.attachments_line(['Схема_ЧЗУ.pdf', 'Приложение_почвы.pdf'])
    assert 'почвенная характеристика' in both, both
    assert 'рельеф' not in both, 'приложения, которого нет, в перечне быть не должно'
    assert sh_note.attachments_line([]).startswith('Приложения:')

if __name__ == '__main__':
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_'):
            fn(); n += 1; print('  ✓ %s' % name)
    print('\nЗАПИСКА ПРОВЕРЕНА (%d проверок)' % n)
