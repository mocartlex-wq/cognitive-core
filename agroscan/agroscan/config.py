# -*- coding: utf-8 -*-
"""Конфигурация участка. YAML, если библиотека есть; иначе JSON — формат один и тот же."""
import json
import os

DEFAULTS = {
    'zone': 'msk58-2',
    'margin_m': 130,
    'crown': {'grn_min': 6.0, 'lum_max': 68.0, 'window_m': 25.0},
    'generalize_ha': 0.30,
    'belts': {'h_min': 8.0, 'elong': 3.0, 'min_ha': 0.10},
    'zones': {'gap_m': 5.0, 'thin_m': 3.0, 'zouit_min_ha': 0.25},
    'sentinel': {'start': '06-01', 'end': '09-15', 'max_cloud': 25, 'scenes': 6},
    'priority': ['3', '4', '1', '2'],
}

def _merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out

def load(path):
    txt = open(path, encoding='utf-8').read()
    if path.endswith(('.yaml', '.yml')):
        import yaml
        raw = yaml.safe_load(txt)
    else:
        raw = json.loads(txt)
    cfg = _merge(DEFAULTS, raw)
    cfg['_dir'] = os.path.dirname(os.path.abspath(path))
    for k in ('kn', 'egrn_ha'):
        if k not in cfg:
            raise ValueError('в конфиге участка не задано поле %r' % k)
    return cfg

def path_of(cfg, key, default=None):
    """Путь из конфига — относительно папки конфига."""
    v = cfg.get(key, default)
    return None if v is None else (v if os.path.isabs(v) else os.path.join(cfg['_dir'], v))
