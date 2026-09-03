# -*- coding: utf-8 -*-
"""Источники ретроспективы: архив Wayback и снимок Google.

Проверяется то, на чём легко обмануться: что архив отдаёт датированные и
неповторяющиеся срезы, а Google без ключа выключается внятно, а не роняет
конвейер и не молчит.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agroscan.sources import google, wayback

LON, LAT = 45.1975, 52.5095          # участок 58:17:0130701:29

def test_google_is_off_without_key():
    saved = {k: os.environ.pop(k, None) for k in ('AGROSCAN_GOOGLE_KEY', 'GOOGLE_MAPS_API_KEY')}
    try:
        assert google.api_key() is None
        s, err = google.session()
        assert s is None and 'ключ не задан' in err, (s, err)
        tpl, err = google.tile_template()
        assert tpl is None and err
        img, meta, err = google.fetch((0, 1, 0, 1), None)
        assert img is None and meta is None and err, err
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v

def test_google_reports_refusal_with_bad_key():
    """Отказ Google должен доходить до отчёта строкой, а не тишиной."""
    os.environ['AGROSCAN_GOOGLE_KEY'] = 'заведомо-неверный-ключ'
    try:
        google._SESSION.clear()
        s, err = google.session()
        assert s is None and err.startswith('Google отказал'), (s, err)
    finally:
        google._SESSION.clear()
        os.environ.pop('AGROSCAN_GOOGLE_KEY', None)

def test_wayback_versions_are_dated_and_thinned():
    rel = wayback.releases()
    if not rel:
        print('    (сети нет — архив пропущен)')
        return
    assert len(rel) > 50, len(rel)
    assert all(wayback.DATE_RE.fullmatch(d) for d, _ in rel)
    assert [d for d, _ in rel] == sorted(d for d, _ in rel)
    v = wayback.versions(LON, LAT)
    assert 2 <= len(v) < len(rel), (len(v), len(rel))       # просеивание работает
    assert len(set(d for d, _ in v)) == len(v)              # дат-дублей нет
    assert '{z}' in v[0][1] and '{x}' in v[0][1] and '{y}' in v[0][1], v[0][1]

def test_wayback_template_is_converted():
    u = wayback._tpl('https://x/MapServer/tile/123/{level}/{row}/{col}')
    assert u.endswith('/123/{z}/{y}/{x}'), u


if __name__ == '__main__':
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_'):
            fn(); n += 1; print('  ✓ %s' % name)
    print('\nИСТОЧНИКИ РЕТРОСПЕКТИВЫ ПРОВЕРЕНЫ (%d проверки)' % n)
