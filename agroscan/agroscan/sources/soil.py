# -*- coding: utf-8 -*-
"""Почвенные свойства: SoilGrids v2.0 (ISRIC World Soil Information).

Значения запрашиваются по координатам участка, а не задаются вручную:
в прежнем листе они были вбиты константами для двух конкретных участков,
и на третьем лист показал бы чужую почву.

Сервис отдаёт величины в единицах хранения (глина в г/кг, pH умноженный
на десять и так далее); здесь они сразу приводятся к тем, в которых их
читает агроном.
"""
import json
import os
import subprocess

BASE = 'https://rest.isric.org/soilgrids/v2.0/properties/query'
PROPS = ('clay', 'sand', 'silt', 'soc', 'phh2o', 'cec', 'bdod', 'nitrogen')
DEPTHS = ('0-5cm', '5-15cm', '15-30cm')
DEPTH_RU = {'0-5cm': '0–5 см', '5-15cm': '5–15 см', '15-30cm': '15–30 см'}
HUMUS_K = 1.724          # переводной коэффициент от органического углерода к гумусу

def query(lon, lat, props=PROPS, depths=DEPTHS, timeout=90):
    """{свойство: {горизонт: значение в целевых единицах}}."""
    p = ['lon=%.5f' % lon, 'lat=%.5f' % lat, 'value=mean']
    p += ['property=' + x for x in props] + ['depth=' + d for d in depths]
    cmd = ['curl', '-sS', '--max-time', str(timeout), BASE + '?' + '&'.join(p)]
    ca = '/root/.ccr/ca-bundle.crt'
    if os.path.exists(ca):
        cmd[1:1] = ['--cacert', ca]
    out = subprocess.run(cmd, capture_output=True, check=True).stdout
    d = json.loads(out)
    res, units = {}, {}
    for lay in d.get('properties', {}).get('layers', []):
        f = lay['unit_measure']['d_factor'] or 1
        units[lay['name']] = lay['unit_measure']['target_units']
        res[lay['name']] = {}
        for dep in lay['depths']:
            v = dep['values'].get('mean')
            if v is not None:
                res[lay['name']][dep['label']] = v / f
    return res, units

def profile_points(points):
    """Профиль, усреднённый по нескольким точкам внутри участка.

    Одна точка характеризует ячейку 250 м, а не массив: на 1173 центр
    участка и его края расходятся по глине на четыре процента. Берём
    несколько точек и усредняем.
    """
    acc, units = {}, {}
    used = 0
    for lon, lat in points:
        try:
            raw, u = query(lon, lat)
        except Exception:
            continue
        if not raw:
            continue
        units.update(u); used += 1
        for k, byd in raw.items():
            for dep, v in byd.items():
                acc.setdefault(k, {}).setdefault(dep, []).append(v)
    if not used:
        return {}, {}, 0
    raw = {k: {d: sum(v) / len(v) for d, v in byd.items()} for k, byd in acc.items()}
    return _rows(raw), units, used

def _rows(raw):
    rows = {}
    for dep in DEPTHS:
        if not all(dep in raw.get(k, {}) for k in ('clay', 'sand', 'silt')):
            continue
        r = {k: raw[k][dep] for k in raw if dep in raw[k]}
        r['humus'] = r.get('soc', 0) / 10 * HUMUS_K    # г/кг → % → гумус
        rows[DEPTH_RU[dep]] = r
    return rows

def profile(lon, lat):
    """Профиль по одной точке — для быстрой проверки."""
    raw, units = query(lon, lat)
    return _rows(raw), units

# ── агрономическая интерпретация: выводы считаются, а не пишутся руками ──
def texture_class(clay, silt, sand):
    """Класс по треугольнику USDA — в терминах, принятых в агрономии."""
    if clay >= 40:
        return 'глина'
    if clay >= 27:
        return 'тяжёлый суглинок' if silt < 50 else 'тяжелосуглинистый пылеватый'
    if clay >= 20:
        return 'средний суглинок'
    if sand >= 70:
        return 'супесь' if clay >= 7 else 'песок'
    return 'лёгкий суглинок'

def _grade(v, steps):
    for lim, name in steps:
        if v < lim:
            return name
    return steps[-1][1]

def _ru(x):
    """Десятичный разделитель — запятая: в таблице листа он такой же."""
    return x.replace('.', ',') if isinstance(x, str) else x

