# -*- coding: utf-8 -*-
"""Проверки геометрии частей. Обязательный шаг, а не ручная сверка.

Каждая проверка возвращает (пройдена, измеренное, допуск, текст). Отчёт
пишется в qa.json и ложится страницей в комплект. Смысл — ловить плохое,
а не подтверждать хорошее: проверка, которая никогда не падает, бесполезна.
"""
import itertools
from shapely.geometry import MultiPolygon
from shapely.ops import unary_union

def check(zones, parcel, egrn_ha, thin=3.0, area_tol_pct=0.01, cover_all=True,
          forest=None, forest_ha=0.0):
    """zones — {ключ: полигон}; egrn_ha — площадь по сведениям ЕГРН.

    forest — полигон лесничеств из КПТ, если они попали на участок:
    координаты частей не должны на него накладываться.
    """
    res = []
    def add(name, ok, got, tol, note=''):
        res.append({'проверка': name, 'пройдена': bool(ok),
                    'измерено': round(float(got), 4), 'допуск': tol, 'примечание': note})

    total = sum(g.area for g in zones.values()) / 1e4
    if cover_all:
        # лесничество вычтено из рабочей площади, поэтому сходимость
        # проверяем с ним: части + лесничество = площадь ЕГРН
        d = abs(total + forest_ha - egrn_ha) / egrn_ha * 100 if egrn_ha else 0
        add('сумма частей = площадь ЕГРН', d <= area_tol_pct, d, '%.2f %%' % area_tol_pct,
            'сумма %.4f га%s при ЕГРН %.4f га'
            % (total, ' + лесничество %.4f га' % forest_ha if forest_ha else '', egrn_ha))
    else:
        # части покрывают участок не целиком (действующая пашня вне ЧЗУ) —
        # проверяем только, что они не больше участка
        add('сумма частей не больше площади ЕГРН', total <= egrn_ha * 1.0001, total,
            '≤ %.4f га' % egrn_ha, 'вне частей %.4f га' % (egrn_ha - total))

    worst, pair = 0.0, ''
    for a, b in itertools.combinations(sorted(zones), 2):
        x = zones[a].intersection(zones[b]).area
        if x > worst:
            worst, pair = x, '%s∩%s' % (a, b)
    add('части не накладываются', worst <= 0.01, worst, '0 м²', pair)

    out = max((g.difference(parcel).area for g in zones.values()), default=0.0)
    add('части внутри границы ЕГРН', out <= 0.01, out, '0 м²')

    if forest is not None:
        over = sum(g.intersection(forest).area for g in zones.values())
        add('части не накладываются на лесничества', over <= 0.01, over, '0 м²',
            'лесничество на участке %.4f га' % forest_ha)

    slivers = []
    for k, g in zones.items():
        for p in (g.geoms if isinstance(g, MultiPolygon) else [g]):
            if not p.is_empty and p.buffer(-thin / 2).is_empty:
                slivers.append((k, round(p.area, 1)))
    add('нет частей уже %.0f м' % thin, not slivers, len(slivers), '0 шт',
        '; '.join('%s %.0f м²' % s for s in slivers[:5]))

    if cover_all:
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
