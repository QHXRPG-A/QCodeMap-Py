# -*- coding: utf-8 -*-
"""P8: ordered components, aliases, callbacks, receiver expansion and partitions."""

import ast
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

from qcodemap import blast, build, cli, mcp_server, path_query, resolve  # noqa: E402
from qcodemap.config import Config  # noqa: E402
from qcodemap.hooks import FactsHooks  # noqa: E402
from qcodemap.scanner import dotted  # noqa: E402
from qcodemap.store import Store  # noqa: E402


COMPONENTS = '''class Common(object):
    def OnReborn(self):
        return "common"

class PlayerSpecific(object):
    def OnReborn(self):
        return "player"

@Rewrite
class StateRound(object):
    def OnExit(self):
        return self.OnNewRound()

@Composes(Common, PlayerSpecific, StateRound)
class Player(object):
    def OnNewRound(self):
        return True

class TimerOwner(object):
    def Top(self):
        return self.Start()

    def Start(self):
        self.add_timer(1, self.Fix)

    def StartPartial(self):
        self.add_timer(1, partial(self.Fix))

    def Fix(self):
        return True
'''

RUNTIME = '''class GameA(object):
    def Capture(self):
        return "a"

class GameB(object):
    def Capture(self):
        return "b"

class FlagA(object):
    def Tick(self):
        logic = self.space.game_logic
        return logic.Capture()

class FlagB(object):
    def Tick(self):
        logic = self.space.game_logic
        return logic.Capture()
'''

SIDE = '''class SideType(object):
    def Ping(self):
        return True
'''

CLIENT_USE = '''from client.model import SideType

def RunClient():
    obj = SideType()
    return obj.Ping()
'''

SERVER_USE = '''from server.model import SideType

def RunServer():
    obj = SideType()
    return obj.Ping()
'''

DUPE = '''class Alpha(object):
    def OnReborn(self):
        return True

class Beta(object):
    def OnReborn(self):
        return False
'''

