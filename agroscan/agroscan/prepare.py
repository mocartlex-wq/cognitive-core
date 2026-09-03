# -*- coding: utf-8 -*-
"""Подготовка участка из КПТ: от XML до готового конфига одной командой.

Раньше это была ручная работа: выковырять границы, собрать смежников,
построить маску ЗОУИТ неизвестно чем, скачать подложку, руками написать
конфиг. На сотне участков такой шаг съедает больше времени, чем сам расчёт.
"""
import json
import os

import numpy as np

from . import kpt
from .geo import Grid, Local, bbox_of
from .rings import rasterize
from .sources import basemap

# Зоны, накрывающие участок целиком, из ЧЗУ/3 исключаются: приаэродромные
# подзоны ограничивают высотное строительство, а не сельхозработы, и
# формально накрывают всё. Проверено на 1173: без них маска ЗОУИТ совпала
# с прежней, построенной вручную, пиксель в пиксель (41,438 га, IoU 1,000).
WIDE_SHARE = 0.95

PARTS = {'1': 'покрыта древесной и кустарниковой растительностью (раскорчёвка)',
         '2': 'не обработана, травяная и кустарниковая растительность (залежь)',
         '3': 'зона с особыми условиями использования территории',
         '4': 'защитные лесные насаждения (полезащитные лесные полосы)'}

CONFIG = """# Участок %(kn)s — подготовлено из КПТ %(kpt)s
kn: "%(kn)s"
egrn_ha: %(ha).4f
zone: %(zone)s
place: "%(place)s"
zone_name: "%(zone_name)s"
вид_работ: "анализ зарастания"   # попадает в имена файлов комплекта

rings: ../data/%(tag)s/rings.json
meta:  ../data/%(tag)s/bgmeta.json
image: ../data/%(tag)s/bg_summer.jpg
zouit: ../data/%(tag)s/zouit.npy
forest: ../data/%(tag)s/forest.npy      # лесничества из КПТ — части туда не заходят
neighbors: ../data/%(tag)s/neighbors.json
%(markup)s

cover_all: %(cover)s

parts:
%(parts)s
priority: ["3", "4", "1", "2"]

belts:
  source: manual

backdrops:
  - path: ../data/%(tag)s/bg_summer.jpg
    caption: "ESRI World Imagery, лето, %(mpp).2f м/пиксель"
    note: "подложка, по которой считаются кроны"
  - path: ../data/%(tag)s/bg_clarity.jpg
    caption: "ESRI Clarity, межсезонье"
    note: "листва частично облетела — видна структура посадок"
fragment_m: 540

export:
  simplify_m: 0.0
"""

