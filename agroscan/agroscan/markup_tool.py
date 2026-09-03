# -*- coding: utf-8 -*-
"""Сборка инструмента разметки по конфигу участка.

Раньше инструмент собирался скриптом на один участок (build_tool_1173.py)
с зашитыми путями — для второго участка его пришлось бы копировать. Здесь
всё берётся из конфига и папки прогона: подложки, высота полога, контуры
частей, кандидаты автопоиска и уже принятая разметка.

Страница самодостаточна: подложки лежат внутри как data-URI, поэтому её
можно опубликовать артефактом и открыть по ссылке.
"""
import json
import os
import time

import numpy as np
from PIL import Image

from . import config as cfg_mod
from . import naming, tool
from .geo import Grid

BACKDROPS = (('bg_clarity.jpg', 'ESRI Clarity, межсезонье'),
             ('bg_summer.jpg', 'ESRI World Imagery, лето'))

def _saved_markup(path):
    """Ранее принятая разметка и время её передачи (мс)."""
    if not path or not os.path.exists(path):
        return None, 0
    d = json.load(open(path, encoding='utf-8'))
    ts = 0
    s = d.get('saved_at')
    if isinstance(s, str) and len(s) >= 19:
        try:
            ts = int(time.mktime(time.strptime(s[:19], '%Y-%m-%dT%H:%M:%S')) * 1000)
        except ValueError:
            ts = 0
    elif isinstance(s, (int, float)):
        ts = int(s)
    return d.get('objects', []), ts

