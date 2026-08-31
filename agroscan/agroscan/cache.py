# -*- coding: utf-8 -*-
"""Кэш загруженных данных ДЗЗ.

Повторный прогон участка заново тянул шесть сцен Sentinel-2 — 80 секунд из
173. При сотне участков это часы на пересчёт того же самого. Ключ кэша —
источник, охват и параметры запроса: изменился охват — данные перезагрузятся.
"""
import hashlib
import json
import os

import numpy as np

def dir_for(sub=''):
    d = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'cache', sub)
    os.makedirs(d, exist_ok=True)
    return d

def key(source, **params):
    raw = json.dumps({'src': source, **params}, sort_keys=True, default=str)
    return '%s_%s' % (source, hashlib.sha1(raw.encode()).hexdigest()[:16])

def get_array(k):
    p = os.path.join(dir_for('arrays'), k + '.npy')
    return np.load(p) if os.path.exists(p) else None

def put_array(k, a):
    if a is None:
        return None
    p = os.path.join(dir_for('arrays'), k + '.npy')
    np.save(p, a)
    return p

def get_json(k):
    p = os.path.join(dir_for('json'), k + '.json')
    return json.load(open(p, encoding='utf-8')) if os.path.exists(p) else None

def put_json(k, obj):
    p = os.path.join(dir_for('json'), k + '.json')
    json.dump(obj, open(p, 'w', encoding='utf-8'), ensure_ascii=False)
    return p

def stats():
    a = dir_for('arrays'); j = dir_for('json')
    files = [os.path.join(a, f) for f in os.listdir(a)] + [os.path.join(j, f) for f in os.listdir(j)]
    return {'файлов': len(files), 'мб': round(sum(os.path.getsize(f) for f in files) / 1048576, 1)}

def clear():
    n = 0
    for sub in ('arrays', 'json'):
        d = dir_for(sub)
        for f in os.listdir(d):
            os.remove(os.path.join(d, f)); n += 1
    return n
