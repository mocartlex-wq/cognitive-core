# -*- coding: utf-8 -*-
"""Пачка не должна падать целиком из-за одного участка."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agroscan.batch import process

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BAD = """kn: "58:24:0341802:9999"
egrn_ha: 50.0
zone: msk58-2
rings: /nonexistent/rings.json
meta: /nonexistent/bgmeta.json
image: /nonexistent/bg.jpg
parts: {"1": "тест"}
priority: ["1"]
"""

def test_broken_config_does_not_stop_batch():
    with tempfile.TemporaryDirectory() as d:
        bad = os.path.join(d, 'bad.yaml')
        open(bad, 'w', encoding='utf-8').write(BAD)
        good = os.path.join(ROOT, 'parcels', '58-24-0341802-1173.yaml')
        s = process([bad, good], sheets=False, formats=False)
        assert s['участков'] == 2
        assert s['записи'][0]['статус'] == 'ошибка'
        assert 'ошибка' in s['записи'][0]
        # второй участок обязан посчитаться, несмотря на падение первого
        assert s['записи'][1]['статус'] == 'готово', s['записи'][1]
        assert abs(s['записи'][1]['сумма_га'] - 141.6154) < 0.02

if __name__ == '__main__':
    test_broken_config_does_not_stop_batch()
    print('\nПАЧКА УСТОЙЧИВА К СБОЮ УЧАСТКА')