def run(kpt_path, kn, root=None, margin_m=130, mpp=0.60, zoom=17, place='',
        cover_all=True, layers=('summer', 'clarity'), verbose=True):
    root = root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tag = kn.replace(':', '-')
    ddir = os.path.join(root, 'data', tag)
    os.makedirs(ddir, exist_ok=True)
    say = (lambda m: print('   ' + m, flush=True)) if verbose else (lambda m: None)

    parcels, zones, sk = kpt.parse(kpt_path)
    say('КПТ: участков %d, зон %d, система координат %s' % (len(parcels), len(zones), sk))
    rec = kpt.find(parcels, kn)
    if rec is None:
        raise ValueError('участок %s в КПТ не найден' % kn)
    zone = kpt.zone_of(rec.get('sk_id') or sk, kn=kn, rings=rec['кольца'])
    rings = rec['кольца']
    ha_xy = kpt.area_of(rings) / 1e4
    ha = (rec['площадь_егрн'] or kpt.area_of(rings)) / 1e4
    d = abs(ha_xy - ha) / max(ha, 1e-9) * 100
    say('участок: колец %d, площадь ЕГРН %.4f га, по координатам %.4f га (расхождение %.3f %%)'
        % (len(rings), ha, ha_xy, d))
    if d > 0.5:
        say('ВНИМАНИЕ: площадь по координатам расходится с ЕГРН больше чем на 0,5 %')
    json.dump(rings, open(os.path.join(ddir, 'rings.json'), 'w'))

    bbox = bbox_of(rings, margin_m)
    loc = Local(zone)
    nb = kpt.neighbours(parcels, (bbox[0], bbox[1], bbox[2], bbox[3]), kn, margin=300)
    json.dump(nb, open(os.path.join(ddir, 'neighbors.json'), 'w'))
    say('смежников в габарите: %d' % len(nb))

    meta = None
    for lay in layers:
        img, meta = basemap.fetch(bbox, loc, layer=lay, zoom=zoom, mpp=mpp, verbose=verbose)
        img.save(os.path.join(ddir, 'bg_%s.jpg' % lay), quality=92)
        say('подложка %s: %d×%d пикселей' % (lay, meta['W'], meta['H']))
    meta['zone'] = zone
    json.dump(meta, open(os.path.join(ddir, 'bgmeta.json'), 'w'))

    grid = Grid(meta, zone)
    mask = rasterize([rings[0]], grid, rings[1:])
    cell = meta['mpp'] ** 2 / 1e4
    total = mask.sum() * cell
    zn = np.zeros_like(mask)
    kept, wide = [], []
    for z in kpt.zouit(zones):
        m = rasterize(z['кольца'], grid) & mask
        if not m.any():
            continue
        share = m.sum() * cell / max(total, 1e-9)
        (wide if share >= WIDE_SHARE else kept).append(
            {'номер': z['реестровый_номер'], 'наименование': z['наименование'] or z['тип'],
             'га': round(m.sum() * cell, 3), 'доля': round(share, 3)})
        if share < WIDE_SHARE:
            zn |= m
    np.save(os.path.join(ddir, 'zouit.npy'), zn)
    json.dump({'учтено': kept, 'исключено_накрывают_целиком': wide},
              open(os.path.join(ddir, 'zouit.json'), 'w'), ensure_ascii=False, indent=1)
    say('ЗОУИТ: учтено зон %d (%.3f га), исключено накрывающих целиком %d'
        % (len(kept), zn.sum() * cell, len(wide)))
    for w in wide:
        say('   исключена: %s' % w['наименование'][:70])

    # Лесничества: чужая категория земель. Считаем наложение на участок и
    # кладём слой рядом с ЗОУИТ — координаты частей туда заходить не должны.
    fr = np.zeros_like(mask)
    fr_list = []
    for z in kpt.forests(zones):
        m = rasterize(z['кольца'], grid) & mask
        fr_list.append({'номер': z['реестровый_номер'],
                        'наименование': z['наименование'] or z['тип'],
                        'га': round(m.sum() * cell, 4)})
        fr |= m
    np.save(os.path.join(ddir, 'forest.npy'), fr)
    json.dump({'лесничества': fr_list, 'наложение_га': round(fr.sum() * cell, 4)},
              open(os.path.join(ddir, 'forest.json'), 'w'), ensure_ascii=False, indent=1)
    if fr_list:
        say('лесничества в КПТ: %d, наложение на участок %.4f га'
            % (len(fr_list), fr.sum() * cell))

    cfg_path = os.path.join(root, 'parcels', tag + '.yaml')
    os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
    parts = ''.join('  "%s": "%s"\n' % (k, v) for k, v in PARTS.items()
                    if k != '3' or zn.any())
    # разметка правообладателя, если её уже присылали, подхватывается сама
    mk = os.path.join(ddir, 'markup.json')
    markup = ('markup: ../data/%s/markup.json' % tag if os.path.exists(mk)
              else '# markup: ../data/%s/markup.json   # разметка правообладателя, когда появится' % tag)
    open(cfg_path, 'w', encoding='utf-8').write(CONFIG % {
        'kn': kn, 'ha': ha, 'zone': zone, 'tag': tag, 'mpp': mpp,
        'kpt': os.path.basename(kpt_path), 'place': place,
        'zone_name': zone.upper().replace('MSK', 'МСК-').replace('-', ' зона ', 1)
        if zone.startswith('msk') else zone,
        'cover': 'true' if cover_all else 'false', 'parts': parts, 'markup': markup})
    say('конфиг: %s' % cfg_path)
    return {'конфиг': cfg_path, 'данные': ddir, 'зона': zone, 'площадь_га': ha,
            'смежников': len(nb), 'зоуит_га': round(zn.sum() * cell, 3),
            'зон_учтено': len(kept), 'зон_исключено': len(wide),
            'лесничеств': len(fr_list), 'лесничество_га': round(fr.sum() * cell, 4)}
