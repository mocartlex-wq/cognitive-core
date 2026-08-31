# -*- coding: utf-8 -*-
"""Типографика листа: формат, шрифты, поле карты, штриховки, раскладка подписей.

Собрано из шестнадцати файлов, где MM=DPI/25.4*SS и F()/W() были скопированы
по месту. Лист рисуется в удвоенном разрешении (SS) и уменьшается при
сохранении — так линии в 0,2 мм не рассыпаются.

Карта рисуется в отдельный слой размером с поле карты и вставляется в
страницу: всё, что выходит за внутреннюю рамку (сеть смежных участков),
обрезается автоматически, а не лезет на поля.
"""
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageChops

Image.MAX_IMAGE_PIXELS = None
SERIF = '/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf'
SERIF_B = '/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf'

# цвета, общие для всех листов комплекта
BLUE = (0, 0, 205)        # ЧЗУ/1 — древесная под раскорчёвку
GREEN = (0, 140, 0)       # ЧЗУ/2 — залежь
MAG = (230, 0, 190)       # ЧЗУ/3 — ЗОУИТ
OLIVE = (95, 75, 15)      # ЧЗУ/4 — защитные лесные полосы
ZUG = (0, 176, 80)        # граница ЗУ по сведениям ЕГРН
RED = (210, 0, 0)         # смежные участки
PART_COLOR = {'1': BLUE, '2': GREEN, '3': MAG, '4': OLIVE}
PART_HATCH = {'1': +1, '2': -1, '4': 0}          # диагональ вправо/влево/вертикаль

