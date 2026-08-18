# -*- coding: utf-8 -*-
"""规模回归：驱动正式包对全库做建库/增量/查询基准断言。

用法：
  python tests/test_scale.py            # 增量模式（快）：校验库存在且指标不劣化
  python tests/test_scale.py --rebuild  # 全量重建（约 80s）：完整基准
验收线（REQUIREMENTS §4）：全量 ≤90s、增量 ≤5s、单函数查询 ≤5s。
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qcodemap import config as cmod
from qcodemap import build as bmod
from qcodemap import resolve as rmod
from qcodemap.store import Store

# 全量基线（2026-08-17 索引范围扩展后：七目录 + gclient/data + gserver/data
# + classutils.py，origin 明文版入库）。文件数走容差（目标库自然演进 ±1%），
# 其余为验收线/监控线（本阶段不设硬限，仅 FAIL 判据保留结构正确性）
BASE_FILES = 8925
BASE_BUILD_S = 90.0      # 验收线
BASE_DB_MB = 500.0       # 验收线（扩展后实测 441MB）
BASE_QUERY_S = 5.0       # 验收线（含语义验证）
BASE_CACHE_S = 0.5       # 验收线（edges 命中）


def main():
    rebuild = '--rebuild' in sys.argv
    cfg = cmod.load_config()
    if not rebuild and not os.path.exists(cfg.db_path):
        print('库不存在，先全量建库')
        rebuild = True
    t0 = time.time()
    stats = bmod.build(cfg, rebuild=rebuild)
    full_s = time.time() - t0

    db_mb = os.path.getsize(cfg.db_path) / 1048576
    store = Store(cfg.db_path)
    try:
        # 查询基准：高频名 + 语义验证
        tq = time.time()
        out1 = rmod.callers(store, cfg,
                            'gclient/gameplay/logic_base/entities/combatavatarmembers/'
                            'cimp_combat_unit.py', 'GetTeammateInfo')
        query_s = time.time() - tq
        tq2 = time.time()
        out2 = rmod.callers(store, cfg,
                            'gclient/gameplay/logic_base/entities/combatavatarmembers/'
                            'cimp_combat_unit.py', 'GetTeammateInfo')
        cache_s = time.time() - tq2
    finally:
        store.close()

    failed = []
    if abs(stats['files'] - BASE_FILES) > BASE_FILES * 0.01:
        failed.append('files=%d 基线 %d 漂移超 1%%（目标库大范围增删？确认后更新基线）'
                      % (stats['files'], BASE_FILES))
    if full_s > BASE_BUILD_S:
        failed.append('全量 %.1fs > %.1fs' % (full_s, BASE_BUILD_S))
    if db_mb > BASE_DB_MB:
        failed.append('库体量 %.0fMB > %.0fMB' % (db_mb, BASE_DB_MB))
    if query_s > BASE_QUERY_S:
        failed.append('查询 %.2fs > %.2fs' % (query_s, BASE_QUERY_S))
    if cache_s > BASE_CACHE_S:
        failed.append('缓存查询 %.3fs > %.2fs' % (cache_s, BASE_CACHE_S))
    if not out2.get('cached'):
        failed.append('二次查询未命中缓存')

    print('files=%d names=%d build=%.1fs db=%.0fMB query=%.2fs cache=%.3fs '
          'VERIFIED=%d CANDIDATE=%d'
          % (stats['files'], stats['names'], full_s, db_mb, query_s, cache_s,
             out1['n_verified'], out1['n_candidate']))
    if failed:
        print('FAIL:')
        for f in failed:
            print('  -', f)
        return 1
    print('PASS')
    return 0


if __name__ == '__main__':
    sys.exit(main())
