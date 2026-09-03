# -*- coding: utf-8 -*-
"""Сборка инструмента разметки: HTML со встроенными подложками и слоями.

Правообладатель обводит объекты по снимку, правит вершины и передаёт
результат обратно. Инструмент несамостоятелен по данным: подложки, границы,
контуры расчёта и кандидаты автопоиска приходят отсюда.

Разметка один раз уже терялась, поэтому сохранений три и они независимы:
черновик в localStorage на каждое действие, публикация лёгкой квитанции
(копия инструмента с подложками весит мегабайты) и выгрузка GeoJSON файлом.
"""
import base64
import json
import os

import numpy as np
from PIL import Image

Image.MAX_IMAGE_PIXELS = None
TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates', 'tool.html')

TYPES = [
    {'id': 'belt',   'name': 'Лесополоса',   'col': '#ff6b2c', 'chzu': 'ЧЗУ/4'},
    {'id': 'gully',  'name': 'Овраг',        'col': '#c9821f', 'chzu': 'овраг'},
    {'id': 'arable', 'name': 'Пашня',        'col': '#e3c53f', 'chzu': 'пашня'},
    {'id': 'forest', 'name': 'Лесной фонд',  'col': '#3f9d5e', 'chzu': 'ЧЗУ/5'},
    {'id': 'pad',    'name': 'Площадка',     'col': '#49a6d6', 'chzu': 'ЧЗУ/6'},
]

def _b64_jpeg(img, max_px=1600, quality=82):
    """Подложка ужимается: data-URI больше мегабайта артефакт не отображает."""
    w, h = img.size
    k = min(1.0, max_px / max(w, h))
    if k < 1.0:
        img = img.resize((int(w * k), int(h * k)), Image.LANCZOS)
    from io import BytesIO
    buf = BytesIO(); img.convert('RGB').save(buf, 'JPEG', quality=quality, optimize=True)
    return base64.b64encode(buf.getvalue()).decode()

# Палитры для производных слоёв: серая шкала годится для полога, но высоты
# и уклон по ней читаются плохо — форма рельефа теряется в полутонах.
CMAPS = {
    'hyps': ((0.00, (60, 110, 70)), (0.35, (160, 165, 95)), (0.65, (170, 130, 80)),
             (0.85, (150, 110, 95)), (1.00, (240, 240, 240))),
    'slope': ((0.00, (70, 130, 80)), (0.35, (215, 205, 90)), (0.65, (215, 140, 60)),
              (1.00, (190, 60, 50))),
    'hollow': ((0.00, (150, 110, 70)), (0.45, (245, 245, 240)), (0.60, (170, 200, 225)),
               (1.00, (30, 90, 170))),
}

def _ramp(a, lo, hi, gamma=1.0, cmap=None):
    """Массив → изображение с обрезкой по диапазону; cmap — имя палитры."""
    v = np.clip((np.nan_to_num(a, nan=lo) - lo) / max(hi - lo, 1e-6), 0, 1) ** gamma
    stops = CMAPS.get(cmap)
    if not stops:
        return Image.fromarray((v * 255).astype(np.uint8))
    rgb = np.zeros(v.shape + (3,), np.float32)
    for (p0, c0), (p1, c1) in zip(stops, stops[1:]):
        m = (v >= p0) & (v <= p1)
        if not m.any():
            continue
        t = ((v[m] - p0) / max(p1 - p0, 1e-6))[:, None]
        rgb[m] = np.array(c0, np.float32) * (1 - t) + np.array(c1, np.float32) * t
    return Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8))

def tiles_from(backdrops, rasters=None, max_px=1600):
    """{ключ: {data, caption}} — подложки для переключателя.

    backdrops — [(путь, подпись)]; rasters — [(ключ, массив, подпись, lo, hi)]
    для производных слоёв (высота полога, индексы).
    """
    out = {}
    for i, (path, caption) in enumerate(backdrops):
        if not os.path.exists(path):
            continue
        out['bd%d' % i] = {'data': _b64_jpeg(Image.open(path), max_px), 'caption': caption}
    for item in (rasters or []):
        key, arr, caption, lo, hi = item[:5]
        cmap = item[5] if len(item) > 5 else None
        if arr is None:
            continue
        out[key] = {'data': _b64_jpeg(_ramp(arr, lo, hi, cmap=cmap), max_px), 'caption': caption}
    return out

def build(path, kn, rings, tiles, zouit=(), chzu=None, candidates=(), saved=None,
          meta=None, header=None, types=None, saved_at=0):
    """Собрать HTML инструмента.

    meta — {e0,e1,n0,n1} местной СК; saved — объекты разметки как в received.json
    (кольца в местной СК, не в пикселях: размер подложки меняется при пережатии);
    saved_at — время передачи в мс, чтобы отличить её от свежего черновика в браузере.
    """
    tpl = open(TEMPLATE, encoding='utf-8').read()
    ph = lambda k: '__' + k + '__'          # плейсхолдеры подставляются по одному:
    # прямой replace по всему тексту однажды залез внутрь JS и раздул файл вдвое
    body = (tpl.replace(ph('META'), json.dumps(meta))
               .replace(ph('RINGS'), json.dumps(rings))
               .replace(ph('ZOUIT'), json.dumps(list(zouit)))
               .replace(ph('CHZU'), json.dumps(chzu or {}))
               .replace(ph('CAND'), json.dumps(list(candidates)))
               .replace(ph('PARCEL'), json.dumps(kn))
               .replace(ph('TYPES'), json.dumps(types or TYPES, ensure_ascii=False))
               .replace(ph('TITLE'), 'Разметка контуров %s' % kn)
               .replace(ph('HDR'), json.dumps(header or ('ЗУ %s' % kn))))
    head = ('<!doctype html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1"></head><body>')
    body = body.replace(ph('SAVED_AT'), json.dumps(saved_at or 0))
    shell = head + body.replace(ph('SHELL'), '').replace(ph('TILES'), '{}') \
                       .replace(ph('SAVED'), 'null').replace(ph('PAYLOAD'), 'null') + '</body></html>'
    html = (body.replace(ph('SHELL'), base64.b64encode(shell.encode()).decode())
                .replace(ph('TILES'), json.dumps(tiles))
                .replace(ph('SAVED'), json.dumps(saved))
                .replace(ph('PAYLOAD'), 'null'))
    open(path, 'w', encoding='utf-8').write(html)
    return path, len(html)

def read_markup(path_or_obj, kinds=('belt',)):
    """Разметка правообладателя → кольца и вырезы нужных типов."""
    d = path_or_obj if isinstance(path_or_obj, dict) else json.load(open(path_or_obj))
    objs = [o for o in d.get('objects', []) if o.get('type', 'belt') in kinds]
    return [o['ring'] for o in objs], [h for o in objs for h in o.get('holes', [])]