class Sheet:
    """Лист заданного формата с внутренней рамкой и полем карты."""

    def __init__(self, w_mm=297, h_mm=210, dpi=400, ss=2, margin_mm=7, title_mm=13):
        self.DPI, self.SS = dpi, ss
        self.MM = dpi / 25.4 * ss
        self.PW, self.PH = int(w_mm * self.MM), int(h_mm * self.MM)
        self.page = Image.new('RGB', (self.PW, self.PH), 'white')
        self.d = ImageDraw.Draw(self.page)
        self.margin, self.title_h = int(margin_mm * self.MM), int(title_mm * self.MM)
        self.IN0, self.IN1 = self.margin, self.PW - self.margin

    # ── единицы ────────────────────────────────────────────────────────
    def mm(self, v):
        return int(v * self.MM)

    def F(self, size, bold=False):
        return ImageFont.truetype(SERIF_B if bold else SERIF, int(size * 1.08 * self.MM))

    def W(self, mm_width):
        """Толщина линии, заданная в миллиметрах."""
        return max(1, int(round(mm_width * self.MM)))

    # ── рамки ──────────────────────────────────────────────────────────
    def frame(self, outer_mm=5, inner=True):
        self.d.rectangle([self.mm(outer_mm), self.mm(outer_mm),
                          self.PW - self.mm(outer_mm), self.PH - self.mm(outer_mm)],
                         outline='black', width=self.W(0.30))
        if inner:
            self.d.rectangle([self.IN0, self.margin, self.IN1, self.PH - self.margin],
                             outline='black', width=self.W(0.75))

    def title(self, lines, size=3.1):
        """Заголовок в рамке под верхней кромкой."""
        self.d.rectangle([self.IN0, self.margin, self.IN1, self.margin + self.title_h],
                         outline='black', width=self.W(0.5))
        y = self.margin + self.mm(2.6)
        for t in lines:
            self.d.text((self.PW / 2, y), t, font=self.F(size), fill='black', anchor='ma')
            y += self.mm(4.8)

    # ── поле карты ─────────────────────────────────────────────────────
    def map_field(self, rings, pad_m=260, shift_mm=0, gap_mm=2):
        """Завести слой карты и функцию проекции координат в его пиксели."""
        self.MX0 = self.IN0 + self.mm(gap_mm)
        self.MY0 = self.margin + self.title_h + self.mm(gap_mm)
        self.MX1, self.MY1 = self.IN1 - self.mm(gap_mm), self.PH - self.margin - self.mm(gap_mm)
        self.MW, self.MH = self.MX1 - self.MX0, self.MY1 - self.MY0
        E = [p[0] for r in rings for p in r]; N = [p[1] for r in rings for p in r]
        e0, e1 = min(E) - pad_m, max(E) + pad_m
        n0, n1 = min(N) - pad_m, max(N) + pad_m
        sc = min(self.MW / (e1 - e0), self.MH / (n1 - n0))
        offx = (self.MW - (e1 - e0) * sc) / 2 - self.mm(shift_mm)
        offy = (self.MH - (n1 - n0) * sc) / 2
        self.scale = sc
        self.P = lambda e, n: (offx + (e - e0) * sc, offy + (n1 - n) * sc)
        self.mp = Image.new('RGB', (self.MW, self.MH), 'white')
        self.md = ImageDraw.Draw(self.mp)
        self.blocks = []          # зоны врезок: там подписи не ставим
        self.placed = []          # уже поставленные подписи
        return self.P

    def denominator(self):
        """Знаменатель масштаба карты: 1 : N.

        scale — пикселей слоя на метр местности; слой уменьшается в SS раз,
        печатается с плотностью DPI, дюйм — 0,0254 м.
        """
        return int(round(self.SS * self.DPI / (self.scale * 0.0254)))

    def block(self, x0, y0, x1, y1):
        self.blocks.append((x0, y0, x1, y1))

    def free(self, x0, y0, x1, y1):
        m = self.mm(1.5)
        if x0 < m or y0 < m or x1 > self.MW - m or y1 > self.MH - m:
            return False
        for b in self.blocks + self.placed:
            if not (x1 < b[0] or x0 > b[2] or y1 < b[1] or y0 > b[3]):
                return False
        return True

    def label(self, x, y, text, font, fill='black', halo='white', pad_mm=0.7,
              offsets=((0, 0), (0, -3.2), (0, 3.2), (-7, 0), (7, 0), (0, -6.4), (0, 6.4))):
        """Подпись с проверкой на пересечение с уже поставленными.

        Без этого на схеме 1173 две подписи ЧЗУ/4 налезали друг на друга и на
        рамку кадастрового номера.
        """
        for dx, dy in offsets:
            xx, yy = x + dx * self.MM, y + dy * self.MM
            b = self.md.textbbox((xx, yy), text, font=font, anchor='mm')
            p = self.mm(pad_mm)
            b = (b[0] - p, b[1] - p, b[2] + p, b[3] + p)
            if self.free(*b):
                self.md.text((xx, yy), text, font=font, fill=fill, anchor='mm',
                             stroke_width=self.W(0.5) if halo else 0, stroke_fill=halo)
                self.placed.append(b)
                return True
        return False

    def reserve(self, box):
        self.placed.append(tuple(box))

    # ── штриховка ──────────────────────────────────────────────────────
    def _pattern(self, diag, step_mm=4.0, width_mm=0.26):
        step = self.W(step_mm); wdt = self.W(width_mm)
        p = Image.new('L', (self.MW, self.MH), 0); pd = ImageDraw.Draw(p)
        if diag > 0:
            for c in range(0, self.MW + self.MH, step):
                pd.line([(c, 0), (c - self.MH, self.MH)], fill=255, width=wdt)
        elif diag < 0:
            for c in range(-self.MH, self.MW, step):
                pd.line([(c, 0), (c + self.MH, self.MH)], fill=255, width=wdt)
        else:
            for c in range(0, self.MW, int(step * 0.8)):
                pd.line([(c, 0), (c, self.MH)], fill=255, width=wdt)
        return p

    def hatch(self, outer, inner, color, diag):
        if not hasattr(self, '_pat'):
            self._pat = {}
        if diag not in self._pat:
            self._pat[diag] = self._pattern(diag)
        m = Image.new('L', (self.MW, self.MH), 0); ld = ImageDraw.Draw(m)
        for r in outer:
            ld.polygon([self.P(*p) for p in r], fill=255)
        for r in inner:
            ld.polygon([self.P(*p) for p in r], fill=0)
        self.mp.paste(Image.new('RGB', (self.MW, self.MH), color), (0, 0),
                      ImageChops.multiply(m, self._pat[diag]))

    def polyline(self, rings, color, width_mm):
        for r in rings:
            self.md.line([self.P(*p) for p in r] + [self.P(*r[0])],
                         fill=color, width=self.W(width_mm))

    # ── завершение ─────────────────────────────────────────────────────
    def paste_map(self):
        self.page.paste(self.mp, (self.MX0, self.MY0))

    def save(self, path, extra_pages=()):
        """extra_pages — другие Sheet (или готовые Image) для многостраничного PDF."""
        img = self.page.resize((self.PW // self.SS, self.PH // self.SS), Image.LANCZOS)
        rest = []
        for p in extra_pages:
            im = p.page if isinstance(p, Sheet) else p
            rest.append(im.resize((im.width // self.SS, im.height // self.SS), Image.LANCZOS))
        img.save(path, 'PDF', resolution=self.DPI, save_all=bool(rest), append_images=rest)
        return path

def fmt_m2(ha):
    """Площадь в квадратных метрах с неразрывными пробелами по разрядам."""
    return format(int(round(ha * 10000)), ',d').replace(',', ' ')

def fmt_ha(ha):
    return ('%.2f' % ha).replace('.', ',')
