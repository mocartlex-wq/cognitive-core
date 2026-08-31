# -*- coding: utf-8 -*-
"""Геометрическая основа: местные системы координат, сетка снимка, выборка растров.

ВАЖНО (цена ошибки — полный пересчёт анализа): ESRI ImageServer расширяет
запрошенный bbox, подгоняя его под соотношение сторон параметра size.
Геопривязку нельзя брать из запроса — только из самого файла. На участке
58:24:0341802:1173 такая ошибка растянула привязку в 1,6 раза и увела края
на 600 м; обнаружилась только по несовпадению контура со снимком.
"""
import json
import numpy as np
import rasterio
from pyproj import CRS, Transformer

# Местные системы координат. Ключ — как пишут в конфиге участка.
# y_0 у зон МСК-58 общий, различаются осевой меридиан и вынос на восток.
_KRASS = ('+ellps=krass +towgs84=23.92,-141.27,-80.9,0,0.35,0.82,-0.12 '
          '+units=m +no_defs')
MSK_ZONES = {
    'msk58-1': '+proj=tmerc +lat_0=0 +lon_0=43.05 +k=1 +x_0=1300000 +y_0=-5514743.504 ' + _KRASS,
    'msk58-2': '+proj=tmerc +lat_0=0 +lon_0=46.05 +k=1 +x_0=2300000 +y_0=-5514743.504 ' + _KRASS,
    'msk58-3': '+proj=tmerc +lat_0=0 +lon_0=49.05 +k=1 +x_0=3300000 +y_0=-5514743.504 ' + _KRASS,
}

def crs_of(zone):
    """CRS по имени зоны из реестра либо по готовой proj4-строке."""
    if zone in MSK_ZONES:
        return CRS.from_proj4(MSK_ZONES[zone])
    if str(zone).startswith('+proj'):
        return CRS.from_proj4(zone)
    return CRS.from_user_input(zone)

class Local:
    """Преобразования между местной СК участка и WGS84."""
    def __init__(self, zone):
        self.zone = zone
        self.crs = crs_of(zone)
        self._to = Transformer.from_crs(self.crs, CRS.from_epsg(4326), always_xy=True)
        self._fr = Transformer.from_crs(CRS.from_epsg(4326), self.crs, always_xy=True)
    def to_wgs(self, pts):
        E = np.asarray([p[0] for p in pts], float); N = np.asarray([p[1] for p in pts], float)
        lon, lat = self._to.transform(E, N)
        return list(zip(np.atleast_1d(lon), np.atleast_1d(lat)))
    def from_wgs(self, pts):
        lon = np.asarray([p[0] for p in pts], float); lat = np.asarray([p[1] for p in pts], float)
        E, N = self._fr.transform(lon, lat)
        return list(zip(np.atleast_1d(E), np.atleast_1d(N)))

def bbox_of(rings, margin=130.0):
    """Габарит набора колец в местной СК с запасом на поля листа."""
    pts = [p for r in rings for p in r]
    E = [p[0] for p in pts]; N = [p[1] for p in pts]
    return (min(E) - margin, max(E) + margin, min(N) - margin, max(N) + margin)

class Grid:
    """Сетка снимка участка: пиксель → местная СК → WGS84 → пиксель чужого растра.

    meta — словарь {e0,e1,n0,n1,W,H,mpp}; step прореживает сетку, когда полное
    разрешение не нужно (рельеф, индексы по 10-30 м).
    """
    def __init__(self, meta, zone, step=1):
        self.M = dict(meta); self.step = step
        self.loc = Local(zone)
        W, H = meta['W'], meta['H']
        self.ys = np.arange(0, H, step); self.xs = np.arange(0, W, step)
        EE = meta['e0'] + (self.xs + .5) / W * (meta['e1'] - meta['e0'])
        NN = meta['n1'] - (self.ys + .5) / H * (meta['n1'] - meta['n0'])
        Eg, Ng = np.meshgrid(EE, NN)
        lon, lat = self.loc._to.transform(Eg.ravel(), Ng.ravel())
        self.lon = np.asarray(lon); self.lat = np.asarray(lat)
        self.shape = (len(self.ys), len(self.xs))
        self.cellHa = (step * meta['mpp']) ** 2 / 10000

    @classmethod
    def from_file(cls, path, zone, step=1):
        return cls(json.load(open(path)), zone, step)

    def sample(self, path, band=1, resample='nearest'):
        """Значения растра в узлах сетки. Геопривязка берётся ИЗ ФАЙЛА.

        path может быть локальным файлом или /vsicurl/... — читается только
        нужное окно, поэтому глобальные COG (карта высот полога) не качаются целиком.
        """
        with rasterio.open(path) as ds:
            if str(ds.crs) != 'EPSG:4326':
                t = Transformer.from_crs('EPSG:4326', ds.crs, always_xy=True)
                X, Y = t.transform(self.lon, self.lat)
            else:
                X, Y = self.lon, self.lat
            inv = ~ds.transform
            col, row = inv * (X, Y)
            col = np.asarray(col); row = np.asarray(row)
            good = (col >= 0) & (col < ds.width) & (row >= 0) & (row < ds.height)
            out = np.full(col.shape, np.nan, np.float32)
            if good.any():
                c0, c1 = int(np.floor(col[good].min())), int(np.ceil(col[good].max())) + 1
                r0, r1 = int(np.floor(row[good].min())), int(np.ceil(row[good].max())) + 1
                win = rasterio.windows.Window(c0, r0, c1 - c0, r1 - r0)
                a = ds.read(band, window=win).astype(np.float32)
                out[good] = a[row[good].astype(int) - r0, col[good].astype(int) - c0]
            nd = ds.nodata
            if nd is not None:
                out[out == nd] = np.nan
        return out.reshape(self.shape)

    def submask(self, mask):
        """Растр полного разрешения → та же прореженная сетка."""
        return mask[np.ix_(self.ys, self.xs)]

    def px(self, e, n):
        """Координаты местной СК → пиксель снимка (дробный)."""
        M = self.M
        return ((e - M['e0']) / (M['e1'] - M['e0']) * M['W'],
                (M['n1'] - n) / (M['n1'] - M['n0']) * M['H'])

    def xy(self, col, row):
        """Пиксель снимка → координаты местной СК (центр пикселя)."""
        M = self.M
        return (M['e0'] + col / M['W'] * (M['e1'] - M['e0']),
                M['n1'] - row / M['H'] * (M['n1'] - M['n0']))
