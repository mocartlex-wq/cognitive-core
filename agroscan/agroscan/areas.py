# -*- coding: utf-8 -*-
"""Площади частей в целых метрах, сведённые с площадью по сведениям ЕГРН.

Площадь ЧЗУ/1 на :29 по координатам — 21 911,61 м², а в ЕГРН записано
21 911. Округление давало 21 912, и в одном документе рядом стояли две
разные площади одного контура. Здесь целые метры распределяются методом
наибольшего остатка так, чтобы сумма частей в точности равнялась ЕГРН,
а сдвиг каждой части не превышал одного метра.

Нормировка применяется только когда части покрывают участок целиком и
уже сходятся с ЕГРН: расхождение больше допуска — это ошибка геометрии,
её ловит qa, а не прячет округление.
"""

def m2(part):
    """Площадь части в целых метрах: нормированная, если конвейер её свёл."""
    v = part.get('м2')
    return int(v) if v is not None else int(round(part['areaHa'] * 10000))

def ha(part):
    """Гектары из тех же метров, что напечатаны в м²: иначе строки разойдутся."""
    return m2(part) / 10000.0

def fit(parts, egrn_ha, cover_all=True, extra_ha=0.0, tol_pct=0.05):
    """Свести целые метры частей к площади ЕГРН. Возвращает отчёт о сведении.

    extra_ha — площадь, вычтенная из рабочей (лесничества из КПТ): она
    входит в баланс так же, как в qa.check, но своей строкой не печатается.
    """
    keys = sorted(parts)
    raw = {k: parts[k]['areaHa'] * 10000 for k in keys}
    total = sum(raw.values())
    target = int(round((egrn_ha - extra_ha) * 10000))
    info = {'сведено': False, 'цель_м2': target,
            'сумма_м2': int(round(total)), 'сдвиг_м2': {}}
    if not cover_all or not keys or target <= 0:
        return info
    if abs(total - target) > max(1.0, target * tol_pct / 100):
        return info                     # это не округление, а расхождение
    base = {k: int(raw[k]) for k in keys}
    rest = target - sum(base.values())
    order = sorted(keys, key=lambda k: (-(raw[k] - base[k]), -raw[k]))
    for i in range(abs(rest)):
        k = order[i % len(order)]
        base[k] += 1 if rest > 0 else -1
    for k in keys:
        parts[k]['м2'] = base[k]
        info['сдвиг_м2'][k] = round(base[k] - raw[k], 2)
    info['сведено'] = True
    info['сумма_м2'] = sum(base.values())
    return info
