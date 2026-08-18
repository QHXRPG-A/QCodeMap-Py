# -*- coding: utf-8 -*-
"""结构四命令回归：deps/importers/hubs/tree 输出正确性与性能。

用法：python tests/test_structure.py（需先有全量库）
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qcodemap import config as cmod
from qcodemap import structure as st
from qcodemap.store import Store


def main():
    cfg = cmod.load_config()
    store = Store(cfg.db_path)
    failed = []
    try:
        # importers：已知组件文件应有多引用且判枢纽
        t0 = time.time()
        out = st.importers(store, 'gclient/gameplay/logic_base/comps/avatar_scene_node.py',
                           json_out=True)
        t_imp = time.time() - t0
        if out['count'] < 5 or not out['hub']:
            failed.append('importers count=%d hub=%s' % (out['count'], out['hub']))
        if out['coverage']['status'] != 'complete':
            failed.append('importers coverage 缺失')

        # hubs：Top 锚点方向（consts/events/cconst 应在前列）+ 耗时
        t0 = time.time()
        out = st.hubs(store, top=25, json_out=True)
        t_hubs = time.time() - t0
        top10 = [h['file'] for h in out['hubs'][:10]]
        for anchor in ('gshare/consts.py', 'gclient/framework/util/events.py'):
            if anchor not in top10:
                failed.append('hubs 锚点 %s 不在 Top10: %s' % (anchor, top10))

        # deps：内域解析率——七个 target 前缀的 import 应大多解析到文件
        #（unresolved 是 common/引擎模块/stdlib 等真外部，不计入分母）
        t0 = time.time()
        out = st.deps(store, 'gclient/framework', json_out=True)
        t_deps = time.time() - t0
        cov = out['coverage']
        prefixes = tuple(cfg.targets)
        rows = store.con.execute(
            'SELECT module FROM imports WHERE file LIKE ?', ('gclient/framework/%',)).fetchall()
        internal = [m for (m,) in rows if m.startswith(prefixes)]
        # 抽验：内域 module 在 modmap 中应可解析（from X import Y 的子模块形态
        # 由 deps 内部处理，这里只验 module 本身的命中率）
        from qcodemap.scanner import module_of
        modmap = {module_of(rel): rel for (rel,) in
                  store.con.execute('SELECT path FROM files')}
        hit = sum(1 for m in internal if m in modmap)
        if internal and hit / len(internal) < 0.8:
            failed.append('deps 内域 module 命中率 %.0f%%（%d/%d）'
                          % (100 * hit / len(internal), hit, len(internal)))
        if cov['resolved'] == 0:
            failed.append('deps 零内部边')

        # tree：总数与库一致
        t0 = time.time()
        out = st.tree(store, cfg, depth=2, json_out=True)
        t_tree = time.time() - t0
        if out['total_files'] != store.count('files'):
            failed.append('tree total_files=%d != 库 %d'
                          % (out['total_files'], store.count('files')))

        # 性能监控（本阶段不设限，只打印）
        print('importers=%.2fs hubs=%.2fs deps=%.2fs tree=%.2fs'
              % (t_imp, t_hubs, t_deps, t_tree))
        for name, t in (('importers', t_imp), ('hubs', t_hubs),
                        ('deps', t_deps), ('tree', t_tree)):
            if t > 2.0:
                print('  监控: %s %.2fs 超过 2s 参考线（不设限，仅记录）' % (name, t))
    finally:
        store.close()
    if failed:
        print('FAIL:')
        for f in failed:
            print('  -', f)
        return 1
    print('PASS')
    return 0


if __name__ == '__main__':
    sys.exit(main())
