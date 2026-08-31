# -*- coding: utf-8 -*-
"""Конвейер участка: конфиг → классификация → части ЗУ → проверки → результат.

Раньше это была последовательность из полутора десятков скриптов, порядок
которых держался в голове. Здесь она записана один раз и повторяема.
"""
import json
import os
import time
import numpy as np

from . import belts as belts_mod
from . import classify as cls_mod
from . import config as cfg_mod
from . import qa as qa_mod
from . import zones as z
from . import cache as cache_mod
from .geo import Grid
from .rings import rasterize, vectorize
from .sources import canopy, sentinel

def _say(t0, msg):
    print('[%5.1f с] %s' % (time.time() - t0, msg), flush=True)

def run(cfg_path, out_dir=None, step_dzz=2, sheets=True, formats=True, no_cache=False):
    t0 = time.time()
    cfg = cfg_mod.load(cfg_path)
    out = out_dir or os.path.join(cfg['_dir'], '..', 'out', cfg['kn'].replace(':', '-'))
    out = os.path.abspath(out); os.makedirs(out, exist_ok=True)
    _say(t0, 'участок %s, ЕГРН %.4f га → %s' % (cfg['kn'], cfg['egrn_ha'], out))

    rings = json.load(open(cfg_mod.path_of(cfg, 'rings')))
    meta = json.load(open(cfg_mod.path_of(cfg, 'meta')))
    grid = Grid(meta, cfg['zone'])
    mpp = meta['mpp']
    parcel = z.parcel_poly(rings)
    mask = rasterize([rings[0]], grid, rings[1:])
    _say(t0, 'граница: полигон %.4f га, растр %.4f га'
         % (parcel.area / 1e4, mask.sum() * mpp * mpp / 1e4))

    # ── данные ДЗЗ (прореженная сетка: индексы 10 м, полог 1 м) ─────────
    gd = Grid(meta, cfg['zone'], step=step_dzz)
    bbox = (round(float(gd.lon.min()), 5), round(float(gd.lat.min()), 5),
            round(float(gd.lon.max()), 5), round(float(gd.lat.max()), 5))
    k_chm = cache_mod.key('chm', bbox=bbox, step=step_dzz, shape=gd.shape)
    chm = None if no_cache else cache_mod.get_array(k_chm)
    if chm is None:
        chm = canopy.sample(gd)
        cache_mod.put_array(k_chm, chm)
    _say(t0, 'полог: %s' % ('нет покрытия' if chm is None else
                            'медиана %.1f м, p90 %.1f м' % (np.nanmedian(chm[gd.submask(mask)]),
                                                            np.nanpercentile(chm[gd.submask(mask)], 90))))
    year = cfg.get('season_year', time.gmtime().tm_year)
    k_s2 = cache_mod.key('s2', bbox=bbox, step=step_dzz, year=year,
                         season=(cfg['sentinel']['start'], cfg['sentinel']['end']),
                         cloud=cfg['sentinel']['max_cloud'], n=cfg['sentinel']['scenes'])
    cached = None if no_cache else cache_mod.get_json(k_s2)
    ix, used = {}, []
    if cached:
        used = cached['used']
        ix = {n: cache_mod.get_array(k_s2 + '_' + n) for n in cached['indices']}
        ix = {n: v for n, v in ix.items() if v is not None}
    if not ix:
        sc = sentinel.search(bbox, '%d-%s' % (year, cfg['sentinel']['start']),
                             '%d-%s' % (year, cfg['sentinel']['end']),
                             cfg['sentinel']['max_cloud'])
        comp, used = sentinel.composite(gd, sc, limit=cfg['sentinel']['scenes'])
        ix = sentinel.indices(comp) if comp else {}
        if ix:
            for n, v in ix.items():
                cache_mod.put_array(k_s2 + '_' + n, v)
            cache_mod.put_json(k_s2, {'used': used, 'indices': sorted(ix)})
    _say(t0, 'Sentinel-2: композит из %d сцен%s%s' % (len(used), ' (из кэша)' if cached else '',
         ', NDMI медиана %.3f' % np.nanmedian(ix['ndmi'][gd.submask(mask)]) if ix else ''))

    up = lambda a: None if a is None else np.repeat(np.repeat(a, step_dzz, 0), step_dzz, 1)[:meta['H'], :meta['W']]
    chm_f, ndvi_f, ndmi_f = up(chm), up(ix.get('ndvi')), up(ix.get('ndmi'))

    # ── классификация ──────────────────────────────────────────────────
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None
    rgb = np.asarray(Image.open(cfg_mod.path_of(cfg, 'image')).convert('RGB')).astype(np.float32)
    crown, grn = cls_mod.crowns(rgb, cfg['crown']['grn_min'], cfg['crown']['lum_max'])
    cov = cls_mod.cover(crown, mpp, cfg['crown']['window_m'])
    grn_m = cls_mod.boxmean(grn, int(8 / mpp))
    cls, missed = cls_mod.grade(cov, mask, grn_m, ndvi_f, ndmi_f, chm_f)
    cls = cls_mod.generalize(cls, mask, mpp, cfg['generalize_ha'])
    ar = cls_mod.areas(cls, mask, mpp, cfg['egrn_ha'])
    _say(t0, 'классы: ' + ', '.join('%s %.2f га' % (cls_mod.NAMES.get(c, c), a)
                                    for c, a in sorted(ar.items())))
    if missed:
        _say(t0, 'уточнено по NDMI+полог: %.2f га подняты из травы в слабое зарастание'
             % (missed * mpp * mpp / 1e4))

    # ── защитные лесные полосы ─────────────────────────────────────────
    b_auto = np.zeros_like(mask)
    b_info = {}
    if chm is not None:
        a, kept = belts_mod.detect(chm, gd.submask(mask), mpp * step_dzz,
                                   cfg['belts']['h_min'], cfg['belts']['elong'],
                                   cfg['belts']['min_ha'])
        b_auto = up(a).astype(bool)
        b_info['найдено_полос'] = len(kept)
    b_man = np.zeros_like(mask)
    mp_path = cfg_mod.path_of(cfg, 'markup')
    if mp_path and os.path.exists(mp_path):
        D = json.load(open(mp_path))
        objs = [o for o in D.get('objects', []) if o.get('type', 'belt') == 'belt']
        if objs:
            b_man = rasterize([o['ring'] for o in objs],
                              grid, [h for o in objs for h in o.get('holes', [])])
        if chm is not None:
            b_info.update(belts_mod.compare(b_auto[::step_dzz, ::step_dzz],
                                            (b_man & mask)[::step_dzz, ::step_dzz],
                                            mpp * step_dzz))
    src = cfg['belts'].get('source', 'manual')
    belt = {'manual': b_man, 'auto': b_auto, 'both': b_man | b_auto}[src] & mask
    _say(t0, 'лесополосы (%s): %.2f га%s' % (src, belt.sum() * mpp * mpp / 1e4,
         ' | ' + json.dumps(b_info, ensure_ascii=False) if b_info else ''))

    # ── части ЗУ ───────────────────────────────────────────────────────
    zn = np.load(cfg_mod.path_of(cfg, 'zouit')) if cfg.get('zouit') else np.zeros_like(mask)
    zin = z.drop_small_zouit(zn & mask, mpp, cfg['zones']['zouit_min_ha'])
    zn = zin | (zn & ~mask)
    gap = cfg['zones']['gap_m']; thin = cfg['zones']['thin_m']
    free = mask & ~zn & ~belt
    drev = np.isin(cls, [1, 2, 3]) & free
    drev = z.merge_touching(z.smooth(drev, free, mpp), free, mpp, gap)
    gr = (cls == 0) & free
    gr = z.merge_touching(z.smooth(gr, free, mpp, 6.0, 3.0), free, mpp, gap)
    raw = {'1': drev, '2': gr, '3': z.merge_touching(zin, mask, mpp, gap),
           '4': z.merge_touching(belt, mask, mpp, gap)}
    eps = {'1': 3.0, '2': 3.0, '3': 2.0, '4': 2.5}
    Z = {k: z.to_poly(*vectorize(m, grid, eps[k], 0.10)[:2]) for k, m in raw.items()}
    Z = z.resolve(Z, parcel, cfg['priority'])

    def decide(sel, sub):
        zz = (zn[sub] & sel).sum(); dd = (np.isin(cls[sub], [1, 2, 3]) & sel).sum(); t = sel.sum()
        return '3' if zz / t > 0.5 else ('1' if dd / t > 0.5 else '2')
    Z, rest, moved = z.fill_remainder(Z, parcel, grid, decide, cfg['priority'], thin)
    Z, cleaned = z.despeckle(Z, parcel, cfg['priority'], thin)
    _say(t0, 'добор остатка %.3f га (%s), чистка нитей %.3f га'
         % (rest, ', '.join('ЧЗУ/%s %.2f' % (k, v) for k, v in moved.items() if v > 0), cleaned))

    # ── проверки и запись ──────────────────────────────────────────────
    qa = qa_mod.check(Z, parcel, cfg['egrn_ha'], thin)
    res = {}
    for k in sorted(Z):
        o, i, a = z.to_rings(Z[k])
        res[k] = {'outer': o, 'inner': i, 'areaHa': a, 'название': cfg['parts'].get(k, '')}
        json.dump(res[k], open(os.path.join(out, 'chzu%s.json' % k), 'w'))
    json.dump({'kn': cfg['kn'], 'egrn_ha': cfg['egrn_ha'],
               'части': {k: round(v['areaHa'], 4) for k, v in res.items()},
               'сумма': round(sum(v['areaHa'] for v in res.values()), 4),
               'классы': {str(c): round(a, 4) for c, a in ar.items()},
               'лесополосы': b_info, 'сцены_sentinel': used, 'qa': qa},
              open(os.path.join(out, 'result.json'), 'w'), ensure_ascii=False, indent=1)
    np.save(os.path.join(out, 'cls.npy'), cls); np.save(os.path.join(out, 'mask.npy'), mask)
    np.save(os.path.join(out, 'belt.npy'), belt)
    if chm is not None:
        np.save(os.path.join(out, 'chm.npy'), chm)
    if 'ndvi' in ix:
        np.save(os.path.join(out, 'ndvi.npy'), ix['ndvi'])

    # ── комплект листов и обменные форматы ─────────────────────────────
    made = {}
    if sheets:
        from .sheets import schema as sh_schema, check_map as sh_check, note as sh_note
        nb_path = cfg_mod.path_of(cfg, 'neighbors')
        nb = json.load(open(nb_path)) if nb_path and os.path.exists(nb_path) else []
        _, den = sh_schema.build(os.path.join(out, 'Схема_ЧЗУ.pdf'), cfg['kn'], rings, res,
                                 cfg['egrn_ha'], cfg['zone'], nb)
        _say(t0, 'схема ЧЗУ собрана, масштаб 1:%d' % den)
        rel = lambda q: q if os.path.isabs(q) else os.path.join(cfg['_dir'], q)
        bd = [(rel(b['path']), b['caption'], b.get('note', ''))
              for b in cfg.get('backdrops', [])]
        bd = [b for b in bd if os.path.exists(b[0])]
        if bd:
            E = [p[0] for r in rings for p in r]; N = [p[1] for r in rings for p in r]
            sh_check.build(os.path.join(out, 'Проверочная_карта.pdf'), cfg['kn'], rings, res,
                           cfg['egrn_ha'], meta, bd,
                           fragment=(sum(E) / len(E), sum(N) / len(N), cfg.get('fragment_m', 540)))
            _say(t0, 'проверочная карта собрана (подложек %d)' % len(bd))
        sh_note.build(os.path.join(out, 'Пояснительная_записка.pdf'), cfg['kn'], res,
                      cfg['egrn_ha'], json.load(open(os.path.join(out, 'result.json'))),
                      cfg.get('place', ''), cfg.get('zone_name', cfg['zone']))
        _say(t0, 'пояснительная записка собрана')
    if formats:
        from . import export
        made = export.all_formats(out, cfg['kn'], rings, res, cfg['egrn_ha'], cfg['zone'],
                                  cfg.get('export', {}).get('simplify_m', 0.0))
        _say(t0, 'обменные форматы: DXF, MIF/MID, каталог (%d точек), ведомость, GeoJSON'
             % made['точек'])
    print()
    for k in sorted(res):
        print('  ЧЗУ/%s %-58s %7.2f га  контуров %2d' %
              (k, res[k]['название'][:58], res[k]['areaHa'], len(res[k]['outer'])))
    print('  %-62s %7.2f га  (ЕГРН %.2f)' % ('сумма частей', sum(v['areaHa'] for v in res.values()), cfg['egrn_ha']))
    print()
    print(qa_mod.report(qa))
    _say(t0, 'готово')
    return res, qa