def build(cfg_path, out_path=None, step=2, verbose=True):
    """Собрать HTML инструмента. Возвращает (путь, размер, сведения)."""
    cfg = cfg_mod.load(cfg_path)
    tag = cfg['kn'].replace(':', '-')
    out_dir = os.path.join(cfg['_dir'], '..', 'out', tag)
    meta = json.load(open(cfg_mod.path_of(cfg, 'meta')))
    rings = json.load(open(cfg_mod.path_of(cfg, 'rings')))
    ddir = os.path.dirname(cfg_mod.path_of(cfg, 'image'))
    say = (lambda m: print('   ' + m, flush=True)) if verbose else (lambda m: None)

    load = lambda name: (np.load(os.path.join(out_dir, name))
                         if os.path.exists(os.path.join(out_dir, name)) else None)
    chm, ndvi, mask = load('chm.npy'), load('ndvi.npy'), load('mask.npy')

    # слои считаются на прореженных сетках (полог — шаг 2, рельеф — 10 м),
    # а инструмент рисует тайл в его собственных пикселях: без растяжения
    # до кадра слой занимал часть поля и не совпадал с границей участка.
    # Множитель у рельефа нецелый, поэтому не np.repeat, а честный resize.
    def full(a):
        if a is None:
            return None
        im = Image.fromarray(np.nan_to_num(a, nan=float(np.nanmin(a))).astype(np.float32),
                             mode='F')
        return np.asarray(im.resize((meta['W'], meta['H']), Image.BILINEAR))

    rasters = []
    if chm is not None:
        rasters.append(('chm', full(chm), 'Высота полога, м', 0, 18))
    if ndvi is not None:
        rasters.append(('ndvi', full(ndvi), 'NDVI', 0.2, 0.9))

    # высотные слои: по снимку овраг не обвести, борта видно только на
    # рельефе. Массивы уже посчитаны конвейером (relief.analyze) и лежат
    # рядом — здесь их только растягиваем и красим.
    z, slope, tpi = load('relief_z.npy'), load('relief_slope.npy'), load('relief_tpi.npy')
    ru = lambda v, f='%.1f': (f % v).replace('.', ',')
    if z is not None:
        pct = lambda a, q: float(np.nanpercentile(a, q))
        z0, z1 = pct(z, 2), pct(z, 98)
        # шкала пишется в подписи: без чисел слой красивый, но не аналитический
        rasters.append(('dem', full(z), 'Высота, %s–%s м' % (ru(z0, '%.0f'), ru(z1, '%.0f')),
                        z0, z1, 'hyps'))
        from .sheets.relief import _hillshade
        grid_m = 10.0
        gp = os.path.join(out_dir, 'relief_grid.json')
        if os.path.exists(gp):
            grid_m = json.load(open(gp)).get('grid_m', 10.0)
        sh = _hillshade(z, grid_m)
        # диапазон берём по самому кадру: на пологом участке освещённость
        # держится в узком коридоре, и фиксированная шкала даёт серую кашу
        rasters.append(('shade', full(sh), 'Отмывка рельефа',
                        float(np.nanpercentile(sh, 2)), float(np.nanpercentile(sh, 98))))
    if slope is not None:
        s_hi = max(8.0, float(np.nanpercentile(slope, 95)))
        rasters.append(('slope', full(slope), 'Уклон, 0–%s°' % ru(s_hi, '%.0f'), 0, s_hi, 'slope'))
    if tpi is not None:
        # знак перевёрнут: ложбины синие, гребни коричневые. Размах берём по
        # самому участку — фиксированные ±1,5 м на равнине заливали всё синим
        r = float(np.clip(np.nanpercentile(np.abs(tpi), 90), 0.5, 3.0))
        rasters.append(('tpi', full(-tpi), 'Ложбины и водотоки, ±%s м' % ru(r), -r, r, 'hollow'))

    tiles = tool.tiles_from([(os.path.join(ddir, f), cap) for f, cap in BACKDROPS], rasters)

    # кандидаты автопоиска: на :29 их ноль — слой просто пустой, и это
    # само по себе сведение для владельца, а не повод не собирать инструмент
    cand = []
    if chm is not None and mask is not None:
        from .belts import detect
        from .rings import vectorize
        gd = Grid(meta, cfg['zone'], step=step)
        sub = mask[::step, ::step][:chm.shape[0], :chm.shape[1]]
        auto, _ = detect(chm, sub, meta['mpp'] * step, **{
            k: v for k, v in cfg['belts'].items() if k in ('h_min', 'elong', 'min_ha')})
        up = np.repeat(np.repeat(auto, step, 0), step, 1)[:meta['H'], :meta['W']]
        cand, _, _ = vectorize(up, Grid(meta, cfg['zone']), eps=2.0, minha=0.10)

    chzu = {}
    for f in sorted(os.listdir(out_dir)) if os.path.isdir(out_dir) else []:
        if f.startswith('chzu') and f.endswith('.json'):
            chzu[f[4:-5]] = json.load(open(os.path.join(out_dir, f)))['outer']

    saved, saved_at = _saved_markup(cfg_mod.path_of(cfg, 'markup'))
    report = {}
    rp = os.path.join(out_dir, 'result.json')
    if os.path.exists(rp):
        report = json.load(open(rp, encoding='utf-8'))
    near = ((report.get('ближайший_нп') or {}).get('ближайшие') or [None])[0]
    b = report.get('лесополосы') or {}
    hdr = ['ЗУ %s%s' % (cfg['kn'], ' · ' + cfg['place'] if cfg.get('place') else '')]
    if near:
        from .sources.places import line as place_line
        hdr.append('Ближайший населённый пункт: ' + place_line(near))
    hdr.append('Координаты в %s · автопоиск нашёл кандидатов: %d%s'
               % (cfg.get('zone_name', cfg['zone']), len(cand),
                  ' · полог: медиана %s м, выше 8 м %d %% площади'
                  % (str(b.get('полог_медиана_м', '—')).replace('.', ','),
                     round(100 * b.get('полог_выше_8м_доля', 0)))
                  if 'полог_медиана_м' in b else ''))

    path = out_path or os.path.join(out_dir, naming.fname(cfg, 'Разметка_лесополос.html'))
    p, n = tool.build(path, cfg['kn'], rings, tiles, zouit=chzu.get('3', []), chzu=chzu,
                      candidates=cand, saved=saved, saved_at=saved_at,
                      meta={k: meta[k] for k in ('e0', 'e1', 'n0', 'n1')},
                      header='<br>'.join(hdr))
    info = {'подложек': len(tiles), 'кандидатов': len(cand), 'разметки': len(saved or []),
            'мб': round(n / 1048576, 2), 'частей': len(chzu)}
    say('инструмент разметки: %.2f МБ, подложек %d, кандидатов %d, принятой разметки %d'
        % (info['мб'], info['подложек'], info['кандидатов'], info['разметки']))
    return p, n, info