OTHER_STATE = '''@Rewrite
class StateRound(object):
    def OnExit(self):
        return self.OtherRound()

@Composes(StateRound)
class OtherGame(object):
    def OtherRound(self):
        return True
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

    def method_alias_facts(self, cd, ctx):
        if not any(isinstance(dec, ast.Name) and dec.id == 'Rewrite'
                   for dec in cd.decorator_list):
            return []
        return [('OnExit', 'OnRoundExit', 'framework',
                 'fixture runtime rewrite')]

    def module_bindings(self, tree, ctx):
        if ctx.rel != 'server/runtime.py':
            return []
        return [
            (1, 'runtime-owner', 'FlagA', 'game_logic_owner', 'GameA',
             'server', 'framework', 'fixture owner'),
            (1, 'runtime-owner', 'FlagB', 'game_logic_owner', 'GameB',
             'server', 'framework', 'fixture owner'),
        ]

    def receiver_type_facts(self, call, ctx):
        if (isinstance(call.func, ast.Attribute)
                and dotted(call.func.value) == 'logic'
                and ctx.cls in ('FlagA', 'FlagB')):
            return [('logic', '@runtime-owner:%s' % ctx.cls, 'framework',
                     'fixture dynamic owner')]
        return []

    def expand_receiver_type(self, typ, store, cfg, from_file):
        prefix = '@runtime-owner:'
        if not typ.startswith(prefix):
            return []
        owner = typ[len(prefix):]
        return [(target, confidence, reason) for target, confidence, reason in
                store.con.execute(
                    'SELECT target,confidence,reason FROM binding '
                    'WHERE domain=? AND owner=? AND relation=?',
                    ('runtime-owner', owner, 'game_logic_owner')).fetchall()]

    def call_callback_facts(self, call, ctx):
        name = dotted(call.func)
        target = None
        kind = None
        if name and name.rsplit('.', 1)[-1] == 'add_timer' \
                and len(call.args) >= 2:
            target = call.args[1]
            kind = 'TIMER'
        elif name == 'partial' and call.args:
            target = call.args[0]
            kind = 'PARTIAL'
        if (isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == 'self'):
            return [(kind, ctx.func, target.attr)]
        return []

    @staticmethod
    def file_partition(rel):
        if rel.startswith('client/'):
            return 'client'
        if rel.startswith('server/'):
            return 'server'
        return None


def _write(root, rel, text):
    path = os.path.join(root, rel.replace('/', os.sep))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as file_obj:
        file_obj.write(text)


def _cli_json(argv):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = cli.main(argv)
    if code not in (None, 0):
        raise AssertionError('CLI exit=%s: %s' % (code, argv))
    return json.loads(output.getvalue())


def main():
    tmp = tempfile.mkdtemp(prefix='qcodemap_p8_')
    failed = []
    try:
        root = os.path.join(tmp, 'src')
        _write(root, 'shared/components.py', COMPONENTS)
        _write(root, 'shared/dupe.py', DUPE)
        _write(root, 'server/runtime.py', RUNTIME)
        _write(root, 'server/other_state.py', OTHER_STATE)
        _write(root, 'client/model.py', SIDE)
        _write(root, 'client/use.py', CLIENT_USE)
        _write(root, 'server/model.py', SIDE)
        _write(root, 'server/use.py', SERVER_USE)
        cfg = Config()
        cfg.root = root
        cfg.targets = ['shared', 'client', 'server']
        cfg.exclude_dirs = set()
        cfg.exclude_files = []
        cfg.db_path = os.path.join(tmp, 'p8.db')
        cfg.hooks = FixtureHooks()
        build.build(cfg, rebuild=True, verbose=False)

        store = Store.open_reader(cfg.db_path)
        try:
            player = resolve.resolve_symbol(store, cfg, 'Player.OnReborn')
            if (player['status'] != 'resolved'
                    or player['selected']['class'] != 'PlayerSpecific'):
                failed.append('后注入组件未覆盖通用组件: %s' % player)

            alias = resolve.resolve_symbol(store, cfg, 'Player.OnRoundExit')
            if (alias['status'] != 'resolved'
                    or alias['selected']['class'] != 'StateRound'
                    or alias['selected']['source_name'] != 'OnExit'):
                failed.append('运行时方法别名未保留物理定义: %s' % alias)
            state_path = path_query.path(
                store, cfg, 'shared/components.py:StateRound.OnExit',
                'Player.OnNewRound')
            if not state_path['found']:
                failed.append('组件 self 未反向解析共同宿主: %s' % state_path)

            dynamic_path = path_query.path(
                store, cfg, 'FlagA.Tick', 'GameA.Capture')
            dynamic_edges = (dynamic_path.get('path') or {}).get('edges', [])
            if (not dynamic_path['found'] or not dynamic_edges
                    or dynamic_edges[0]['level'] != 'FRAMEWORK-INFERRED'):
                failed.append('动态 receiver 未形成 framework path: %s'
                              % dynamic_path)
            a_callers = resolve.callers_by_symbol(store, cfg, 'GameA.Capture')
            callers = {item['caller'] for item in a_callers['items']}
            if 'FlagA.Tick' not in callers or 'FlagB.Tick' in callers:
                failed.append('精确 callers 同名降噪失败: %s' % a_callers)

            fix_callers = resolve.callers_by_symbol(store, cfg, 'TimerOwner.Fix')
            levels = {item['level'] for item in fix_callers['items']}
            if not {'TIMER-INFERRED', 'PARTIAL-INFERRED'} <= levels:
                failed.append('timer/partial callers 缺失: %s' % fix_callers)
            for source, level in (('TimerOwner.Start', 'TIMER-INFERRED'),
                                  ('TimerOwner.StartPartial', 'PARTIAL-INFERRED')):
                result = resolve.callees_by_symbol(store, cfg, source)
                if not any(item['level'] == level and item['symbol'] == 'TimerOwner.Fix'
                           for item in result['items']):
                    failed.append('%s callees 缺少 %s: %s'
                                  % (source, level, result))
            timer_path = path_query.path(
                store, cfg, 'TimerOwner.Start', 'TimerOwner.Fix')
            if not timer_path['found']:
                failed.append('timer callback 未进入 path: %s' % timer_path)
            direct, transitive, _truncated = blast._impact_closure(
                store, cfg,
                [{'class': 'TimerOwner', 'func': 'Fix',
                  'file': 'shared/components.py'}], 2)
            if not any(item['caller'] == 'TIMER TimerOwner.Start'
                       for item in direct):
                failed.append('timer callback 未进入 blast-radius: %s' % direct)
            if not any(item['caller'] == 'TimerOwner.Top'
                       for item in transitive):
                failed.append('timer callback 未继续 blast 闭包: %s' % transitive)

            client_callers = resolve.callers_by_symbol(
                store, cfg, 'client/model.py:SideType.Ping')
            side_callers = {item['caller'] for item in client_callers['items']}
            if 'RunClient' not in side_callers or 'RunServer' in side_callers:
                failed.append('客户端/服务端分区串线: %s' % client_callers)

            legacy = resolve.callees(store, cfg, 'shared/dupe.py', 'OnReborn')
            if (legacy.get('resolution', {}).get('status') != 'ambiguous'
                    or len(legacy['items']) != 2):
                failed.append('旧 callees 未返回同文件歧义候选: %s' % legacy)
            exact = resolve.callees_by_symbol(
                store, cfg, 'shared/dupe.py:Alpha.OnReborn')
            if exact['resolution']['selected']['class'] != 'Alpha':
                failed.append('callees 文件+类限定失效: %s' % exact)
            exact_defs = resolve.defs(
                store, 'shared/dupe.py:Beta.OnReborn', cfg=cfg)
            if (len(exact_defs['items']) != 1
                    or exact_defs['items'][0]['symbol'] != 'Beta.OnReborn'):
                failed.append('defs 未使用统一符号解析器: %s' % exact_defs)
            file_func = resolve.defs(store, 'shared/dupe.py:OnReborn', cfg=cfg)
            if len(file_func['items']) != 2:
                failed.append('path.py:Func 语法未返回文件内定义: %s' % file_func)
            if resolve.usages(store, cfg, 'Alpha.OnReborn')['items']:
                failed.append('usages 不应接受限定符号')
        finally:
            store.close()

        common = ['--root', root, '--db', cfg.db_path, '--refresh', 'off', '--json']
        old_cli_loader = cli.config_mod.load_config
        try:
            cli.config_mod.load_config = lambda **_kwargs: cfg
            cli_callees = _cli_json([
                'callees', 'shared/dupe.py:Alpha.OnReborn'] + common)
            cli_defs = _cli_json([
                'defs', 'shared/dupe.py:Beta.OnReborn'] + common)
            cli_legacy = _cli_json([
                'callees', 'shared/dupe.py', 'OnReborn'] + common)
        finally:
            cli.config_mod.load_config = old_cli_loader
        if cli_callees['resolution']['selected']['class'] != 'Alpha':
            failed.append('CLI callees 单符号入口失败: %s' % cli_callees)
        if cli_defs['items'][0]['symbol'] != 'Beta.OnReborn':
            failed.append('CLI defs 限定符号失败: %s' % cli_defs)
        if cli_legacy.get('resolution', {}).get('status') != 'ambiguous':
            failed.append('CLI callees 旧接口歧义失败: %s' % cli_legacy)

        callees_tool = next(tool for tool in mcp_server._TOOLS
                            if tool['name'] == 'qcodemap_callees')
        if len(callees_tool['inputSchema'].get('anyOf', ())) != 2:
            failed.append('MCP callees schema 未兼容 symbol/file+func')
        old_loader = mcp_server.config_mod.load_config
        try:
            mcp_server.config_mod.load_config = lambda: cfg
            mcp_symbol = callees_tool['handler'](
                {'symbol': 'shared/dupe.py:Alpha.OnReborn'})
            mcp_legacy = callees_tool['handler'](
                {'file': 'shared/dupe.py', 'func': 'OnReborn'})
        finally:
            mcp_server.config_mod.load_config = old_loader
        if mcp_symbol['resolution']['selected']['class'] != 'Alpha':
            failed.append('MCP callees symbol 入口失败: %s' % mcp_symbol)
        if mcp_legacy.get('resolution', {}).get('status') != 'ambiguous':
            failed.append('MCP callees 旧接口歧义失败: %s' % mcp_legacy)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failed:
        print('FAIL:')
        for item in failed:
            print('  -', item)
        return 1
    print('PASS（动态链路 1-7 + CLI/MCP 兼容）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