def interpret(rows):
    """Список выводов: (заголовок, строки, цвет-подсветка или None)."""
    if not rows:
        return []
    top = list(rows.values())[0]
    bot = list(rows.values())[-1]
    out = []

    cls = texture_class(top['clay'], top['silt'], top['sand'])
    heavy = top['clay'] >= 27
    out.append(('Гранулометрический состав — %s.' % cls, [
        'Глина %.0f–%.0f %%, пыль %.0f–%.0f %%, песок %.0f–%.0f %% по профилю.'
        % (min(r['clay'] for r in rows.values()), max(r['clay'] for r in rows.values()),
           min(r['silt'] for r in rows.values()), max(r['silt'] for r in rows.values()),
           min(r['sand'] for r in rows.values()), max(r['sand'] for r in rows.values())),
        ('Для раскорчёвки это значит: работать по сухому — во влажном'
         if heavy else 'Состав лёгкий: техника проходит и во влажном состоянии,'),
        ('состоянии такой грунт залипает и мнётся техникой.'
         if heavy else 'но почва склонна к иссушению и ветровой эрозии.')], None))

    h = top['humus']
    lvl = _grade(h, [(2, 'низкий'), (4, 'средний'), (6, 'повышенный'), (99, 'высокий')])
    out.append(('Гумус в верхнем горизонте %s — %.1f %%.' % (lvl, h), [
        'В слое %s — %.1f %%, в слое %s — %.1f %%.'
        % (list(rows)[0], h, list(rows)[-1], bot['humus']),
        ('Верхний горизонт богатый: типично для задернованной залежи.'
         if h >= 4 else 'Запас органики умеренный.'),
        ('Плодородие после раскорчёвки восстанавливается быстро.'
         if h >= 4 else 'Под первую культуру целесообразно внесение органики.')],
        (0, 110, 0) if h >= 4 else None))

    ph = top['phh2o']
    if ph < 5.0:
        lime = ('Кислотность высокая — известкование необходимо.',
                'Сильнокислая реакция (pH %.1f) угнетает большинство культур.' % ph)
    elif ph < 5.5:
        lime = ('Кислотность повышенная — известкование желательно.',
                'Среднекислая реакция (pH %.1f): зерновые терпят, бобовые нет.' % ph)
    elif ph < 6.0:
        lime = ('Кислотность слабая — известкование не требуется.',
                'Слабокислая реакция (pH %.1f) годится под зерновые и травы.' % ph)
    elif ph <= 7.3:
        lime = ('Реакция близкая к нейтральной — поправок не нужно.',
                'pH %.1f по профилю — оптимум для большинства культур.' % ph)
    else:
        lime = ('Реакция щелочная.', 'pH %.1f: возможен дефицит железа и цинка.' % ph)
    out.append((lime[0], [lime[1],
                'По горизонтам: ' + ', '.join('%s — %.1f' % (k, r['phh2o'])
                                              for k, r in rows.items()) + '.',
                'Перед закладкой пропашных стоит подтвердить полевым анализом.'],
                (0, 110, 0) if 5.5 <= ph <= 7.3 else (170, 90, 0)))

    bd = [r['bdod'] for r in rows.values()]
    dense = max(bd) >= 1.4
    out.append(('Плотность %s.' % ('повышенная — есть переуплотнение' if dense else 'в норме'), [
        '%.2f–%.2f г/см³ с ростом по глубине — обычный профиль.' % (min(bd), max(bd)),
        ('Требуется глубокое рыхление перед вводом в оборот.' if dense
         else 'Плужной подошвы данные не показывают.')],
        (170, 90, 0) if dense else None))

    if 'cec' in top:
        cec = top['cec']
        lvl = _grade(cec, [(15, 'низкая'), (25, 'средняя'), (99, 'высокая')])
        out.append(('Ёмкость катионного обмена %s — %.0f смоль/кг.' % (lvl, cec), [
            'В подпахотном слое %.0f смоль/кг.' % bot.get('cec', cec),
            ('Поглощающий комплекс развит, удобрения удерживаются.' if cec >= 25
             else 'Удобрения вносить дробно: комплекс удерживает их слабо.')], None))
    return [(_ru(h), [_ru(l) for l in lines], c) for h, lines, c in out]

# ── тип почвы ───────────────────────────────────────────────────────────
CLASSIFY = 'https://rest.isric.org/soilgrids/v2.0/classification/query'

# Соответствие WRB и русской классификации — ОРИЕНТИРОВОЧНОЕ: системы
# построены на разных признаках, WRB опирается на диагностические горизонты,
# отечественная — на генезис. Даём как подсказку, а не как перевод.
WRB_RU = {
    'Phaeozems': 'тёмно-серые лесные и лугово-чернозёмные',
    'Chernozems': 'чернозёмы',
    'Luvisols': 'серые лесные',
    'Albeluvisols': 'дерново-подзолистые',
    'Retisols': 'дерново-подзолистые',
    'Podzols': 'подзолы',
    'Greyzems': 'серые лесные',
    'Kastanozems': 'каштановые',
    'Gleysols': 'глеевые',
    'Fluvisols': 'аллювиальные',
    'Arenosols': 'песчаные',
    'Cambisols': 'буроземы',
    'Umbrisols': 'дерново-гумусовые',
    'Solonetz': 'солонцы',
    'Solonchaks': 'солончаки',
    'Histosols': 'торфяные',
}

def classification(lon, lat, n=5, timeout=180, tries=3):
    """Класс WRB по координатам: ведущий и распределение вероятностей.

    Эндпоинт классификации отвечает от десяти секунд до нескольких минут и
    иногда обрывается, поэтому попытки повторяются, а при неудаче
    возвращается None: лист собирается без блока, а не падает.
    """
    ca = '/root/.ccr/ca-bundle.crt'
    url = '%s?lon=%.5f&lat=%.5f&number_classes=%d' % (CLASSIFY, lon, lat, n)
    for attempt in range(tries):
        cmd = ['curl', '-sS', '--max-time', str(timeout), url]
        if os.path.exists(ca):
            cmd[1:1] = ['--cacert', ca]
        r = subprocess.run(cmd, capture_output=True)
        if r.returncode == 0 and r.stdout.lstrip().startswith(b'{'):
            try:
                d = json.loads(r.stdout)
            except Exception:
                continue
            top = d.get('wrb_class_name')
            if top:
                return {'wrb': top, 'русское_соответствие': WRB_RU.get(top),
                        'вероятности': [(k, v) for k, v in d.get('wrb_class_probability', [])]}
    return None

def fridland(index=None, code=None, path=None):
    """Тип по легенде почвенной карты РСФСР 1:2 500 000 (Фридланд и др., 1988).

    Карта показывается на soil-db.ru; открытого интерфейса к её контурам нет,
    поэтому индекс участка задаётся в конфиге вручную, а здесь по нему
    подставляется полное название из легенды.
    """
    path = path or os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), 'data', 'fridland_legend.json')
    if not os.path.exists(path):
        return None
    for row in json.load(open(path, encoding='utf-8')):
        if (index and row['индекс'] == index) or (code and str(row['код']) == str(code)):
            return row
    return None
