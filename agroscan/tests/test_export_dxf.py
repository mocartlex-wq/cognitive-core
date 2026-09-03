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

def test_schema_sheet_has_layout_and_viewport():
    """Схема в DXF: лист A4, видовой экран в круглом масштабе, слои чертежа.

    Владелец открывает файл в AutoCAD и печатает лист, поэтому проверяется
    не только геометрия, но и сам документ: рамка, легенда, штамп и окно
    карты с масштабом из ряда круглых знаменателей.
    """
    import tempfile
    from ezdxf import recover
    from agroscan.export_cad import SCALES, schema_dxf
    with tempfile.TemporaryDirectory() as tmp:
        meta = json.load(open(os.path.join(DATA, 'bgmeta.json')))
        rings = json.load(open(os.path.join(DATA, 'rings.json')))
        parts = {'1': json.load(open(os.path.join(OUT, 'chzu1.json')))}
        path, den = schema_dxf(os.path.join(tmp, 'схема.dxf'), '58:17:0130701:29', rings, parts,
                               2.1911, 'msk58-2', meta=meta,
                               image=os.path.join(DATA, 'bg_summer.jpg'),
                               place={'название': 'Новое Славкино', 'тип': 'village',
                                      'lon': 45.1416, 'lat': 52.544, 'км': 4.75, 'азимут': 300})
        assert den in SCALES, den
        doc, aud = recover.readfile(path)
        assert not aud.errors, aud.errors
        lay = doc.layouts.get('Схема ЧЗУ')
        vp = [v for v in lay.query('VIEWPORT') if v.dxf.id != 1][0]
        # высота окна = высота видового экрана на бумаге × знаменатель
        assert abs(vp.dxf.view_height - vp.dxf.height * den / 1000.0) < 0.5, vp.dxf.view_height
        E = [p[0] for r in rings for p in r]; N = [p[1] for r in rings for p in r]
        assert abs(vp.dxf.view_center_point.x - (max(E) + min(E)) / 2) < 1, vp.dxf.view_center_point
        assert abs(vp.dxf.view_center_point.y - (max(N) + min(N)) / 2) < 1, vp.dxf.view_center_point
        names = {l.dxf.name for l in doc.layers}
        assert {'Ramka', 'Legenda', 'Shtamp', 'Kompas', 'Podlozhka'} <= names, names
        # таблицы закрывают карту маской, иначе сквозь строки видно снимок
        assert len(lay.query('WIPEOUT')) >= 3, len(lay.query('WIPEOUT'))
        assert doc.modelspace().query('IMAGE'), 'подложка не вставлена'

def test_schema_without_raster_still_builds():
    """Обратный случай: снимка нет — чертёж всё равно собирается."""
    import tempfile
    from ezdxf import recover
    from agroscan.export_cad import schema_dxf
    with tempfile.TemporaryDirectory() as tmp:
        rings = json.load(open(os.path.join(DATA, 'rings.json')))
        parts = {'1': json.load(open(os.path.join(OUT, 'chzu1.json')))}
        path, den = schema_dxf(os.path.join(tmp, 'без_растра.dxf'), '58:17:0130701:29', rings,
                               parts, 2.1911, 'msk58-2', meta=None, image=None)
        doc, aud = recover.readfile(path)
        assert not aud.errors and not doc.modelspace().query('IMAGE')
        assert doc.layouts.get('Схема ЧЗУ') is not None and den > 0

def test_scale_is_round_and_fits():
    """Масштаб круглый и участок в окно влезает."""
    from agroscan.export_cad import SCALES, scale_for
    rings = json.load(open(os.path.join(DATA, 'rings.json')))
    den = scale_for(rings, 279, 179)
    assert den in SCALES
    E = [p[0] for r in rings for p in r]; N = [p[1] for r in rings for p in r]
    assert (max(E) - min(E)) / (den / 1000.0) <= 279 and (max(N) - min(N)) / (den / 1000.0) <= 179
    # вдвое меньший знаменатель участок бы уже не вместил
    prev = SCALES[max(0, SCALES.index(den) - 1)]
    assert prev == den or max(max(E) - min(E), max(N) - min(N)) / (prev / 1000.0) > 179 * 0.9

def test_cad_pack_keeps_files_together():
    """Архив для AutoCAD: чертёж, растр и привязка едут вместе.

    Растр в DXF — внешняя ссылка по имени: стоит файлам разъехаться или
    переименоваться, и AutoCAD открывает лист с пустым полем вместо карты.
    """
    import tempfile, zipfile
    from agroscan.export import cad_pack
    with tempfile.TemporaryDirectory() as tmp:
        dxf = os.path.join(tmp, 'X_Схема_ЧЗУ.dxf')
        open(dxf, 'w').write('0\nEOF\n')
        open(os.path.join(tmp, 'X_Схема_ЧЗУ.jpg'), 'wb').write(b'jpg')
        open(os.path.join(tmp, 'X_Схема_ЧЗУ.jgw'), 'w').write('1\n0\n0\n-1\n0\n0\n')
        open(os.path.join(tmp, 'X_Схема_ЧЗУ.png'), 'wb').write(b'png')   # картинка листа
        z = cad_pack(os.path.join(tmp, 'pack.zip'), [dxf], den=2000)
        names = zipfile.ZipFile(z).namelist()
        assert 'X_Схема_ЧЗУ.dxf' in names and 'X_Схема_ЧЗУ.jpg' in names
        assert 'X_Схема_ЧЗУ.jgw' in names and 'Как_открыть.txt' in names
        assert 'X_Схема_ЧЗУ.png' not in names, names      # это лист редактора, не растр
        txt = zipfile.ZipFile(z).read('Как_открыть.txt').decode('utf-8')
        assert '1:2000' in txt and 'X_Схема_ЧЗУ.jpg' in txt
        assert cad_pack(os.path.join(tmp, 'empty.zip'), [None]) is None


if __name__ == '__main__':
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_'):
            fn(); n += 1; print('  ✓ %s' % name)
    print('\nDXF С РАСТРОМ ПРОВЕРЕН (%d проверки)' % n)
