# -*- coding: utf-8 -*-
"""Собрать инструмент разметки для 1173 из результатов конвейера."""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from agroscan import tool
from agroscan.geo import Grid
from agroscan.rings import vectorize
from agroscan.belts import detect
from agroscan.sources import canopy

A = os.path.dirname(os.path.abspath(__file__))
O = os.path.join(A, 'out', '58-24-0341802-1173')
meta = json.load(open(os.path.join(A, 'data/1173/bgmeta.json')))
rings = json.load(open(os.path.join(A, 'data/1173/c1173.json')))
g = Grid(meta, 'msk58-2'); gd = Grid(meta, 'msk58-2', step=2)

chm_path = os.path.join(O, 'chm.npy')
chm = np.load(chm_path) if os.path.exists(chm_path) else canopy.sample(gd)
np.save(chm_path, chm)
mask = gd.submask(np.load(os.path.join(O, 'mask.npy')))
auto, kept = detect(chm, mask, meta['mpp'] * 2)
up = np.repeat(np.repeat(auto, 2, 0), 2, 1)[:meta['H'], :meta['W']]
cand, _, _ = vectorize(up, g, eps=2.0, minha=0.10)

ndvi_path = os.path.join(O, 'ndvi.npy')
ndvi = np.load(ndvi_path) if os.path.exists(ndvi_path) else None
rasters = [('chm', chm, 'Высота полога', 0, 18)]
if ndvi is not None and ndvi.shape == chm.shape:
    rasters.append(('ndvi', ndvi, 'NDVI', 0.2, 0.9))

tiles = tool.tiles_from(
    [(os.path.join(A, 'data/1173/bg_clarity.jpg'), 'Clarity 0,36 м'),
     (os.path.join(A, 'data/1173/bg_google.jpg'), 'Лето 0,72 м')], rasters)
chzu = {k: json.load(open(os.path.join(O, 'chzu%s.json' % k)))['outer'] for k in '1234'}
mk = json.load(open(os.path.join(A, 'data/1173/received.json')))
p, n = tool.build(os.path.join(O, 'tool.html'), '58:24:0341802:1173', rings, tiles,
                  zouit=chzu['3'], chzu=chzu, candidates=cand,
                  saved=mk.get('objects', []),
                  saved_at=int(time.mktime(time.strptime(mk['saved_at'][:19],
                                                         '%Y-%m-%dT%H:%M:%S')) * 1000),
                  meta={k: meta[k] for k in ('e0', 'e1', 'n0', 'n1')},
                  header='ЗУ 58:24:0341802:1173 · Никольский р-н<br>'
                         'Координаты в МСК-58, зона 2 · автопоиск лесополос по высоте полога')
print('инструмент: %.2f МБ, подложек %d, кандидатов %d, разметки %d'
      % (n / 1048576, len(tiles), len(cand), len(mk.get('objects', []))))
