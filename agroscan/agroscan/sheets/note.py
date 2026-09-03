# -*- coding: utf-8 -*-
"""Пояснительная записка — лист, который специалист читает первым.

Содержание собирается из результата расчёта: ведомость частей, источники,
методика, отчёт проверок, допущения. Числа не набиваются руками, поэтому
записка не может разойтись со схемой.
"""
from PIL import Image

from ..sheet import Sheet, fmt_ha, fmt_m2

class Note:
    """Многостраничный текстовый лист с автоматическим переносом."""

    def __init__(self, w_mm=210, h_mm=297, dpi=300):
        self.proto = Sheet(w_mm, h_mm, dpi=dpi, ss=2, margin_mm=10)
        self.w_mm, self.h_mm, self.dpi = w_mm, h_mm, dpi
        self.pages = []
        self.n_sec = 0                  # номера разделов считаются, а не пишутся руками
        self.new_page()

    def new_page(self):
        s = Sheet(self.w_mm, self.h_mm, dpi=self.dpi, ss=2, margin_mm=10)
        s.frame(outer_mm=10, inner=False)
        self.s = s
        self.L = s.mm(18); self.R = s.PW - s.mm(13)
        self.y = s.mm(15)
        self.bottom = s.PH - s.mm(26)
        self.pages.append(s)

    def need(self, h_mm):
        if self.y + self.s.mm(h_mm) > self.bottom:
            self.new_page()

    def head(self, t, size=3.6, keep=48):
        self.need(keep)                    # заголовок не отрывается от своего текста
        self.s.d.text((self.L, self.y), t, font=self.s.F(size, True), fill='black')
        self.y += self.s.mm(5.0)

    def section(self, t, size=3.6):
        """Раздел с автонумерацией: выпал раздел — номера не скачут."""
        self.n_sec += 1
        self.head('%d. %s' % (self.n_sec, t), size=size)

    def wrap(self, t, font):
        """Перенос по ширине колонки: длинная строка иначе уходит за рамку."""
        wmax = self.R - self.L
        out = []
        for para in t.split('\n'):
            cur = ''
            for word in para.split(' '):
                probe = (cur + ' ' + word).strip()
                if not cur or self.s.d.textlength(probe, font=font) <= wmax:
                    cur = probe
                else:
                    out.append(cur); cur = word
            out.append(cur)
        return '\n'.join(out)

    def image(self, img, w_mm=155, gap_mm=3.0):
        """Врезка во всю ширину колонки; не влезла — уходит на новую страницу."""
        w = self.s.mm(w_mm)
        h = int(img.height * w / img.width)
        if self.y + h + self.s.mm(gap_mm) > self.bottom:
            self.new_page()
        x = self.L
        self.s.page.paste(img.resize((w, h), Image.LANCZOS), (x, int(self.y)))
        self.s.d.rectangle([x, int(self.y), x + w, int(self.y) + h],
                           outline=(60, 60, 60), width=self.s.W(0.3))
        self.y += h + self.s.mm(gap_mm)

    def text(self, t, size=2.65, gap=3.6, color=(35, 35, 35)):
        f = self.s.F(size)
        t = self.wrap(t, f)
        self.need(gap * (t.count('\n') + 1))
        self.s.d.multiline_text((self.L, self.y), t, font=f, fill=color,
                                spacing=self.s.mm(1.35))
        self.y += self.s.mm(gap) * (t.count('\n') + 1)

    def save(self, path):
        for i, p in enumerate(self.pages, 1):
            p.d.text((p.PW / 2, p.PH - p.mm(15)), '— %d —' % i, font=p.F(2.5),
                     fill=(90, 90, 90), anchor='ma')
        return self.pages[0].save(path, extra_pages=self.pages[1:])

# Перечень приложений собирается по факту: они выпускаются не для каждого
# участка, и записка не должна обещать того, чего в комплекте нет.
TITLES = {'Схема_ЧЗУ.pdf': 'схема расположения частей ЗУ',
          'Проверочная_карта.pdf': 'проверочная карта без заливки',
          'Приложение_ИК.pdf': 'материалы съёмки в ИК-диапазоне',
          'Приложение_динамика.pdf': 'ряд NDVI и датировка выбытия из оборота',
          'Приложение_рельеф.pdf': 'рельеф и овражно-балочная сеть',
          'Приложение_почвы.pdf': 'почвенная характеристика и карта показателей'}

def _ru_num(v):
    return ('%g' % v).replace('.', ',')

