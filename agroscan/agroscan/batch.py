# -*- coding: utf-8 -*-
"""Пакетная обработка участков.

Один упавший участок не должен ронять очередь из сотни: ошибка ловится,
пишется в журнал и работа продолжается. Журнал — то, по чему принимают
результат пачки, поэтому в нём и площади, и непройденные проверки.
"""
import json
import os
import time
import traceback

from .pipeline import run

def process(configs, out_root=None, log_path=None, **kw):
    rows = []
    t0 = time.time()
    for i, cfg in enumerate(configs, 1):
        name = os.path.splitext(os.path.basename(cfg))[0]
        print('\n[%d/%d] %s' % (i, len(configs), name), flush=True)
        rec = {'конфиг': cfg, 'участок': name, 'начат': time.strftime('%Y-%m-%d %H:%M:%S')}
        t = time.time()
        try:
            res, qa = run(cfg, out_dir=(os.path.join(out_root, name) if out_root else None), **kw)
            rec.update({'статус': 'готово' if qa['пройдено'] else 'проверки не пройдены',
                        'части': {k: round(v['areaHa'], 4) for k, v in res.items()},
                        'сумма_га': round(sum(v['areaHa'] for v in res.values()), 4),
                        'провалено': qa['провалено']})
        except Exception as e:
            rec.update({'статус': 'ошибка', 'ошибка': '%s: %s' % (type(e).__name__, e),
                        'трассировка': traceback.format_exc().splitlines()[-4:]})
            print('   ОШИБКА: %s' % e, flush=True)
        rec['секунд'] = round(time.time() - t, 1)
        rows.append(rec)
    ok = sum(1 for r in rows if r['статус'] == 'готово')
    summary = {'участков': len(rows), 'готово': ok, 'с_замечаниями': len(rows) - ok,
               'всего_секунд': round(time.time() - t0, 1), 'записи': rows}
    if log_path:
        json.dump(summary, open(log_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('\n' + '─' * 74)
    print('%-26s %-22s %10s %8s' % ('участок', 'статус', 'сумма, га', 'сек'))
    for r in rows:
        print('%-26s %-22s %10s %8.1f'
              % (r['участок'][:26], r['статус'][:22], r.get('сумма_га', '—'), r['секунд']))
    print('─' * 74)
    print('готово %d из %d, всего %.0f с' % (ok, len(rows), summary['всего_секунд']))
    for r in rows:
        if r['статус'] != 'готово':
            print('  %s: %s' % (r['участок'], r.get('ошибка') or ', '.join(r.get('провалено', []))))
    return summary
