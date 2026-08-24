# -*- coding: utf-8 -*-
"""P7: 符号消歧、endpoint 宿主/别名、严格 RPC、混合路径与覆盖问题。"""

import ast
import os
import shutil
import sys
import tempfile

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

from qcodemap import build, path_query, resolve, rpc_refs  # noqa: E402
from qcodemap.config import Config  # noqa: E402
from qcodemap.hooks import FactsHooks  # noqa: E402
from qcodemap.scanner import dotted  # noqa: E402
from qcodemap.store import Store  # noqa: E402


CLIENT = '''class Sender(object):
    def Start(self):
        return self.Step()

    def Step(self):
        bus.SendEndpoint("Handle")
'''

SERVER = '''class BaseHandler(object):
    def Handle(self):
        return False

class HandlerPart(object):
    @endpoint_handler(INBOUND)
    def Handle(self):
        return True

@Composes(HandlerPart)
class ServerHost(BaseHandler):
    EXPOSED_AS = "ClientHost"
'''

OTHER = '''class Other(object):
    def Handle(self):
        return False
'''


class FixtureHooks(FactsHooks):

    def class_facts(self, cd, ctx):
        rows = []
        for decorator in cd.decorator_list:
            if not (isinstance(decorator, ast.Call)
                    and dotted(decorator.func) == 'Composes'):
                continue
            for arg in decorator.args:
                if isinstance(arg, ast.Name):
                    rows.append(('comp_raw',
                                 (ctx.rel, cd.name, 'ref', arg.id)))
        return rows

    def rpc_facts(self, call, ctx):
        if isinstance(call.func, ast.Attribute) \
                and call.func.attr == 'SendEndpoint' \
                and call.args and isinstance(call.args[0], ast.Constant):
            return [('IN', call.args[0].value, None)]
        return []

    def handler_facts(self, fn, ctx):
        if any(isinstance(dec, ast.Call)
               and dotted(dec.func) == 'endpoint_handler'
               for dec in fn.decorator_list):
            return [('IN', fn.name, ctx.cls, 'verified',
                     'neutral endpoint decorator')]
        return []

    def endpoint_aliases(self, tree, ctx):
        rows = []
        for cls in (node for node in ast.walk(tree)
                    if isinstance(node, ast.ClassDef)):
            for stmt in cls.body:
                if not (isinstance(stmt, ast.Assign)
                        and any(isinstance(target, ast.Name)
                                and target.id == 'EXPOSED_AS'
                                for target in stmt.targets)
                        and isinstance(stmt.value, ast.Constant)
                        and isinstance(stmt.value.value, str)):
                    continue
                rows.append((stmt.lineno, cls.name, stmt.value.value,
                             'declared', 'neutral endpoint alias'))
        return rows


def _write(root, rel, text):
    path = os.path.join(root, rel.replace('/', os.sep))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as file_obj:
        file_obj.write(text)


def main():
    tmp = tempfile.mkdtemp(prefix='qcodemap_p7_')
    failed = []
    try:
        root = os.path.join(tmp, 'src')
        _write(root, 'app/client.py', CLIENT)
        _write(root, 'app/server.py', SERVER)
        _write(root, 'app/other.py', OTHER)
        _write(root, 'app/broken.py', 'Handle(\n')
        cfg = Config()
        cfg.root = root
        cfg.targets = ['app']
        cfg.exclude_dirs = set()
        cfg.exclude_files = []
        cfg.db_path = os.path.join(tmp, 'p7.db')
        cfg.hooks = FixtureHooks()
        build.build(cfg, rebuild=True, verbose=False)

        store = Store.open_reader(cfg.db_path)
        try:
            unique = resolve.callers_by_symbol(store, cfg, 'Sender.Step')
            if unique['resolution']['status'] != 'resolved' or not any(
                    item['level'] == 'VERIFIED'
                    and item['caller'] == 'Sender.Start'
                    for item in unique['items']):
                failed.append('callers 符号自动定位失败: %s' % unique)
            ambiguous = resolve.callers_by_symbol(store, cfg, 'Handle')
            if ambiguous['resolution']['status'] != 'ambiguous' \
                    or len(ambiguous['resolution']['candidates']) != 3:
                failed.append('callers 歧义候选错误: %s' % ambiguous)
            if not any(issue['file'] == 'app/broken.py'
                       for issue in ambiguous['coverage']['issues']):
                failed.append('callers 未列相关 parse-failed 文件: %s'
                              % ambiguous['coverage'])

            runtime = resolve.resolve_symbol(store, cfg, 'ServerHost.Handle')
            if runtime['status'] != 'resolved' \
                    or runtime['selected'].get('via') != 'runtime-host' \
                    or runtime['selected']['class'] != 'HandlerPart':
                failed.append('组件宿主符号定位失败: %s' % runtime)

            strict = rpc_refs.rpc_refs(store, cfg, 'Handle', stub='ServerHost')
            if strict['n_rpc'] != 1 or strict['n_handler'] != 1 \
                    or strict['n_name_only'] != 0:
                failed.append('严格 endpoint 配对失败: %s' % strict)
            alias = rpc_refs.rpc_refs(store, cfg, 'Handle', stub='ClientHost')
            if alias['n_rpc'] != 1 or alias['n_handler'] != 1:
                failed.append('endpoint alias 配对失败: %s' % alias)
            missing = rpc_refs.rpc_refs(store, cfg, 'Handle', stub='MissingHost')
            if missing['items'] or not any(
                    item['code'] == 'NO_RESULT' for item in missing['unmatched']):
                failed.append('严格过滤未解释空结果: %s' % missing)

            chain = path_query.path(
                store, cfg, 'Sender.Start', 'ServerHost.Handle', max_depth=3)
            kinds = [edge['kind'] for edge in
                     (chain.get('path') or {}).get('edges', [])]
            if not chain['found'] or kinds != ['call', 'rpc']:
                failed.append('混合调用路径失败: %s' % chain)
        finally:
            store.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failed:
        print('FAIL:')
        for item in failed:
            print('  -', item)
        return 1
    print('PASS（符号消歧 + endpoint 宿主/别名 + 严格 RPC + 混合路径 + 覆盖问题）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
