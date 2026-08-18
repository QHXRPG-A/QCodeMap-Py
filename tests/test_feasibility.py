# -*- coding: utf-8 -*-
"""语义链路回归：10 文件建库 + 可行性基准 5/5 边回归（驱动正式包）。

用法：python tests/test_feasibility.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qcodemap import config as cmod
from qcodemap import scanner
from qcodemap.build import _resolve_comps
from qcodemap.store import Store

SMALL_DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'cache', 'smoke.db')

FILES = [
    'gclient/gameplay/logic_base/entities/combat_avatar.py',
    'gclient/gameplay/logic_base/entities/combatavatarmembers/__init__.py',
    'gclient/gameplay/logic_base/entities/combatavatarmembers/cimp_combat_unit.py',
    'gclient/gameplay/logic_base/entities/combatavatarmembers/cimp_replay.py',
    'gclient/framework/entities/space.py',
    'gclient/gameplay/logic_base/entities/combat_team.py',
    'gclient/gameplay/logic_base/comps/avatar_scene_node.py',
    'gclient/gameplay/logic_base/comps/comp_toplogo.py',
    'gclient/gameplay/logic_base/comps/comp_mark.py',
    'gclient/framework/util/replay_util.py',
]

# (函数, 定义文件, 已知调用方 file:line) —— 可行性验证的标准答案
TARGETS = [
    ('RefreshAiTakeoverToplogo', 'gclient/gameplay/logic_base/comps/avatar_scene_node.py',
     ['gclient/gameplay/logic_base/comps/avatar_scene_node.py:272',
      'gclient/gameplay/logic_base/comps/comp_toplogo.py:368']),
    ('RemoveDummyEntity', 'gclient/framework/entities/space.py',
     ['gclient/gameplay/logic_base/entities/combat_team.py:359',
      'gclient/gameplay/logic_base/entities/combatavatarmembers/cimp_replay.py:80']),
    ('GetTeammateInfo', 'gclient/gameplay/logic_base/entities/combatavatarmembers/cimp_combat_unit.py',
     ['gclient/gameplay/logic_base/comps/avatar_scene_node.py:366']),
]


def build_small(cfg):
    if os.path.exists(SMALL_DB):
        os.remove(SMALL_DB)
    store = Store(SMALL_DB)
    for rel in FILES:
        rows = scanner.scan_file(rel, os.path.join(cfg.root, rel.replace('/', os.sep)),
                                 cfg.hooks)
        fid = store.insert_file(rel, os.path.getmtime(
            os.path.join(cfg.root, rel.replace('/', os.sep))))
        store.insert_rows(fid, rows)
    store.con.executemany(
        'INSERT INTO comp VALUES(?,?,?)',
        _resolve_comps(store, cfg))
    store.commit()
    return store


def main():
    cfg = cmod.load_config()
    t0 = time.time()
    store = build_small(cfg)
    print('小样建库: %d 文件 names=%d classes=%d attr=%d genv=%d ret=%d comp=%d  %.1fs'
          % (len(FILES), store.count('names'), store.count('classes'),
             store.count('attr'), store.count('global_assign'), store.count('ret'),
             store.count('comp'), time.time() - t0))
    from qcodemap import resolve as rmod
    r = rmod.Resolver(store, cfg)
    total_hit = total_ans = 0
    for (name, def_file, answers) in TARGETS:
        hits = []
        cands = store.con.execute(
            'SELECT f.path, n.line FROM names n JOIN files f ON n.file=f.id '
            'WHERE n.name=? ORDER BY f.path, n.line', (name,)).fetchall()
        for (f, ln) in cands:
            try:
                text = r._lines(f)[ln - 1]
                idx = text.index(name)
            except (IndexError, ValueError):
                continue
            if not text[idx + len(name):].lstrip()[:1] == '(':
                continue
            got = r.resolve_call(f, ln, name)
            if got and got[0] == def_file:
                hits.append('%s:%d' % (f, ln))
        ah = [a for a in answers if a in hits]
        total_hit += len(ah)
        total_ans += len(answers)
        print('%-28s 候选=%d 命中 %d/%d: %s'
              % (name, len(cands), len(ah), len(answers), ah))
        extra = [h for h in hits if h not in answers]
        if extra:
            print('%-28s 额外边: %s' % ('', extra))
    store.close()
    print('总命中 %d/%d  总耗时 %.1fs' % (total_hit, total_ans, time.time() - t0))
    return 0 if total_hit == total_ans else 1


if __name__ == '__main__':
    sys.exit(main())
