# -*- coding: utf-8 -*-
"""blast-radius 回归：--files 显式清单模式（不走 svn），断言已知调用链边。

用法：python tests/test_blast.py（需先有全量库）
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qcodemap import blast
from qcodemap import config as cmod
from qcodemap.store import Store


def main():
    cfg = cmod.load_config()
    store = Store(cfg.db_path)
    failed = []
    try:
        t0 = time.time()
        out = blast.blast(store, cfg,
                          files=['gclient/framework/util/replay_util.py',
                                 'gclient/gameplay/logic_base/comps/avatar_scene_node.py'],
                          depth=1, json_out=True)
        cold_s = time.time() - t0

        # 已知边锚点：replay_util.GetPlayer 被 avatar_scene_node:365 调用
        # （callees 验证过的 VERIFIED 边；变更文件 replay_util.py 的 GetPlayer
        # 应把该调用点收入直接调用方）
        anchor = [d for d in out['direct_callers']
                  if d['caller_file'] == 'gclient/gameplay/logic_base/comps/avatar_scene_node.py'
                  and d['caller_line'] == 365 and d['target'] == 'GetPlayer']
        if not anchor:
            failed.append('闭包未命中已知边 GetPlayer <- avatar_scene_node.py:365')

        # import 级维度非空（replay_util 被多处 import）
        if not out.get('importers'):
            failed.append('import 级 importers 为空')

        t0 = time.time()
        blast.blast(store, cfg,
                    files=['gclient/framework/util/replay_util.py',
                           'gclient/gameplay/logic_base/comps/avatar_scene_node.py'],
                    depth=1, json_out=True)
        warm_s = time.time() - t0

        print('cold=%.1fs warm=%.1fs direct=%d files=%d funcs=%d'
              % (cold_s, warm_s, len(out['direct_callers']),
                 len(out['changed_files']), len(out['changed_functions'])))
        # 监控不设限：冷查询参考线 30s，超了只提示
        if cold_s > 30:
            print('  监控: 冷查询 %.1fs 超过 30s 参考线（建议先跑一次 CLI 预热 edges）' % cold_s)
        if warm_s > 5:
            print('  监控: 热查询 %.1fs 超过 5s 参考线' % warm_s)
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