def attachments_line(attachments=()):
    # имена файлов начинаются с участка и вида работ, поэтому сверяем
    # не полное имя, а название документа в конце
    from ..naming import doc_kind
    have = [TITLES[doc_kind(f)] for f in attachments if doc_kind(f) in TITLES]
    return ('Приложения: %s; каталог координат; обменные файлы DXF и MIF/MID.'
            % ('; '.join(have) if have else 'схема расположения частей ЗУ'))

def build(path, kn, parts, egrn_ha, result, place='', zone_name='МСК-58, зона 2',
          attachments=(), soil_image=None):
    n = Note()
    s = n.s
    s.d.text((s.PW / 2, n.y), 'Пояснительная записка', font=s.F(4.6, True), fill='black', anchor='ma')
    n.y += s.mm(7.5)
    for t in ('к схеме расположения частей земельного участка %s,' % kn,
              'покрытых древесной и кустарниковой растительностью'):
        s.d.text((s.PW / 2, n.y), t, font=s.F(3.0), fill='black', anchor='ma')
        n.y += s.mm(4.6)
    n.y += s.mm(4.4)

    n.section('Объект')
    n.text('Кадастровый номер:  %s\nМестоположение:  %s\n'
           'Категория:  земли сельскохозяйственного назначения\n'
           'Площадь по сведениям ЕГРН:  %s м² (%s га)\nСистема координат:  %s'
           % (kn, place or '—', fmt_m2(egrn_ha), fmt_ha(egrn_ha), zone_name))
    n.y += s.mm(3)

    n.section('Ведомость частей')
    keys = sorted(parts)
    cols = [n.L, n.L + s.mm(22), n.L + s.mm(112), n.L + s.mm(146), n.R]
    HH = s.mm(8.2)
    s.d.rectangle([cols[0], n.y, cols[4], n.y + HH], outline='black', width=s.W(0.35))
    for c in cols[1:4]:
        s.d.line([c, n.y, c, n.y + HH], fill='black', width=s.W(0.35))
    for i, t in enumerate(('Обозна-\nчение', 'Наименование части', 'Площадь,\nм²', 'Площадь,\nга')):
        s.d.multiline_text(((cols[i] + cols[i + 1]) / 2, n.y + s.mm(1.0)), t, font=s.F(2.5, True),
                           fill='black', anchor='ma', align='center', spacing=s.mm(0.8))
    n.y += HH
    total = 0.0
    for k in keys:
        a = parts[k]['areaHa']; total += a
        hh = s.mm(9.0)
        n.need(15)
        s.d.rectangle([cols[0], n.y, cols[4], n.y + hh], outline='black', width=s.W(0.35))
        for c in cols[1:4]:
            s.d.line([c, n.y, c, n.y + hh], fill='black', width=s.W(0.35))
        s.d.text(((cols[0] + cols[1]) / 2, n.y + s.mm(2.8)), 'ЧЗУ/%s' % k, font=s.F(2.8, True),
                 fill='black', anchor='ma')
        s.d.multiline_text((cols[1] + s.mm(2), n.y + s.mm(1.8)), parts[k].get('название', ''),
                           font=s.F(2.45), fill=(35, 35, 35), spacing=s.mm(1.1))
        s.d.text(((cols[2] + cols[3]) / 2, n.y + s.mm(2.9)), fmt_m2(a), font=s.F(2.6),
                 fill='black', anchor='ma')
        s.d.text(((cols[3] + cols[4]) / 2, n.y + s.mm(2.9)), fmt_ha(a), font=s.F(2.6),
                 fill='black', anchor='ma')
        n.y += hh
    hh = s.mm(6.0)
    s.d.rectangle([cols[0], n.y, cols[4], n.y + hh], outline='black', width=s.W(0.55))
    for c in cols[2:4]:
        s.d.line([c, n.y, c, n.y + hh], fill='black', width=s.W(0.35))
    s.d.text((cols[1] + s.mm(2), n.y + s.mm(1.5)),
             'Итого — площадь земельного участка по сведениям ЕГРН', font=s.F(2.6, True), fill='black')
    s.d.text(((cols[2] + cols[3]) / 2, n.y + s.mm(1.5)), fmt_m2(total), font=s.F(2.6, True),
             fill='black', anchor='ma')
    s.d.text(((cols[3] + cols[4]) / 2, n.y + s.mm(1.5)), fmt_ha(total), font=s.F(2.6, True),
             fill='black', anchor='ma')
    n.y += hh + s.mm(3)
    # части без площади в комплект не попадают — упоминать «0,00 га (ЧЗУ/2)»
    # в записке нечестно: такой части нет
    a1 = parts.get('1', {}).get('areaHa', 0); a2 = parts.get('2', {}).get('areaHa', 0)
    involved = 'ЧЗУ/1 + ЧЗУ/2' if a1 and a2 else ('ЧЗУ/1' if a1 else 'ЧЗУ/2')
    line = ['Под вовлечение в сельскохозяйственный оборот: %s = %s га.'
            % (involved, fmt_ha(a1 + a2))]
    if a1 and a2:
        line.append('Из них требуют раскорчёвки %s га (ЧЗУ/1); %s га (ЧЗУ/2) вовлекаются '
                    'без раскорчёвки.' % (fmt_ha(a1), fmt_ha(a2)))
    elif a1:
        line.append('Вся эта площадь требует раскорчёвки: участок покрыт древесной '
                    'и кустарниковой растительностью целиком.')
    elif a2:
        line.append('Раскорчёвка не требуется: древесной растительности не выявлено.')
    n.text('\n'.join(line))
    n.y += s.mm(3)

    n.section('Исходные материалы')
    sc = result.get('сцены_sentinel', [])
    src = ['• ESRI World Imagery и Clarity, 0,72 и 0,36 м/пиксель — проективное покрытие крон;']
    if sc:
        src.append('• Sentinel-2 L2A, %d сцены %s — NDVI/NDRE/NDMI, медианный композит;'
                   % (len(sc), ', '.join(sorted({x[1][:4] for x in sc}))))
    src += ['• Canopy Height Map (Meta/WRI), ~1 м — высота полога, защитные лесные насаждения;',
            '• КПТ Роскадастра — граница участка, смежные участки, ЗОУИТ.']
    n.text('\n'.join(src))
    n.y += s.mm(3)

    n.section('Как разделены части')
    n.text('Проективное покрытие крон считается в скользящем окне 25 м. Пороги покрытия:\n'
           '10 % — слабое зарастание, 30 % — среднее, 60 % и выше — сильное. В ЧЗУ/1 вошли все\n'
           'три градации: по методике раскорчёвке подлежит любая древесная растительность.\n'
           'Спорные места уточнены по данным, которых на ортофото нет: участки с древесным\n'
           'влагосодержанием (NDMI) и высотой полога от 3 м отнесены к зарастанию, а класс\n'
           '«открытая почва» ставится только при подтверждении по NDVI.\n'
           'Контуры генерализованы: ареалы мельче 0,30 га растворены в окружающем классе.\n'
           'Части не накладываются и разведены по приоритету: ЗОУИТ → лесополосы →\n'
           'древесная → залежь. Лесополоса, попавшая в ЗОУИТ, отнесена к ЧЗУ/3:\n'
           'мероприятия там не проводятся в любом случае.')
    n.y += s.mm(3)

    b = result.get('лесополосы') or {}
    has4 = '4' in parts and parts['4'].get('areaHa', 0) > 0
    if b or has4:
        n.section('Защитные лесные насаждения')
        L = []
        if has4:
            L.append('Состав ЧЗУ/4 принят по разметке правообладателя. Автоматический поиск '
                     'по высоте полога выполнен как проверка: найдено %s полос общей площадью '
                     '%s га, покрытие осевых линий ручной разметки %s %%, точность в зоне '
                     'разметки %s %%.'
                     % (b.get('найдено_полос', '—'), _ru_num(b.get('авто_га', 0)),
                        b.get('покрытие_осевых_линий', '—'),
                        b.get('точность_в_зоне_разметки', '—')))
            if b.get('новых_полос_га'):
                L.append('Дополнительно обнаружено %s га насаждений вне размеченной зоны — '
                         'перечень полос может быть дополнен, при этом площадь ЧЗУ/4 вырастет '
                         'за счёт ЧЗУ/1.' % _ru_num(b['новых_полос_га']))
        else:
            # полос в комплекте нет, но специалист видит облесённый участок:
            # он должен понимать, почему это не защитные насаждения
            L.append('Полезащитные лесные полосы на участке не выделены: разметка '
                     'правообладателя не представлена, а автоматический поиск даёт только '
                     'кандидатов и в состав частей без подтверждения не идёт.')
            if b.get('найдено_полос'):
                k = (b.get('кандидаты') or [{}])[0]
                L.append('Автопоиск по высоте полога: кандидатов %s, площадь %s га%s.'
                         % (b['найдено_полос'], _ru_num(b.get('авто_га', 0)),
                            ', ширина %s м' % _ru_num(k['ширина_по_скелету_м'])
                            if k.get('ширина_по_скелету_м') else ''))
            elif b.get('полог_медиана_м') is not None:
                L.append('Автопоиск кандидатов не дал: высота полога в границах ЗУ — '
                         'медиана %s м, 90-й процентиль %s м, выше 8 м только %s %% площади; '
                         'сплошных рядовых посадок нет.'
                         % (_ru_num(b['полог_медиана_м']), _ru_num(b.get('полог_p90_м', 0)),
                            _ru_num(round(100 * b.get('полог_выше_8м_доля', 0)))))
            r = b.get('ретроспектива')
            if r:
                L.append('Ретроспектива %d года: на месте нынешнего древостоя пар %s га, '
                         'сомкнутого полога %s га — это %s, а не посаженная полоса.'
                         % (r['ранний_год'], _ru_num(r.get('пар_га') or 0),
                            _ru_num(r.get('полог_га') or 0), r['вывод']))
                if 'самосев' in r['вывод']:
                    L.append('Поэтому древесная растительность отнесена к ЧЗУ/1 '
                             '(раскорчёвка), а не к защитным насаждениям.')
        n.text('\n'.join(L))
        n.y += s.mm(3)

    sl = result.get('почвы') or {}
    if sl.get('профиль'):
        rows = sl['профиль']
        top = list(rows.values())[0]
        bot = list(rows.values())[-1]
        hz = list(rows)
        n.section('Почвенная характеристика')
        fr = sl.get('карта_почв') or {}
        L = []
        if fr.get('название'):
            L.append('Тип почвы по Почвенной карте РСФСР 1:2 500 000 (Фридланд и др., 1988):')
            L.append('%s — индекс %s%s%s.'
                     % (fr['название'], fr.get('индекс', '—'),
                        ', код %s' % fr['код'] if fr.get('код') else '',
                        ', почвообразующая порода — %s' % fr['порода'].lower()
                        if fr.get('порода') else ''))
            if fr.get('сопутствующие'):
                L.append('Сопутствующие почвы контура: %s.' % ', '.join(fr['сопутствующие']).lower())
            L.append('Контур карты измеряется километрами и характеризует массив, '
                     'а не участок.')
        w = sl.get('wrb') or {}
        if w.get('wrb'):
            prob = dict((k, v) for k, v in (w.get('вероятности') or []))
            ru = w.get('русское_соответствие') or ''
            L.append('Классификация WRB по SoilGrids: %s%s%s.'
                     % (w['wrb'],
                        ' (%d %%)' % prob[w['wrb']] if prob.get(w['wrb']) else '',
                        ' — ориентировочно %s' % ru if ru else ''))
            if fr.get('название'):
                # согласие проверяем по словам названия: чернозёмы карты должны
                # находиться и в русском соответствии класса WRB
                key = fr['название'].split()[0].lower().replace('ё', 'е')[:7]
                agree = key and key in ru.lower().replace('ё', 'е')
                L.append('Источники согласуются: обе системы указывают на один ряд почв. '
                         'Точного соответствия между ними не бывает — WRB опирается на '
                         'диагностические горизонты, отечественная классификация на генезис.'
                         if agree else
                         'Источники расходятся: карта даёт «%s», модель — «%s». '
                         'Расхождение снимается полевым обследованием.'
                         % (fr['название'].lower(), w['wrb']))
        n.text('\n'.join(L))
        n.y += n.s.mm(2)

        # карта: утверждение о типе почвы должно быть проверяемым —
        # специалист берёт подписанную линию сетки и сверяет сам
        if soil_image is not None:
            n.image(soil_image, w_mm=172)
            n.text('Участок показан красным контуром, во врезке — обзор, красной рамкой '
                   'на нём отмечен кадр крупного плана. Сетка координат — WGS-84; '
                   'цвет заливки и индексы контуров взяты с самой карты.',
                   size=2.4, gap=3.4, color=(90, 90, 90))
            n.y += n.s.mm(1)

        pf = sl.get('разрезы') or {}
        if pf:
            near = pf.get('ближайший')
            same = pf.get('того_же_типа')
            P0 = []
            if near:
                P0.append('Ближайший полевой разрез базы — № %s, %s км%s.'
                          % (near['id'], _ru_num(near['км']),
                             ', %s' % near['тип'].lower() if near.get('тип') else ''))
            if same and (not near or same['id'] != near['id']):
                P0.append('Ближайший разрез того же типа почвы — № %s, %s км.'
                          % (same['id'], _ru_num(same['км'])))
            elif near and not same:
                P0.append('Разрезов того же типа почвы в просмотренном радиусе нет.')
            P0.append('В радиусе 25 км разрезов %s: полевых данных, характеризующих '
                      'этот участок, в открытой базе нет — приведённые значения '
                      'модельные.' % ('нет' if not pf.get('в_радиусе_25км') else
                                      '%d' % pf['в_радиусе_25км']))
            n.text('\n'.join(P0))
            n.y += n.s.mm(2)

        r = lambda v, f='%.1f': (f % v).replace('.', ',')
        P = ['Профиль по данным SoilGrids v2.0 (ISRIC), усреднение по %d точкам внутри участка:'
             % sl.get('точек', 0)]
        for name, v in rows.items():
            P.append('%s — глина %s %%, гумус %s %%, pH %s, плотность %s г/см³.'
                     % (name, r(v['clay']), r(v['humus']), r(v['phh2o']),
                        r(v.get('bdod', 0), '%.2f')))
        st = sl.get('слои') or {}
        if st:
            got = []
            for key, lab, unit in (('humus', 'гумус', ' %'), ('clay', 'глина', ' %'),
                                   ('phh2o', 'pH', '')):
                if key in st:
                    got.append('%s %s…%s%s' % (lab, r(st[key][0]), r(st[key][2]), unit))
            P.append('Разброс в границах участка по растровым слоям (ячейка 250 м): %s.'
                     % ', '.join(got))
        n.text('\n'.join(P))
        n.y += n.s.mm(2)

        cz = [h for h, _, _ in (sl.get('выводы') or [])]
        if cz:
            n.text('Выводы для вовлечения в оборот:\n' + '\n'.join('• ' + t for t in cz))
            n.y += n.s.mm(2)
        n.text('SoilGrids — модельное предсказание с шагом 250 м, а не полевая съёмка; '
               'гумус в слое 0–5 см завышен: модель относит к нему лесную подстилку.\n'
               'Для проектной документации требуется отбор проб аккредитованной лабораторией.\n'
               'Профиль по горизонтам, тип почвы и карта распределения показателей — '
               'в приложении «Почвенная характеристика».', size=2.5, color=(70, 70, 70))
        n.y += n.s.mm(3)

    fr = result.get('лесничество') or {}
    if fr.get('список'):
        n.section('Земли лесного фонда')
        L = ['Проверено по КПТ: %s.'
             % '; '.join('%s (%s)' % (f['наименование'], f['номер']) for f in fr['список'])]
        if fr.get('исключено_га'):
            L.append('Наложение на участок — %s га; эта площадь исключена из рабочей '
                     'и в части ЧЗУ не входит.' % _ru_num(fr['исключено_га']))
        else:
            L.append('Наложения на участок нет: границы соприкасаются, но контуры '
                     'лесничества за границу ЗУ не заходят.')
        L.append('Координаты частей проверены на пересечение с лесничеством — '
                 'см. раздел контроля.')
        n.text('\n'.join(L))
        n.y += n.s.mm(2)

    qa = result.get('qa', {})
    if qa:
        n.section('Контроль')
        n.text('Все проверки геометрии пройдены.' if qa.get('пройдено')
               else 'ВНИМАНИЕ: часть проверок не пройдена.', size=2.7,
               color=(20, 90, 20) if qa.get('пройдено') else (160, 0, 0))
        lines = []
        for r in qa.get('проверки', []):
            # галочки в Liberation Serif нет — пишем словом, иначе в PDF пусто
            lines.append('%s%s — измерено %s при допуске %s'
                         % ('' if r['пройдена'] else 'НЕ ПРОЙДЕНА — ', r['проверка'],
                            str(r['измерено']).replace('.', ','), r['допуск']))
        n.text('\n'.join(lines))
        n.y += s.mm(3)

    n.section('Принятые допущения')
    n.text('1. Контуры ЧЗУ/4 обведены правообладателем; автоматический поиск даёт кандидатов\n'
           '   и в состав части не входит без его подтверждения.\n'
           '2. Границы ЗОУИТ приняты по сведениям КПТ, а не дешифрированы по снимку;\n'
           '   на местности они ничем не обозначены и подлежат сверке по выписке.\n'
           '3. Карта высот полога снята ранее даты оптической съёмки, поэтому используется\n'
           '   как признак взрослых насаждений, а не для датировки зарастания.\n'
           '4. Контуры получены по данным дистанционного зондирования и подлежат\n'
           '   подтверждению при обследовании на местности.')
    n.y += s.mm(4)
    n.text(attachments_line(attachments), size=2.5, color=(60, 60, 60))
    return n.save(path)
