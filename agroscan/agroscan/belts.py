# -*- coding: utf-8 -*-
"""Защитные лесные насаждения по высоте полога.

Шесть признаков по ортофото (яркостный хребет, геометрия, Хаф, текстура
рядов, сезонный NDVI, спрямление осей) границ не дали: на снимке взрослая
полоса и молодой самосев одного цвета. Разделяет их высота.

Замер на 58:24:0341802:1173 против ручной разметки правообладателя:
    внутри полос      медиана 8 м, p75 11 м, доля пикселей ≥6 м — 66 %
    вне полос         медиана 0 м, p75  3 м, доля пикселей ≥6 м —  8 %
    осевые линии ручных полос покрыты автопоиском на 84 %
    точность в зоне разметки — 78 %
Автопоиск даёт кандидатов; правообладатель подтверждает и правит.
"""
import numpy as np
from scipy import ndimage
from skimage.measure import label, regionprops
from skimage.morphology import skeletonize

from .rings import disk

def width_m(sub, mpp):
    """Типичная ширина области: площадь, делённая на длину её осевой линии.

    Вытянутость по осям эллипса ломается на разветвлённых полосах: на
    58:17:0130701:29 Y-образная фигура давала вытянутость 1,5 и
    отбрасывалась. Расстояние до края тоже не годится — у квадрата
    100 × 100 м оно даёт «30 м», как у полосы. Площадь на длину скелета
    для полосы шириной w и длиной L даёт ровно w, а компактную куртину
    честно показывает широкой.
    """
    n = int(sub.sum())
    if not n:
        return 0.0
    sk = skeletonize(sub).sum()
    if sk < 1:
        return float(np.sqrt(n) * mpp)
    return float(n * mpp * mpp / (sk * mpp))

def detect(chm, mask, mpp, h_min=8.0, elong=3.0, min_ha=0.10, close_m=8.0, open_m=3.0,
           w_max=40.0):
    """Кандидаты в защитные лесные полосы.

    h_min  — высота взрослого насаждения (ниже — самосев на залежи);
    elong  — вытянутость (большая ось к малой): полоса линейна, куртина нет;
    w_max  — предельная медианная ширина: разветвлённая полоса вытянутость
             не набирает, но остаётся узкой.
    """
    m = (np.nan_to_num(chm) >= h_min) & mask
    m = ndimage.binary_closing(m, disk(max(1, int(close_m / mpp))))
    m = ndimage.binary_opening(m, disk(max(1, int(open_m / mpp)))) & mask
    cell = mpp * mpp / 10000
    lb = label(m); out = np.zeros_like(m)
    kept = []
    for r in regionprops(lb):
        if r.area * cell < min_ha:
            continue
        minor = max(r.axis_minor_length, 1e-6)
        e = r.axis_major_length / minor
        w = width_m(lb == r.label, mpp)
        by = 'вытянутость' if e >= elong else ('ширина' if w <= w_max else None)
        if by is None:
            continue
        out |= (lb == r.label)
        kept.append({'га': round(r.area * cell, 3), 'вытянутость': round(e, 1),
                     'длина_м': round(r.axis_major_length * mpp),
                     'ширина_м': round(minor * mpp, 1),
                     'ширина_по_скелету_м': round(w, 1), 'признак': by})
    return out, kept

def compare(auto, manual, mpp, near_m=60.0):
    """Сверка автопоиска с ручной разметкой.

    IoU здесь мало о чём говорит: правообладатель обводит полосы с запасом и
    размечает не все, поэтому меряем то, что имеет смысл — покрытие осевых
    линий и точность в зоне, где разметка вообще велась.
    """
    cell = mpp * mpp / 10000
    near = ndimage.binary_dilation(manual, disk(max(1, int(near_m / mpp))))
    a_in = auto & near
    sk = skeletonize(manual)
    cov = (ndimage.binary_dilation(auto, disk(max(1, int(6 / mpp)))) & sk).sum() / max(sk.sum(), 1)
    return {'авто_га': round(auto.sum() * cell, 2),
            'ручная_га': round(manual.sum() * cell, 2),
            'новых_полос_га': round((auto & ~near).sum() * cell, 2),
            'точность_в_зоне_разметки': round(100 * (a_in & manual).sum() / max(a_in.sum(), 1)),
            'покрытие_осевых_линий': round(100 * cov),
            'iou': round((auto & manual).sum() / max((auto | manual).sum(), 1), 2)}
