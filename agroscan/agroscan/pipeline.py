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
from .sources import canopy, dem as dem_src, landcover, landsat, sentinel, soil as soil_src

def _say(t0, msg):
    print('[%5.1f с] %s' % (time.time() - t0, msg), flush=True)

def run(cfg_path, out_dir=None, step_dzz=2, sheets=True, formats=True, no_cache=False,
        appendices=True):
    t0 = time.time()
    cfg = cfg_mod.load(cfg_path)
    out = out_dir or os.path.join(cfg['_dir'], '..', 'out', cfg['kn'].replace(':', '-'))
    out = os.path.abspath(out); os.makedirs(out, exist_ok=True)
    _say(t0, 'участок %s, ЕГРН %.4f га → %s' % (cfg['kn'], cfg['egrn_ha'], out))

    rings = json.load(open(cfg_mod.path_of(cfg, 'rings')))
    if rings and isinstance(rings[0][0], (int, float)):
        rings = [rings]              # выгрузки бывают и одним кольцом, и списком колец
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
    bands = {}
    if cached:
        used = cached['used']
        ix = {n: cache_mod.get_array(k_s2 + '_' + n) for n in cached['indices']}
        ix = {n: v for n, v in ix.items() if v is not None}
        bands = {n: cache_mod.get_array(k_s2 + '_b_' + n) for n in cached.get('bands', [])}
        bands = {n: v for n, v in bands.items() if v is not None}
    # каналы нужны не только для индексов, но и для ИК-композитов приложения:
    # запись в кэше без них — повод перезагрузить, иначе лист молча пропадёт
    if not ix or not bands:
        sc = sentinel.search(bbox, '%d-%s' % (year, cfg['sentinel']['start']),
                             '%d-%s' % (year, cfg['sentinel']['end']),
                             cfg['sentinel']['max_cloud'])
        comp, used = sentinel.composite(gd, sc, limit=cfg['sentinel']['scenes'])
        ix = sentinel.indices(comp) if comp else {}
        bands = comp or {}
        if ix:
            for n, v in ix.items():
                cache_mod.put_array(k_s2 + '_' + n, v)
            for n, v in bands.items():
                cache_mod.put_array(k_s2 + '_b_' + n, v)
            cache_mod.put_json(k_s2, {'used': used, 'indices': sorted(ix),
                                      'bands': sorted(bands)})
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
    # строим только те части, что объявлены в конфиге: на :74 нет ни ЗОУИТ,
    # ни лесополос, и пустые ЧЗУ/3, ЧЗУ/4 роняли проверку «все части непустые»
    raw = {'1': drev, '2': gr, '3': z.merge_touching(zin, mask, mpp, gap),
           '4': z.merge_touching(belt, mask, mpp, gap)}
    raw = {k: v for k, v in raw.items() if k in cfg['parts']}
    eps = {'1': 3.0, '2': 3.0, '3': 2.0, '4': 2.5}
    Z = {k: z.to_poly(*vectorize(m, grid, eps.get(k, 3.0), 0.10)[:2]) for k, m in raw.items()}
    order = [k for k in cfg['priority'] if k in Z]
    Z = z.resolve(Z, parcel, order)

    def decide(sel, sub):
        zz = (zn[sub] & sel).sum(); dd = (np.isin(cls[sub], [1, 2, 3]) & sel).sum(); t = sel.sum()
        return '3' if zz / t > 0.5 else ('1' if dd / t > 0.5 else '2')
    # cover_all=false — участок покрыт частями не целиком: на 58:28:0500401:74
    # больше половины площади занимает действующая пашня, она не часть ЧЗУ
    # и добирать остаток в зоны нельзя.
    if cfg.get('cover_all', True):
        Z, rest, moved = z.fill_remainder(Z, parcel, grid, decide, order, thin)
        _say(t0, 'добор остатка %.3f га (%s)'
             % (rest, ', '.join('ЧЗУ/%s %.2f' % (k, v) for k, v in moved.items() if v > 0)))
    Z, cleaned = z.despeckle(Z, parcel, order, thin) if len(Z) > 1 else (Z, 0.0)
    _say(t0, 'чистка нитей %.3f га' % cleaned)

    # ── проверки и запись ──────────────────────────────────────────────
    qa = qa_mod.check(Z, parcel, cfg['egrn_ha'], thin, cover_all=cfg.get('cover_all', True))
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
    # ── приложения: обоснование принятых решений ───────────────────────
    if appendices and cfg.get('appendices', True):
        want = cfg.get('appendices', True)
        want = ('relief', 'dynamics', 'ir', 'soil') if want in (True, None) else tuple(want)
        from .sheets import relief as sh_relief, dynamics as sh_dyn, ir as sh_ir

        if 'relief' in want:
            try:
                from . import relief as relief_mod
                k_dem = cache_mod.key('dem', bbox=bbox, step=4, shape=Grid(meta, cfg['zone'], 4).shape)
                dm = None if no_cache else cache_mod.get_array(k_dem)
                tiles = []
                if dm is None:
                    dm, tiles = dem_src.sample(Grid(meta, cfg['zone'], step=4))
                    if dm is not None:
                        cache_mod.put_array(k_dem, dm)
                if dm is None:
                    _say(t0, 'рельеф: покрытия DEM нет, лист пропущен')
                else:
                    D = np.repeat(np.repeat(dm, 4, 0), 4, 1)[:meta['H'], :meta['W']]
                    rel = relief_mod.analyze(D, mask, mpp)
                    sh_relief.build(os.path.join(out, 'Приложение_рельеф.pdf'), cfg['kn'], rings,
                                    rel, cfg['egrn_ha'], meta, cfg_mod.path_of(cfg, 'image'))
                    json.dump({'stats': rel['stats'], 'формы': rel['формы'], 'тайлы': tiles},
                              open(os.path.join(out, 'relief.json'), 'w'), ensure_ascii=False, indent=1)
                    _say(t0, 'рельеф: уклон медиана %.1f°, форм %d, оврагов %d, к исключению %.2f га'
                         % (rel['stats']['уклон_медиана'], rel['stats']['форм_найдено'],
                            rel['stats']['оврагов'], rel['stats']['исключается_га']))
            except Exception as e:
                _say(t0, 'рельеф: лист пропущен (%s)' % str(e)[:70])

        if 'dynamics' in want:
            try:
                from . import timeseries as ts_mod
                gs = Grid(meta, cfg['zone'], step=8)
                # С 1999 года — КАЖДЫЙ год, без прореживания. Шаг в два года
                # пропустил 2010-й и сдвинул дату выбытия с 2011 на 2002:
                # ответ листа зависит от того, какие годы попали в выборку,
                # поэтому экономить на них нельзя.
                yy = cfg.get('landsat_years') or (
                    [1977, 1985, 1990, 1995] + list(range(1999, year + 1)))
                cdir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                    'cache', 'landsat', cfg['kn'].replace(':', '-'))
                ser, _ = landsat.ndvi_series(gs, yy, cdir, verbose=False)
                # ряд считается по зарастающей части, а не по всему участку:
                # на :74 три четверти площади — действующая пашня, летом она
                # зелёная, пар в статистике тонет, и дата выбытия не находится
                grow = np.zeros_like(mask)
                for k in ('1', '2'):
                    if k in res:
                        grow |= rasterize(res[k]['outer'], grid, res[k]['inner'])
                grow &= mask
                m8 = gs.submask(grow if grow.sum() > 0.05 * mask.sum() else mask)
                scope = ('зарастающая часть %.1f га' % (grow.sum() * mpp * mpp / 1e4)
                         if grow.sum() > 0.05 * mask.sum() else 'весь участок')
                if len(ser) < 4:
                    _say(t0, 'динамика: сцен Landsat %d — мало, лист пропущен' % len(ser))
                else:
                    ts = ts_mod.summary(ser, m8, gs.cellHa, this_year=year,
                                        edge_m=30.0, cell_m=meta['mpp'] * gs.step)
                    lc, tnames = landcover.sample(gs)
                    extra = []
                    wc = lc.get('worldcover')
                    if wc is not None:
                        tree = np.isin(np.nan_to_num(wc).astype(int), [10, 20]) & m8
                        extra.append(('ESA WorldCover 2021, древесная',
                                      '%.2f га' % (tree.sum() * gs.cellHa),
                                      'тайл ' + tnames['тайл_worldcover']))
                    own = sum(res[k]['areaHa'] for k in ('1', '4') if k in res)
                    extra.append(('Наш расчёт, древесная', '%.2f га' % own, ''))
                    hs = lc.get('hansen_treecover2000')
                    if hs is not None:
                        extra.append(('Hansen GFC, покров крон 2000 г.',
                                      '%d %% ≥ 25 %%' % (100 * ((np.nan_to_num(hs) >= 25) & m8).sum()
                                                         // max(m8.sum(), 1)),
                                      'тайл ' + tnames['тайл_hansen']))
                    if chm is not None:
                        cs = gs.submask(chm_f) if chm_f is not None else None
                        if cs is not None:
                            v = cs[m8 & np.isfinite(cs)]
                            if len(v):
                                extra.append(('Высота полога, медиана', '%.0f м' % np.median(v),
                                              'Meta/WRI Canopy Height, ~1 м'))
                    sh_dyn.build(os.path.join(out, 'Приложение_динамика.pdf'), cfg['kn'], rings,
                                 meta, ser, ts, cfg['egrn_ha'], extra, scope=scope)
                    json.dump(ts, open(os.path.join(out, 'timeseries.json'), 'w'),
                              ensure_ascii=False, indent=1)
                    _say(t0, 'динамика (%s): сцен %d, год выбытия %s, возраст %s лет'
                         % (scope, len(ser), ts['год_выбытия'],
                            ts.get('возраст_зарастания_лет', '—')))
            except Exception as e:
                _say(t0, 'динамика: лист пропущен (%s)' % str(e)[:70])

        if 'soil' in want:
            try:
                # точки берём внутри маски участка: одна ячейка SoilGrids — 250 м,
                # по центру и краям значения расходятся на несколько процентов
                ys, xs = np.nonzero(mask[::16, ::16])
                pts = []
                if len(ys):
                    idx = np.linspace(0, len(ys) - 1, min(5, len(ys))).astype(int)
                    gg = Grid(meta, cfg['zone'], step=16)
                    for i in idx:
                        r, c = ys[i], xs[i]
                        if r < gg.shape[0] and c < gg.shape[1]:
                            k = r * gg.shape[1] + c
                            pts.append((float(gg.lon[k]), float(gg.lat[k])))
                k_soil = cache_mod.key('soil', pts=[(round(a, 3), round(b, 3)) for a, b in pts])
                cached_soil = None if no_cache else cache_mod.get_json(k_soil)
                if cached_soil:
                    srows, used_pts = cached_soil['rows'], cached_soil['точек']
                else:
                    srows, _u, used_pts = soil_src.profile_points(pts)
                    if srows:
                        cache_mod.put_json(k_soil, {'rows': srows, 'точек': used_pts})
                if not srows:
                    _say(t0, 'почвы: SoilGrids не ответил, лист пропущен')
                else:
                    from .sheets import soil as sh_soil
                    concl = soil_src.interpret(srows)
                    # классификация WRB отвечает долго и не всегда — кэшируем,
                    # а при отказе лист собирается без блока типа почвы
                    # берём точку, ближайшую к центру участка: первая из списка
                    # зависит от порядка обхода маски и класс от прогона к прогону плавал
                    ctr = (float(np.mean([q[0] for q in pts])), float(np.mean([q[1] for q in pts]))) \
                        if pts else None
                    pw = min(pts, key=lambda q: (q[0] - ctr[0]) ** 2 + (q[1] - ctr[1]) ** 2) \
                        if pts else None
                    k_wrb = cache_mod.key('wrb', pt=(round(pw[0], 3), round(pw[1], 3)) if pw else None)
                    wrb = None if no_cache else cache_mod.get_json(k_wrb)
                    if wrb is None and pw:
                        wrb = soil_src.classification(*pw)
                        if wrb:
                            cache_mod.put_json(k_wrb, wrb)
                    sm = cfg.get('soil_map') or None
                    if sm and not (sm.get('индекс') or '').strip():
                        sm = None          # индекс не заполнен — блока карты не будет
                    if sm and sm.get('индекс') and not sm.get('название'):
                        row = soil_src.fridland(index=sm['индекс'])
                        if row:
                            sm = dict(sm, название=row['название'], код=row['код'])
                    sh_soil.build(os.path.join(out, 'Приложение_почвы.pdf'), cfg['kn'], srows,
                                  concl, cfg['egrn_ha'], cfg.get('place', ''), used_pts,
                                  wrb=wrb, soil_map=sm)
                    json.dump({'точек': used_pts, 'профиль': srows, 'wrb': wrb,
                               'карта_почв': sm},
                              open(os.path.join(out, 'soil.json'), 'w'),
                              ensure_ascii=False, indent=1)
                    top = list(srows.values())[0]
                    _say(t0, 'почвы: %s, гумус %.1f %%, pH %.1f (точек %d)%s'
                         % (soil_src.texture_class(top['clay'], top['silt'], top['sand']),
                            top['humus'], top['phh2o'], used_pts,
                            ' | WRB ' + wrb['wrb'] if wrb else ' | WRB не ответил'))
            except Exception as e:
                _say(t0, 'почвы: лист пропущен (%s)' % str(e)[:70])

        if 'ir' in want and bands:
            try:
                pr = {k: v['outer'] for k, v in res.items()}
                if sh_ir.build(os.path.join(out, 'Приложение_ИК.pdf'), cfg['kn'], rings, meta,
                               bands, cfg['egrn_ha'], pr, used):
                    _say(t0, 'ИК-материалы: композиты из каналов Sentinel-2')
            except Exception as e:
                _say(t0, 'ИК-материалы: лист пропущен (%s)' % str(e)[:70])

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
    tot_parts = sum(v['areaHa'] for v in res.values())
    print('  %-62s %7.2f га  (ЕГРН %.2f%s)'
          % ('сумма частей', tot_parts, cfg['egrn_ha'],
             ', вне частей %.2f' % (cfg['egrn_ha'] - tot_parts) if not cfg.get('cover_all', True) else ''))
    print()
    print(qa_mod.report(qa))
    _say(t0, 'готово')
    return res, qa
