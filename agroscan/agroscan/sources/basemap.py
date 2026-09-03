# -*- coding: utf-8 -*-
"""Подложка: тайлы ESRI, перепроецированные в местную СК участка.

Тайлы приходят в веб-Меркаторе, а работать надо в МСК: пересэмплирование
делается сразу здесь, поэтому ниже по конвейеру снимок и границы участка
живут в одной системе координат и bgmeta описывает их обе.

Тайлы теряются регулярно, поэтому недокачанные запрашиваются повторно
несколькими раундами — без этого в подложке остаются серые квадраты.
"""
import concurrent.futures
import io
import math
import os
import subprocess

import numpy as np
from PIL import Image

Image.MAX_IMAGE_PIXELS = None
LAYERS = {
    'summer': ('https://server.arcgisonline.com/ArcGIS/rest/services/'
               'World_Imagery/MapServer/tile/{z}/{y}/{x}'),
    'clarity': ('https://clarity.maptiles.arcgis.com/arcgis/rest/services/'
                'World_Imagery/MapServer/tile/{z}/{y}/{x}'),
}

def _deg2tile(lon, lat, z):
    n = 2 ** z
    return ((lon + 180) / 360 * n,
            (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)

def _get(url, timeout=40):
    # -L обязателен: Clarity отвечает 301 и уводит на wayback.maptiles,
    # без него в подложке остаётся ровный серый фон вместо снимка
    cmd = ['curl', '-sSL', '--max-time', str(timeout), url]
    ca = '/root/.ccr/ca-bundle.crt'
    if os.path.exists(ca):
        cmd[1:1] = ['--cacert', ca]
    r = subprocess.run(cmd, capture_output=True)
    return r.stdout if r.returncode == 0 and len(r.stdout) > 400 else None

def fetch(bbox, loc, layer='summer', zoom=17, mpp=0.60, rounds=4, workers=8, verbose=True,
          tpl=None):
    """bbox — (e0, e1, n0, n1) в местной СК; loc — geo.Local.

    tpl — шаблон тайлов вместо готового слоя: так же качаются архивные
    срезы Wayback и тайлы Google, а перепроецирование в МСК одно на всех.

    Возвращает (изображение, meta) — meta годится прямо в bgmeta.json.
    """
    e0, e1, n0, n1 = bbox
    tpl = tpl or LAYERS[layer]
    corners = loc.to_wgs([(e0, n1), (e1, n1), (e1, n0), (e0, n0)])
    lons = [c[0] for c in corners]; lats = [c[1] for c in corners]
    x0f, y1f = _deg2tile(min(lons) - .0008, min(lats) - .0008, zoom)
    x1f, y0f = _deg2tile(max(lons) + .0008, max(lats) + .0008, zoom)
    X0, X1, Y0, Y1 = int(x0f), int(x1f), int(y0f), int(y1f)
    todo = [(x, y) for x in range(X0, X1 + 1) for y in range(Y0, Y1 + 1)]
    canvas = Image.new('RGB', ((X1 - X0 + 1) * 256, (Y1 - Y0 + 1) * 256), (60, 70, 60))
    if verbose:
        print('   тайлов %d, зум %d' % (len(todo), zoom), flush=True)
    for rnd in range(rounds):
        fail = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_get, tpl.format(z=zoom, x=x, y=y)): (x, y) for x, y in todo}
            for f in concurrent.futures.as_completed(futs):
                x, y = futs[f]
                data = f.result()
                ok = False
                if data:
                    try:
                        canvas.paste(Image.open(io.BytesIO(data)).convert('RGB'),
                                     ((x - X0) * 256, (y - Y0) * 256))
                        ok = True
                    except Exception:
                        pass
                if not ok:
                    fail.append((x, y))
        if verbose and fail:
            print('   раунд %d: не скачано %d' % (rnd + 1, len(fail)), flush=True)
        if not fail:
            break
        todo = fail

    src = np.asarray(canvas)
    W = int((e1 - e0) / mpp); H = int((n1 - n0) / mpp)
    oE = e0 + (np.arange(W) + 0.5) * (e1 - e0) / W
    oN = n1 - (np.arange(H) + 0.5) * (n1 - n0) / H
    mE, mN = np.meshgrid(oE, oN)
    lon, lat = loc._to.transform(mE.ravel(), mN.ravel())
    xt = (np.asarray(lon) + 180) / 360 * (2 ** zoom)
    yt = (1 - np.arcsinh(np.tan(np.radians(np.asarray(lat)))) / math.pi) / 2 * (2 ** zoom)
    img = src[np.clip(((yt - Y0) * 256).astype(int), 0, src.shape[0] - 1),
              np.clip(((xt - X0) * 256).astype(int), 0, src.shape[1] - 1)].reshape(H, W, 3)
    meta = {'e0': e0, 'e1': e1, 'n0': n0, 'n1': n1, 'W': W, 'H': H,
            'mpp': mpp, 'zoom': zoom, 'layer': layer}
    return Image.fromarray(img), meta
