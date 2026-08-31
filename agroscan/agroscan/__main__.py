# -*- coding: utf-8 -*-
"""Точка входа: python -m agroscan <команда>."""
import glob
import os
import sys

USAGE = """agroscan — анализ зарастания и подготовка схем ЧЗУ

  python -m agroscan run <конфиг.yaml> [--no-sheets] [--no-formats] [--no-cache]
  python -m agroscan batch <шаблон|папка> [--log журнал.json]
  python -m agroscan new <кадастровый:номер> [--zone msk58-2]
  python -m agroscan cache [--clear]
"""

def main(argv):
    if len(argv) < 2:
        print(USAGE); return 1
    cmd = argv[1]; args = argv[2:]
    flag = lambda f: f in args
    opt = lambda f, d=None: args[args.index(f) + 1] if f in args and args.index(f) + 1 < len(args) else d
    pos = [a for a in args if not a.startswith('--') and args[args.index(a) - 1] not in ('--log', '--zone')]

    if cmd == 'run':
        from .pipeline import run
        run(pos[0], sheets=not flag('--no-sheets'), formats=not flag('--no-formats'),
            no_cache=flag('--no-cache'))
    elif cmd == 'batch':
        from .batch import process
        p = pos[0]
        cfgs = sorted(glob.glob(os.path.join(p, '*.y*ml')) if os.path.isdir(p) else glob.glob(p))
        if not cfgs:
            print('не найдено конфигов по «%s»' % p); return 1
        process(cfgs, log_path=opt('--log', 'batch_log.json'),
                sheets=not flag('--no-sheets'), formats=not flag('--no-formats'))
    elif cmd == 'new':
        kn = pos[0]; zone = opt('--zone', 'msk58-2')
        tag = kn.replace(':', '-')
        path = os.path.join('parcels', tag + '.yaml')
        os.makedirs('parcels', exist_ok=True)
        if os.path.exists(path):
            print('уже есть: %s' % path); return 1
        open(path, 'w', encoding='utf-8').write(TEMPLATE % {'kn': kn, 'zone': zone, 'tag': tag})
        print('создан %s — заполните egrn_ha и пути к данным' % path)
    elif cmd == 'cache':
        from . import cache
        if flag('--clear'):
            print('удалено файлов:', cache.clear())
        else:
            print(cache.stats())
    else:
        print(USAGE); return 1
    return 0

TEMPLATE = """# Участок %(kn)s
kn: "%(kn)s"
egrn_ha: 0.0            # площадь по сведениям ЕГРН, га — обязательно
zone: %(zone)s
place: ""

# пути относительно этого файла
rings: ../data/%(tag)s/rings.json      # граница ЗУ из КПТ: [[[E,N],...], ...]
meta:  ../data/%(tag)s/bgmeta.json     # {e0,e1,n0,n1,W,H,mpp}
image: ../data/%(tag)s/bg.jpg          # ортофото для расчёта крон
# zouit:   ../data/%(tag)s/zouit.npy   # ЗОУИТ растром, если есть
# markup:  ../data/%(tag)s/markup.json # разметка правообладателя
# neighbors: ../data/%(tag)s/neighbors.json

parts:
  "1": "покрыта древесной и кустарниковой растительностью (раскорчёвка)"
  "2": "не обработана, травяная и кустарниковая растительность (залежь)"
  "3": "зона с особыми условиями использования территории"
  "4": "защитные лесные насаждения (полезащитные лесные полосы)"
priority: ["3", "4", "1", "2"]

belts:
  source: manual        # manual | auto | both

backdrops: []           # [{path, caption, note}] для проверочной карты
export:
  simplify_m: 0.0
"""

if __name__ == '__main__':
    sys.exit(main(sys.argv))
