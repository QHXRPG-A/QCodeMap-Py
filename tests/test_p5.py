# -*- coding: utf-8 -*-
"""P5: 子集安全、全仓新鲜度、profile、receiver/RPC/诊断与 blast 回归。"""

import ast
import copy
import json
import os
import shutil
import sys
import subprocess
import tempfile
import time

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

from qcodemap import blast, build, diagnostics, freshness, resolve, rpc_refs  # noqa: E402
from qcodemap import mcp_server  # noqa: E402
from qcodemap.config import Config  # noqa: E402
from qcodemap.hooks import FactsHooks  # noqa: E402
from qcodemap.scanner import dotted  # noqa: E402
from qcodemap.store import Store  # noqa: E402


CLIENT_ENTITY = '''class FestivalTargetEntity(object):
    def Activate(self):
        player.SendEndpoint('ActivateFestivalTarget')
'''
CLIENT_ENTITY_FIXED = '''class FestivalTargetEntity(object):
    SyncedField("activated", False, ALL_PEERS)
    def Activate(self):
        player.SendEndpoint('ActivateFestivalTarget')
'''
UI = '''class UI(object):
    def _ActivateFestivalTargetEntity(self):
        target = self.selected_object
        return target.Activate()
    def _ActivateStorageCrateEntity(self):
        target = self.selected_object
        return target.Activate()
'''
OTHER = '''class StorageCrateEntity(object):
    def Activate(self):
        return True
    def Other(self):
        return False
'''
SERVER = '''class FestivalTargetEntity(object):
    SyncedField("activated", False, ALL_PEERS)

class GameEndpoint(object):
    @endpoint_handler(INBOUND)
    def ActivateFestivalTarget(self, entity_id):
        return entity_id

class NameCollision(object):
    def ActivateFestivalTarget(self, entity_id):
        return entity_id
'''


def _write(root, rel, text):
    path = os.path.join(root, rel.replace('/', os.sep))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)
    os.utime(path, (time.time() + 2, time.time() + 2))
    return path


class FixtureHooks(FactsHooks):

    def receiver_type_facts(self, call, ctx):
        if not isinstance(call.func, ast.Attribute) or call.func.attr != 'Activate':
            return []
        receiver = dotted(call.func.value)
        if not receiver or ctx.function_node is None:
            return []
        local_name = receiver.split('.', 1)[0]
        selected = any(
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == local_name
                    for target in node.targets)
            and dotted(node.value) == 'self.selected_object'
            for node in ast.walk(ctx.function_node))
        prefix = '_Activate'
        if selected and ctx.func and ctx.func.startswith(prefix):
            return [(receiver, ctx.func[len(prefix):], 'framework',
                     'neutral selected-object route')]
        return []

    def rpc_facts(self, call, ctx):
        if isinstance(call.func, ast.Attribute) and call.func.attr == 'SendEndpoint' \
                and call.args and isinstance(call.args[0], ast.Constant) \
                and isinstance(call.args[0].value, str):
            return [('C2S', call.args[0].value, None)]
        return []

    def handler_facts(self, fn, ctx):
        for decorator in fn.decorator_list:
            if isinstance(decorator, ast.Call) and dotted(decorator.func) == 'endpoint_handler' \
                    and decorator.args and dotted(decorator.args[0]) == 'INBOUND':
                return [('C2S', fn.name, ctx.cls, 'verified',
                         '@endpoint_handler(INBOUND)')]
        return []

    def callback_facts(self, stmt, ctx):
        if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
                and dotted(stmt.value.func) == 'SyncedField'):
            return []
        args = stmt.value.args
        if len(args) < 3 or not isinstance(args[0], ast.Constant) \
                or not isinstance(args[0].value, str) or dotted(args[2]) != 'ALL_PEERS':
            return []
        name = args[0].value
        return [('SYNCED_FIELD', name, '_on_set_%s' % name)]

    def project_diagnostics(self, store, cfg):
        rows = store.con.execute(
            'SELECT file,line,class,source FROM callback_raw WHERE kind=?',
            ('SYNCED_FIELD',)).fetchall()
        client = {(cls, source) for file, _line, cls, source in rows
                  if file.startswith('client/')}
        return [{
            'code': 'SYNCED_FIELD_CLIENT_MISSING',
            'severity': 'error',
            'file': file,
            'line': line,
            'class': cls,
            'property': source,
            'message': '%s.%s is missing from the client mirror' % (cls, source),
        } for file, line, cls, source in rows
            if file.startswith('server/') and (cls, source) not in client]


