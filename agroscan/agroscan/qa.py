# -*- coding: utf-8 -*-
"""Проверки геометрии частей. Обязательный шаг, а не ручная сверка.

Каждая проверка возвращает (пройдена, измеренное, допуск, текст). Отчёт
пишется в qa.json и ложится страницей в комплект. Смысл — ловить плохое,
а не подтверждать хорошее: проверка, которая никогда не падает, бесполезна.
"""
import itertools
from shapely.geometry import MultiPolygon
from shapely.ops import unary_union

def check(zones, parcel, egrn_ha, thin=3.0, area_tol_pct=0.01):
    """zones — {ключ: полигон}; egrn_ha — площадь по сведениям ЕГРН."""
    res = []
    def add(name, ok, got, tol, note=''):
        res.append({'проверка': name, 'пройдена': bool(ok),
                    'измерено': round(float(got), 4), 'допуск': tol, 'примечание': note})

    total = sum(g.area for g in zones.values()) / 1e4
    d = abs(total - egrn_ha) / egrn_ha * 100 if egrn_ha else 0
    add('сумма частей = площадь ЕГРН', d <= area_tol_pct, d, '%.2f %%' % area_tol_pct,
        'сумма %.4f га при ЕГРН %.4f га' % (total, egrn_ha))

    worst, pair = 0.0, ''
    for a, b in itertools.combinations(sorted(zones), 2):
        x = zones[a].intersection(zones[b]).area
        if x > worst:
            worst, pair = x, '%s∩%s' % (a, b)
    add('части не накладываются', worst <= 0.01, worst, '0 м²', pair)

    out = max((g.difference(parcel).area for g in zones.values()), default=0.0)
    add('части внутри границы ЕГРН', out <= 0.01, out, '0 м²')

    slivers = []
    for k, g in zones.items():
        for p in (g.geoms if isinstance(g, MultiPolygon) else [g]):
            if not p.is_empty and p.buffer(-thin / 2).is_empty:
                slivers.append((k, round(p.area, 1)))
    add('нет частей уже %.0f м' % thin, not slivers, len(slivers), '0 шт',
        '; '.join('%s %.0f м²' % s for s in slivers[:5]))

    gap = parcel.difference(unary_union(list(zones.values()))).area
    add('участок покрыт без зазоров', gap <= 1.0, gap, '≤ 1 м²')

    empty = [k for k, g in zones.items() if g.is_empty]
    add('все части непустые', not empty, len(empty), '0 шт', ', '.join(empty))

    return {'пройдено': all(r['пройдена'] for r in res),
            'провалено': [r['проверка'] for r in res if not r['пройдена']],
            'проверки': res}

def report(qa, prefix='  '):
    lines = []
    for r in qa['проверки']:
        lines.append('%s%s %-32s измерено %-12s допуск %s%s'
                     % (prefix, '✓' if r['пройдена'] else '✗', r['проверка'],
                        r['измерено'], r['допуск'],
                        '  — ' + r['примечание'] if r['примечание'] else ''))
    lines.append('%s%s' % (prefix, 'ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ' if qa['пройдено']
                           else 'ПРОВАЛЕНО: ' + ', '.join(qa['провалено'])))
    return '\n'.join(lines)
