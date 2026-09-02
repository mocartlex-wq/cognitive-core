# -*- coding: utf-8 -*-
"""Имена файлов комплекта: участок и вид работ должны быть в названии.

Файлы уходят заказчику по одному, вне своей папки; три «Пояснительная
записка.pdf» из разных комплектов неразличимы.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agroscan import naming

CFG = {'kn': '58:24:0341802:1173', 'вид_работ': 'анализ зарастания'}

def test_name_carries_parcel_and_work():
    n = naming.fname(CFG, 'Пояснительная_записка.pdf')
    assert n == '58-24-0341802-1173_анализ_зарастания_Пояснительная_записка.pdf', n
    assert ':' not in n and ' ' not in n, 'двоеточий и пробелов в имени быть не должно'

def test_work_defaults_and_is_sanitised():
    assert naming.work_of({'kn': 'x'}) == naming.WORK_DEFAULT
    dirty = {'kn': '58:24:0341802:1173', 'вид_работ': 'обследование / оценка: 2026'}
    n = naming.fname(dirty, 'Схема_ЧЗУ.pdf')
    assert 'обследование_оценка_2026' in n, n
    assert '/' not in n and ':' not in n.split('_анализ')[0].replace('58-24', ''), n

def test_doc_kind_finds_the_document():
    """По имени файла узнаём документ — на этом держится перечень приложений."""
    assert naming.doc_kind('58-24-0341802-1173_анализ_зарастания_Приложение_почвы.pdf') \
        == 'Приложение_почвы.pdf'
    assert naming.doc_kind('/tmp/x/58-28-0500401-74_обмер_Схема_ЧЗУ.pdf') == 'Схема_ЧЗУ.pdf'
    assert naming.doc_kind('что-то_своё.pdf') == 'что-то_своё.pdf'

def test_attachments_line_survives_prefixed_names():
    from agroscan.sheets import note
    line = note.attachments_line(['58-24-0341802-1173_анализ_зарастания_Приложение_почвы.pdf'])
    assert 'почвенная характеристика' in line, line

if __name__ == '__main__':
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_'):
            fn(); n += 1; print('  ✓ %s' % name)
    print('\nИМЕНА ФАЙЛОВ ПРОВЕРЕНЫ (%d проверок)' % n)