def main():
    tmp = tempfile.mkdtemp(prefix='qcodemap_p5_')
    failed = []
    try:
        root = os.path.join(tmp, 'src')
        paths = {
            'entity': 'client/entities/festival_target.py',
            'ui': 'client/ui.py', 'other': 'client/entities/storage_crate.py',
            'server': 'server/entities/festival_target.py',
            'data': 'client/data/generated.py',
        }
        _write(root, paths['entity'], CLIENT_ENTITY)
        _write(root, paths['ui'], UI)
        _write(root, paths['other'], OTHER)
        _write(root, paths['server'], SERVER)
        _write(root, paths['data'], 'class GeneratedSemantic(object):\n    value = RareToken\n')
        cfg = Config()
        cfg.root = root
        cfg.targets = ['client', 'server']
        cfg.exclude_dirs = set()
        cfg.exclude_files = []
        cfg.include_paths = []
        cfg.db_path = os.path.join(tmp, 'p5.db')
        cfg.hooks = FixtureHooks()
        cfg.index_profile_rules = [('client/data/**', 'semantic-only')]
        stats = build.build(cfg, rebuild=True, verbose=False)
        if stats['files'] != 5:
            failed.append('初建文件数错误: %s' % stats)

        store = Store(cfg.db_path)
        try:
            out = resolve.callers(store, cfg, paths['entity'], 'Activate')
            inferred = [i for i in out['items']
                        if i['level'] == 'FRAMEWORK-INFERRED']
            if len(inferred) != 1 or inferred[0]['file'] != paths['ui']:
                failed.append('receiver 未收敛到目标类型: %s' % out['items'])
            rpc = rpc_refs.rpc_refs(store, cfg, 'ActivateFestivalTarget')
            if rpc['n_rpc'] != 1 or rpc['n_handler'] != 1:
                failed.append('RPC 应为 1 call/1 handler: %s' % rpc)
            if not any(i['level'] == 'NAME-ONLY' for i in rpc['items']):
                failed.append('RPC 同名兜底未标 NAME-ONLY')
            diag = diagnostics.diagnose(store, cfg)
            if not any(i.get('property') == 'activated' for i in diag['issues']):
                failed.append('diagnose 未报客户端镜像字段缺口: %s' % diag)
            profile = store.con.execute(
                'SELECT profile FROM files WHERE path=?', (paths['data'],)).fetchone()[0]
            n_name = store.con.execute(
                'SELECT COUNT(*) FROM names n JOIN files f ON n.file=f.id '
                'WHERE f.path=? AND n.name=?', (paths['data'], 'RareToken')).fetchone()[0]
            n_def = store.con.execute(
                'SELECT COUNT(*) FROM classes WHERE file=? AND name=?',
                (paths['data'], 'GeneratedSemantic')).fetchone()[0]
            if profile != 'semantic-only' or n_name or n_def != 1:
                failed.append('semantic-only 结构/names 语义错误')
        finally:
            store.close()

        # 修复 Property 后不手工 build，查询前 auto 增量刷新。
        _write(root, paths['entity'], CLIENT_ENTITY_FIXED)
        meta = freshness.ensure_fresh(cfg, 'auto')
        if not meta['refreshed'] or meta['drift_count'] != 1:
            failed.append('auto 修改刷新失败: %s' % meta)
        store = Store(cfg.db_path)
        try:
            if any(i.get('property') == 'activated'
                   for i in diagnostics.diagnose(store, cfg)['issues']):
                failed.append('镜像字段修复后诊断未归零')
        finally:
            store.close()

        # MCP 同样走全仓 auto：RPC 两端改名后无手工 build 就得 1/1。
        mcp_method = 'ActivateFestivalTargetMcp'
        _write(root, paths['entity'], CLIENT_ENTITY_FIXED.replace(
            'ActivateFestivalTarget', mcp_method))
        _write(root, paths['server'], SERVER.replace(
            'ActivateFestivalTarget', mcp_method))
        old_loader = mcp_server.config_mod.load_config
        mcp_server.config_mod.load_config = lambda: cfg
        freshness._THROTTLE.pop(cfg.db_path, None)
        try:
            mcp_rpc = mcp_server._tool_rpc_refs({'method': mcp_method})
        finally:
            mcp_server.config_mod.load_config = old_loader
        if mcp_rpc['n_rpc'] != 1 or mcp_rpc['n_handler'] != 1 \
                or not mcp_rpc['index']['refreshed']:
            failed.append('MCP auto rpc-refs 应为最新 1/1: %s' % mcp_rpc)
        _write(root, paths['entity'], CLIENT_ENTITY_FIXED)
        _write(root, paths['server'], SERVER)
        freshness.ensure_fresh(cfg, 'auto')

        # 新增/删除同样由全仓文件集检查捕获。
        added = 'client/new_file.py'
        _write(root, added, 'def AddedByRefresh():\n    return 1\n')
        if freshness.ensure_fresh(cfg, 'auto')['drift_count'] != 1:
            failed.append('auto 新增刷新失败')
        os.remove(os.path.join(root, added.replace('/', os.sep)))
        if freshness.ensure_fresh(cfg, 'auto')['drift_count'] != 1:
            failed.append('auto 删除刷新失败')

        # 局部 targets 刷新不能删除范围外事实。
        partial = copy.copy(cfg)
        partial.targets = ['client']
        partial.targets_overridden = True
        before = Store(cfg.db_path)
        try:
            count_before = before.count('files')
        finally:
            before.close()
        partial_stats = build.build(partial, verbose=False)
        if partial_stats['vacuumed'] or not all(
                name in partial_stats['stages']
                for name in ('collect', 'scan', 'pass2', 'commit', 'vacuum')):
            failed.append('普通增量不应 VACUUM，且应返回分阶段耗时')
        after = Store(cfg.db_path)
        try:
            if after.count('files') != count_before or not after.con.execute(
                    'SELECT 1 FROM defs WHERE file=? AND name=?',
                    (paths['server'], 'ActivateFestivalTarget')).fetchone():
                failed.append('build --targets 删除了范围外索引')
        finally:
            after.close()

        # check 只报漂移，off 不扫描；profile 变化拒绝旧库。
        _write(root, paths['other'], OTHER + '\ndef Later():\n    return 2\n')
        check = freshness.ensure_fresh(cfg, 'check')
        off = freshness.ensure_fresh(cfg, 'off')
        if not check.get('stale') or check['drift_count'] != 1 or off['drift_count'] != 0:
            failed.append('refresh check/off 语义错误: %s / %s' % (check, off))
        freshness.ensure_fresh(cfg, 'auto')
        incompatible = copy.copy(cfg)
        incompatible.index_profile_rules = []
        try:
            freshness.ensure_fresh(incompatible, 'off')
            failed.append('配置指纹变化未拒绝旧库')
        except RuntimeError:
            pass

        # CLI 统一参数 + Windows 无 PYTHONUTF8 环境的中文 JSON。
        cli_root = os.path.join(tmp, 'cli_src')
        _write(cli_root, 'src/demo.py', 'def Hello():\n    return 1\n')
        cli_db = os.path.join(tmp, 'cli.db')
        custom_dir = os.path.join(tmp, 'custom')
        os.makedirs(custom_dir)
        with open(os.path.join(custom_dir, 'config.py'), 'w', encoding='utf-8') as f:
            f.write('ROOT = %r\nTARGETS = ["src"]\nDB_PATH = %r\n'
                    % (cli_root, cli_db))
        env = os.environ.copy()
        env.pop('PYTHONUTF8', None)
        env.pop('PYTHONIOENCODING', None)
        base = [sys.executable, '-m', 'qcodemap', '--custom', custom_dir]
        subprocess.run(base + ['build', '--rebuild', '--json'], cwd=PROJECT,
                       env=env, check=True, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE)
        raw = subprocess.run(
            base + ['usages', 'class X', '--json', '--refresh', 'off'],
            cwd=PROJECT, env=env, check=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE).stdout
        cli_out = json.loads(raw.decode('utf-8'))
        if '只接受单个 Python 标识符' not in cli_out['note'] \
                or 'index' not in cli_out:
            failed.append('CLI UTF-8/非法 usages/index 元数据失败: %s' % cli_out)
        for command in ('callers', 'callees', 'usages', 'rpc-refs', 'deps',
                        'hubs', 'tree', 'find', 'context', 'defs', 'diagnose'):
            help_text = subprocess.run(
                base + [command, '--help'], cwd=PROJECT, env=env,
                check=True, stdout=subprocess.PIPE).stdout.decode('utf-8')
            if not all(flag in help_text for flag in
                       ('--root', '--db', '--json', '--refresh')):
                failed.append('CLI 统一参数缺失: %s' % command)

        # AST end_lineno：单 hunk 不播种相邻函数；类头变化播种全类。
        diff = '@@ -2,1 +2,1 @@\n-    return 1\n+    return 2\n'
        store = Store(cfg.db_path)
        try:
            funcs = blast.changed_functions(store, cfg, paths['other'], diff, OTHER)
        finally:
            store.close()
        if {f['func'] for f in funcs} != {'Activate'}:
            failed.append('blast 单函数 hunk 播种错误: %s' % funcs)
        class_diff = '@@ -1,1 +1,1 @@\n-class StorageCrateEntity(object):\n+class StorageCrateEntity(Base):\n'
        store = Store(cfg.db_path)
        try:
            funcs = blast.changed_functions(store, cfg, paths['other'], class_diff, OTHER)
        finally:
            store.close()
        if not {'Activate', 'Other'} <= {f['func'] for f in funcs}:
            failed.append('blast 类头未播种全类: %s' % funcs)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failed:
        print('FAIL:')
        for item in failed:
            print('  -', item)
        return 1
    print('PASS（P5 全面回归）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
