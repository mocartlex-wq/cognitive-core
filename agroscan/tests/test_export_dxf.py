# -*- coding: utf-8 -*-
"""DXF с привязанным растром: контуры и снимок в одних координатах.

Владелец открывает выгрузку в AutoCAD и сверяет границы по подложке, поэтому
проверяется не «файл записался», а привязка: угол вставки, размер пикселя и
то, что контур на растре попадает туда же, куда на схеме.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agroscan import export

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TAG = '58-17-0130701-29'
DATA = os.path.join(ROOT, 'data', TAG)
OUT = os.path.join(ROOT, 'out', TAG)

def _fixture(tmp):
    meta = json.load(open(os.path.join(DATA, 'bgmeta.json')))
    rings = json.load(open(os.path.join(DATA, 'rings.json')))
    parts = {'1': json.load(open(os.path.join(OUT, 'chzu1.json')))}
    p = export.dxf_raster(os.path.join(tmp, 'test.dxf'), rings, parts,
                          os.path.join(DATA, 'bg_summer.jpg'), meta, kn='58:17:0130701:29')
    return p, meta, rings

def test_image_is_georeferenced():
    import tempfile
    from ezdxf import recover
    with tempfile.TemporaryDirectory() as tmp:
        path, meta, rings = _fixture(tmp)
        assert path, 'без ezdxf выгрузка пропускается, тест запускать не на чем'
        doc, aud = recover.readfile(path)
        assert not aud.errors, aud.errors
        msp = doc.modelspace()
        img = msp.query('IMAGE')[0]
        assert abs(img.dxf.insert.x - meta['e0']) < 1e-6, img.dxf.insert
        assert abs(img.dxf.insert.y - meta['n0']) < 1e-6, img.dxf.insert
        mpp = (meta['e1'] - meta['e0']) / meta['W']
        assert abs(img.dxf.u_pixel.x - mpp) < 1e-9, (img.dxf.u_pixel, mpp)
        assert img.image_def.dxf.filename == 'test.jpg', img.image_def.dxf.filename
        assert os.path.exists(os.path.join(tmp, 'test.jpg'))

        # контур в тех же координатах: крайняя точка кольца — на своём месте
        pts = [q[:2] for p in msp.query('LWPOLYLINE') if p.dxf.layer == 'Granica_ZU'
               for q in p.get_points('xy')]
        assert len(pts) == len(rings[0]), (len(pts), len(rings[0]))
        assert min(abs(x - rings[0][0][0]) + abs(y - rings[0][0][1]) for x, y in pts) < 1e-6

def test_world_file_matches_meta():
    """Файл привязки .jgw читается как «пиксель → метр», центр первого пикселя."""
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        meta = json.load(open(os.path.join(DATA, 'bgmeta.json')))
        p = export.world_file(os.path.join(tmp, 'x.jgw'), meta)
        a, b, c, d, e, f = [float(v) for v in open(p).read().split()]
        sx = (meta['e1'] - meta['e0']) / meta['W']
        assert abs(a - sx) < 1e-9 and b == 0 and c == 0
        assert d < 0, 'ось N вниз по картинке'
        # в .jgw координаты пишутся с точностью 0,1 мм
        assert abs(e - (meta['e0'] + sx / 2)) < 1e-3 and abs(f - (meta['n1'] + d / 2)) < 1e-3

def test_normalize_keeps_real_vertex():
    """Живая вершина не должна пропадать при нормализации кольца.

    np.allclose с относительным допуском на координатах МСК (2,2 млн м)
    считал совпавшими точки в 22 м друг от друга: у :29 из границы ЕГРН
    молча исчезала вершина 2242199,76 / 305786,64 — в DXF и MIF выходило
    11 точек вместо 12.
    """
    ring = json.load(open(os.path.join(DATA, 'rings.json')))[0]
    assert len(export.normalize([ring])[0]) == len(ring)

    # замыкающая точка (буквальный повтор первой) по-прежнему снимается
    closed = ring + [ring[0]]
    assert len(export.normalize([closed])[0]) == len(ring)


if __name__ == '__main__':
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_'):
            fn(); n += 1; print('  ✓ %s' % name)
    print('\nDXF С РАСТРОМ ПРОВЕРЕН (%d проверки)' % n)
