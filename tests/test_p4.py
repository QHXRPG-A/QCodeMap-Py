# -*- coding: utf-8 -*-
"""P4 第一轮回归：覆盖率契约 + find_file/get_file_context/context + 懒刷新。

自建临时小库（不碰主库）：含一个语法错误文件，覆盖 partial 契约与三新命令，
最后 touch 正常文件验证 drift_check 检测。

用法：python tests/test_p4.py
"""

import json
import os
import shutil
import sys
import tempfile
import time

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

from qcodemap import build as build_mod          # noqa: E402
from qcodemap import context as ctx_mod          # noqa: E402
from qcodemap import resolve as rmod             # noqa: E402
from qcodemap import structure as st_mod         # noqa: E402
from qcodemap.config import Config               # noqa: E402
from qcodemap.store import Store                 # noqa: E402

# 目标文件取自主库真实路径（临时库只建这几个文件，路径假造到同前缀下）
F_GOOD = 'client/gameplay/demo_good.py'
F_BAD = 'client/gameplay/demo_bad.py'

GOOD_SRC = '''# -*- coding: utf-8 -*-
from client.gameplay import helper


class Demo(object):
    def ping(self):
        return helper.echo()

    def pong(self):
        return self.ping()
'''

BAD_SRC = '''def broken(:
    pass
'''

HELPER_SRC = '''def echo():
    return 1
'''

HELPER = 'client/gameplay/helper.py'


def _make_cfg(root, db):
    cfg = Config()
    cfg.root = root
    cfg.targets = ['client']
    cfg.db_path = db
    cfg.hooks = None
    return cfg


def main():
    tmp = tempfile.mkdtemp(prefix='qcodemap_p4_')
    failed = []
    try:
        root = os.path.join(tmp, 'src').replace('\\', '/')
        os.makedirs(os.path.join(root, 'client/gameplay'))
        for rel, src in ((F_GOOD, GOOD_SRC), (F_BAD, BAD_SRC), (HELPER, HELPER_SRC)):
            with open(os.path.join(root, rel.replace('/', os.sep)), 'w') as f:
                f.write(src)
        db = os.path.join(tmp, 'p4.db').replace('\\', '/')
        cfg = _make_cfg(root, db)
        stats = build_mod.build(cfg, verbose=False)
        if stats['parse_failed'] != 1:
            failed.append('parse_failed 应为 1，实际 %s' % stats['parse_failed'])

        store = Store(db)
        try:
            # ---- 覆盖率契约 ----
            out = rmod.callers(store, cfg, F_GOOD, 'ping')
            cov = out.get('coverage')
            if not cov or cov['status'] != 'partial' or cov['parse_failed'] != 1:
                failed.append('callers coverage 应为 partial/1: %s' % cov)
            out = rmod.callers(store, cfg, F_BAD, 'broken')
            if 'ast 解析失败' not in (out.get('note') or ''):
                failed.append('坏文件定义未找到的 note 应说明 ast 失败: %s'
                              % out.get('note'))
            cov = st_mod.deps(store, 'client/gameplay', json_out=True)['coverage']
            if cov['status'] != 'partial' or 'issues' not in cov:
                failed.append('deps coverage 应 partial 带 issues: %s' % cov)
            # importers 的 scope 是被引用目标（helper 正常）-> complete；
            # 引用方坏文件不进 scope，语义与 deps（scope=查询目标集）一致
            cov = st_mod.importers(store, HELPER, json_out=True)['coverage']
            if cov['status'] != 'complete' or cov['parse_failed'] != 0:
                failed.append('importers coverage 应 complete（目标文件正常）: %s'
                              % cov)

            # ---- find_file ----
            out = ctx_mod.find_file(store, 'demo_goo', json_out=True)
            if out['count'] != 1 or out['matches'][0]['file'] != F_GOOD:
                failed.append('find_file 应命中 demo_good: %s' % out)
            out = ctx_mod.find_file(store, 'demo_g00d', json_out=True)
            if out['count'] != 0:
                failed.append('find_file 转义后不应命中 demo_g00d: %s' % out)

            # ---- get_file_context ----
            out = ctx_mod.get_file_context(store, cfg, F_GOOD, json_out=True)
            if out['file'] != F_GOOD or not out['defs'] or not out['classes']:
                failed.append('get_file_context 定义/类为空: %s' % list(out))
            if HELPER not in out['imports']:
                failed.append('get_file_context imports 应含 helper: %s'
                              % out['imports'])
            out2 = ctx_mod.get_file_context(store, cfg, 'no/such.py', json_out=True)
            if '不在索引内' not in (out2.get('note') or ''):
                failed.append('get_file_context 不存在目标应带 note: %s' % out2)

            # ---- context ----
            out = ctx_mod.context(store, cfg, json_out=True)
            if out['schema_version'] != 'qcodemap.context/v1':
                failed.append('context schema 版本不符: %s' % out['schema_version'])
            if out['stats']['parse_failed'] != 1 or not out['hubs'] \
                    or not out['top_dirs']:
                failed.append('context 统计/枢纽/目录为空: %s' % out['stats'])
            out = ctx_mod.context(store, cfg, compact=True, json_out=True)
            if len(out['hubs']) > 10 or not out['compact']:
                failed.append('context compact 截断失效: %d' % len(out['hubs']))
        finally:
            store.close()

        # ---- 懒刷新 ----
        good_abs = os.path.join(root, F_GOOD.replace('/', os.sep))
        with open(good_abs, 'a') as f:
            f.write('\n\ndef added_later(self):\n    return 2\n')
        os.utime(good_abs, None)
        store = Store(db)
        try:
            drifted = build_mod.drift_check(store, cfg, [F_GOOD, HELPER])
            if drifted != [F_GOOD]:
                failed.append('drift_check 应只报 demo_good: %s' % drifted)
        finally:
            store.close()
        build_mod.build(cfg, verbose=False)
        store = Store(db)
        try:
            defs = store.con.execute(
                'SELECT COUNT(*) FROM defs WHERE file=? AND name=?',
                (F_GOOD, 'added_later')).fetchone()[0]
            if defs != 1:
                failed.append('增量后 added_later 应可查')
            if store.parse_failed_count() != 1:
                failed.append('刷新后 parse_failed 仍应为 1')
        finally:
            store.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failed:
        print('FAIL:')
        for f in failed:
            print('  -', f)
        return 1
    print('PASS（覆盖率契约 + 三新命令 + 懒刷新）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
