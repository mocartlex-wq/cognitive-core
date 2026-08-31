# -*- coding: utf-8 -*-
"""Пояснительная записка — лист, который специалист читает первым.

Содержание собирается из результата расчёта: ведомость частей, источники,
методика, отчёт проверок, допущения. Числа не набиваются руками, поэтому
записка не может разойтись со схемой.
"""
from ..sheet import Sheet, fmt_ha, fmt_m2

class Note:
    """Многостраничный текстовый лист с автоматическим переносом."""

    def __init__(self, w_mm=210, h_mm=297, dpi=300):
        self.proto = Sheet(w_mm, h_mm, dpi=dpi, ss=2, margin_mm=10)
        self.w_mm, self.h_mm, self.dpi = w_mm, h_mm, dpi
        self.pages = []
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

    def text(self, t, size=2.65, gap=3.6, color=(35, 35, 35)):
        self.need(gap * (t.count('\n') + 1))
        self.s.d.multiline_text((self.L, self.y), t, font=self.s.F(size), fill=color,
                                spacing=self.s.mm(1.35))
        self.y += self.s.mm(gap) * (t.count('\n') + 1)

    def save(self, path):
        for i, p in enumerate(self.pages, 1):
            p.d.text((p.PW / 2, p.PH - p.mm(15)), '— %d —' % i, font=p.F(2.5),
                     fill=(90, 90, 90), anchor='ma')
        return self.pages[0].save(path, extra_pages=self.pages[1:])

def build(path, kn, parts, egrn_ha, result, place='', zone_name='МСК-58, зона 2'):
    n = Note()
    s = n.s
    s.d.text((s.PW / 2, n.y), 'Пояснительная записка', font=s.F(4.6, True), fill='black', anchor='ma')
    n.y += s.mm(7.5)
    for t in ('к схеме расположения частей земельного участка %s,' % kn,
              'покрытых древесной и кустарниковой растительностью'):
        s.d.text((s.PW / 2, n.y), t, font=s.F(3.0), fill='black', anchor='ma')
        n.y += s.mm(4.6)
    n.y += s.mm(4.4)

    n.head('1. Объект')
    n.text('Кадастровый номер:  %s\nМестоположение:  %s\n'
           'Категория:  земли сельскохозяйственного назначения\n'
           'Площадь по сведениям ЕГРН:  %s м² (%s га)\nСистема координат:  %s'
           % (kn, place or '—', fmt_m2(egrn_ha), fmt_ha(egrn_ha), zone_name))
    n.y += s.mm(3)

    n.head('2. Ведомость частей')
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
    a1 = parts.get('1', {}).get('areaHa', 0); a2 = parts.get('2', {}).get('areaHa', 0)
    n.text(('Под вовлечение в сельскохозяйственный оборот: ЧЗУ/1 + ЧЗУ/2 = %s га.\n'
            'Из них требуют раскорчёвки %s га (ЧЗУ/1); %s га (ЧЗУ/2) вовлекаются без раскорчёвки.'
            % (fmt_ha(a1 + a2), fmt_ha(a1), fmt_ha(a2))))
    n.y += s.mm(3)

    n.head('3. Исходные материалы')
    sc = result.get('сцены_sentinel', [])
    src = ['• ESRI World Imagery и Clarity, 0,72 и 0,36 м/пиксель — проективное покрытие крон;']
    if sc:
        src.append('• Sentinel-2 L2A, %d сцены %s — NDVI/NDRE/NDMI, медианный композит;'
                   % (len(sc), ', '.join(sorted({x[1][:4] for x in sc}))))
    src += ['• Canopy Height Map (Meta/WRI), ~1 м — высота полога, защитные лесные насаждения;',
            '• КПТ Роскадастра — граница участка, смежные участки, ЗОУИТ.']
    n.text('\n'.join(src))
    n.y += s.mm(3)

    n.head('4. Как разделены части')
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
    if b:
        n.head('5. Защитные лесные насаждения')
        n.text('Состав ЧЗУ/4 принят по разметке правообладателя. Автоматический поиск по высоте\n'
               'полога выполнен как проверка: найдено %s полос общей площадью %s га, покрытие\n'
               'осевых линий ручной разметки %s %%, точность в зоне разметки %s %%.\n'
               'Дополнительно обнаружено %s га насаждений вне размеченной зоны — перечень\n'
               'полос может быть дополнен, при этом площадь ЧЗУ/4 вырастет за счёт ЧЗУ/1.'
               % (b.get('найдено_полос', '—'), str(b.get('авто_га', '—')).replace('.', ','),
                  b.get('покрытие_осевых_линий', '—'), b.get('точность_в_зоне_разметки', '—'),
                  str(b.get('новых_полос_га', '—')).replace('.', ',')))
        n.y += s.mm(3)

    qa = result.get('qa', {})
    if qa:
        n.head('6. Контроль')
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

    n.head('7. Принятые допущения')
    n.text('1. Контуры ЧЗУ/4 обведены правообладателем; автоматический поиск даёт кандидатов\n'
           '   и в состав части не входит без его подтверждения.\n'
           '2. Границы ЗОУИТ приняты по сведениям КПТ, а не дешифрированы по снимку;\n'
           '   на местности они ничем не обозначены и подлежат сверке по выписке.\n'
           '3. Карта высот полога снята ранее даты оптической съёмки, поэтому используется\n'
           '   как признак взрослых насаждений, а не для датировки зарастания.\n'
           '4. Контуры получены по данным дистанционного зондирования и подлежат\n'
           '   подтверждению при обследовании на местности.')
    n.y += s.mm(4)
    n.text('Приложения: схема расположения частей ЗУ; проверочная карта без заливки;\n'
           'каталог координат; обменные файлы DXF и MIF/MID.', size=2.5, color=(60, 60, 60))
    return n.save(path)
