# -*- coding: utf-8 -*-
"""MCP server：stdio JSON-RPC 2.0（换行分隔），纯 stdlib。

协议要点：
- 请求/响应每行一个 JSON 对象；notification（无 id）不回包；
- initialize 回显客户端 protocolVersion（兼容 2024-11-05 / 2025-06-18 等）；
- 一切日志走 stderr——stdout 被协议流独占，print 即损坏协议。
"""

import json
import sys
import time

from qcodemap import __version__
from qcodemap import blast as blast_mod
from qcodemap import build as build_mod
from qcodemap import config as config_mod
from qcodemap import context as ctx_mod
from qcodemap import resolve as rmod
from qcodemap import rpc_refs as rpc_mod
from qcodemap import structure as st_mod
from qcodemap.store import Store


def _log(msg):
    sys.stderr.write('[qcodemap-mcp] %s\n' % msg)
    sys.stderr.flush()


# ---- 懒刷新（P4-5） ----

def _scope_rels(store, target):
    """deps/importers 的目标文件集（SQL 前缀匹配，不建 StructureIndex）。"""
    target = target.replace('\\', '/')
    row = store.con.execute('SELECT 1 FROM files WHERE path=?', (target,)).fetchone()
    if row:
        return [target]
    pref = target.rstrip('/') + '/%'
    return [p for (p,) in store.con.execute(
        'SELECT path FROM files WHERE path LIKE ?', (pref,))]


def _refresh_if_drifted(cfg, rels):
    """rels 中磁盘 mtime 与库内漂移时增量重建索引，返回 refresh 摘要。

    必须在打开查询 Store 之前调用（build 自开连接，并发写会锁库）。
    rels 为空返回 None；超过检测上限（大目录 scope）静默跳过。
    """
    rels = [r for r in (rels or []) if r]
    if not rels:
        return None
    store = Store(cfg.db_path)
    try:
        drifted = build_mod.drift_check(store, cfg, rels)
    finally:
        store.close()
    if not drifted:
        return None
    t0 = time.time()
    stats = build_mod.build(cfg, verbose=False)
    _log('lazy refresh: %d drifted files, build %.1fs'
         % (len(drifted), stats.get('elapsed', 0)))
    return {'files': len(drifted), 'elapsed': round(time.time() - t0, 1)}


# ---- 工具定义（name -> {description, inputSchema, handler}） ----

def _tool_build(args):
    cfg = config_mod.load_config()
    stats = build_mod.build(cfg, rebuild=bool(args.get('rebuild')), verbose=False)
    return stats


def _tool_callers(args):
    cfg = config_mod.load_config()
    refresh = _refresh_if_drifted(cfg, [args['file']])
    store = Store(cfg.db_path)
    try:
        out = rmod.callers(store, cfg, args['file'], args['func'])
    finally:
        store.close()
    if refresh:
        out['refresh'] = refresh
    return out


def _tool_callees(args):
    cfg = config_mod.load_config()
    refresh = _refresh_if_drifted(cfg, [args['file']])
    store = Store(cfg.db_path)
    try:
        out = rmod.callees(store, cfg, args['file'], args['func'])
    finally:
        store.close()
    if refresh:
        out['refresh'] = refresh
    return out


def _tool_usages(args):
    cfg = config_mod.load_config()
    store = Store(cfg.db_path)
    try:
        return rmod.usages(store, cfg, args['symbol'], limit=int(args.get('limit', 200)))
    finally:
        store.close()


def _tool_deps(args):
    cfg = config_mod.load_config()
    refresh = _refresh_if_drifted(cfg, _scope_rels_probe(cfg, args['target']))
    store = Store(cfg.db_path)
    try:
        out = st_mod.deps(store, args['target'], json_out=True)
    finally:
        store.close()
    if refresh:
        out['refresh'] = refresh
    return out


def _tool_importers(args):
    cfg = config_mod.load_config()
    refresh = _refresh_if_drifted(cfg, _scope_rels_probe(cfg, args['target']))
    store = Store(cfg.db_path)
    try:
        out = st_mod.importers(store, args['target'], json_out=True)
    finally:
        store.close()
    if refresh:
        out['refresh'] = refresh
    return out


def _scope_rels_probe(cfg, target):
    """为懒刷新取目标文件集（单开短命连接；空目标/超量由 drift_check 兜底）。"""
    store = Store(cfg.db_path)
    try:
        return _scope_rels(store, target)
    finally:
        store.close()


def _tool_hubs(args):
    cfg = config_mod.load_config()
    store = Store(cfg.db_path)
    try:
        return st_mod.hubs(store, top=int(args.get('top', 25)), json_out=True)
    finally:
        store.close()


def _tool_tree(args):
    cfg = config_mod.load_config()
    store = Store(cfg.db_path)
    try:
        return st_mod.tree(store, cfg, depth=int(args.get('depth', 2)), json_out=True)
    finally:
        store.close()


def _tool_blast(args):
    cfg = config_mod.load_config()
    files = args.get('files')
    if files:
        flist = [f.strip() for f in files.split(',') if f.strip()]
    elif not args.get('rev'):
        # svn st 模式：变更文件先懒刷新，再显式传入（与 blast 内部采集等价，
        # 省去二次 svn st）；rev 模式对历史版本，刷新无意义
        flist = blast_mod.collect_svn_status(cfg)
    else:
        flist = None
    refresh = _refresh_if_drifted(cfg, flist)
    store = Store(cfg.db_path)
    try:
        out = blast_mod.blast(store, cfg,
                              files=flist,
                              rev=args.get('rev'),
                              depth=int(args.get('depth', 3)), json_out=True)
    finally:
        store.close()
    if refresh:
        out['refresh'] = refresh
    return out


def _tool_rpc_refs(args):
    cfg = config_mod.load_config()
    store = Store(cfg.db_path)
    try:
        return rpc_mod.rpc_refs(store, cfg, args['method'],
                                stub=args.get('stub'), json_out=True)
    finally:
        store.close()


def _tool_find_file(args):
    cfg = config_mod.load_config()
    store = Store(cfg.db_path)
    try:
        return ctx_mod.find_file(store, args['pattern'],
                                 limit=int(args.get('limit', 50)), json_out=True)
    finally:
        store.close()


def _tool_get_file_context(args):
    cfg = config_mod.load_config()
    refresh = _refresh_if_drifted(cfg, [args['file']])
    store = Store(cfg.db_path)
    try:
        out = ctx_mod.get_file_context(store, cfg, args['file'], json_out=True)
    finally:
        store.close()
    if refresh:
        out['refresh'] = refresh
    return out


def _tool_context(args):
    cfg = config_mod.load_config()
    store = Store(cfg.db_path)
    try:
        return ctx_mod.context(store, cfg, compact=bool(args.get('compact')),
                               json_out=True)
    finally:
        store.close()


_TOOLS = [
    {
        'name': 'qcodemap_build',
        'description': '（增量）建索引。日常调用秒级；rebuild=true 删库全量重建（约 80s）。',
        'inputSchema': {'type': 'object',
                        'properties': {'rebuild': {'type': 'boolean'}},
                        'required': []},
        'handler': _tool_build,
    },
    {
        'name': 'qcodemap_callers',
        'description': '谁调用这个函数（VERIFIED=语义验证边 / CANDIDATE=同名未验证）。',
        'inputSchema': {'type': 'object',
                        'properties': {'file': {'type': 'string',
                                                'description': '函数所在文件（相对项目根）'},
                                       'func': {'type': 'string'}},
                        'required': ['file', 'func']},
        'handler': _tool_callers,
    },
    {
        'name': 'qcodemap_callees',
        'description': '这个函数调了谁（反向调用链）。',
        'inputSchema': {'type': 'object',
                        'properties': {'file': {'type': 'string'},
                                       'func': {'type': 'string'}},
                        'required': ['file', 'func']},
        'handler': _tool_callees,
    },
    {
        'name': 'qcodemap_usages',
        'description': '标识符全仓出现点与定义点摘要。',
        'inputSchema': {'type': 'object',
                        'properties': {'symbol': {'type': 'string'},
                                       'limit': {'type': 'integer', 'default': 200}},
                        'required': ['symbol']},
        'handler': _tool_usages,
    },
    {
        'name': 'qcodemap_deps',
        'description': '文件/目录依赖了谁（文件级边+外部模块）。',
        'inputSchema': {'type': 'object',
                        'properties': {'target': {'type': 'string'}},
                        'required': ['target']},
        'handler': _tool_deps,
    },
    {
        'name': 'qcodemap_importers',
        'description': '谁 import 这个文件（反向边+枢纽判定）。',
        'inputSchema': {'type': 'object',
                        'properties': {'target': {'type': 'string'}},
                        'required': ['target']},
        'handler': _tool_importers,
    },
    {
        'name': 'qcodemap_hubs',
        'description': 'import 入度排行（枢纽文件识别）。',
        'inputSchema': {'type': 'object',
                        'properties': {'top': {'type': 'integer', 'default': 25}},
                        'required': []},
        'handler': _tool_hubs,
    },
    {
        'name': 'qcodemap_tree',
        'description': '目录树聚合（文件数+体积）。',
        'inputSchema': {'type': 'object',
                        'properties': {'depth': {'type': 'integer', 'default': 2}},
                        'required': []},
        'handler': _tool_tree,
    },
    {
        'name': 'qcodemap_blast_radius',
        'description': '变更影响面：调用链闭包（直接+传递调用方）+ import 级 importers。'
                       '不传 files/rev 时按 svn st 采集当前工作副本变更。',
        'inputSchema': {'type': 'object',
                        'properties': {'files': {'type': 'string',
                                                 'description': '逗号分隔文件清单（可选）'},
                                       'rev': {'type': 'string',
                                               'description': 'svn 版本区间 X:Y（可选）'},
                                       'depth': {'type': 'integer', 'default': 3}},
                        'required': []},
        'handler': _tool_blast,
    },
    {
        'name': 'qcodemap_rpc_refs',
        'description': 'RPC 方法名双端配对：字符串分发调用点（RPC-INFERRED，含通道'
                       '与 stub）+ handler 定义（HANDLER）。适用于 CallServer/'
                       'CallClient/stub 等字符串分发形态的跨端跳转。',
        'inputSchema': {'type': 'object',
                        'properties': {'method': {'type': 'string',
                                                  'description': 'RPC 方法名字符串'},
                                       'stub': {'type': 'string',
                                                'description': '目标实体类名（可选，'
                                                               'stub 通道精确配对用）'}},
                        'required': ['method']},
        'handler': _tool_rpc_refs,
    },
    {
        'name': 'qcodemap_find_file',
        'description': '模糊路径搜索（子串匹配，ASCII 大小写不敏感，短路径优先）。'
                       '路径记不清时先用它定位。',
        'inputSchema': {'type': 'object',
                        'properties': {'pattern': {'type': 'string'},
                                       'limit': {'type': 'integer', 'default': 50}},
                        'required': ['pattern']},
        'handler': _tool_find_file,
    },
    {
        'name': 'qcodemap_get_file_context',
        'description': '单文件完整消费面打包：类/定义清单、imports+外部模块、'
                       'importers+枢纽判定、Property/组件框架事实，一次拿全省往返。',
        'inputSchema': {'type': 'object',
                        'properties': {'file': {'type': 'string',
                                                'description': '相对项目根的路径'}},
                        'required': ['file']},
        'handler': _tool_get_file_context,
    },
    {
        'name': 'qcodemap_context',
        'description': '一次性项目档案（统计+目录树+枢纽+外部依赖排行+覆盖率），'
                       'AI 会话冷启动注入用；compact=true 为列表截断的紧凑版。',
        'inputSchema': {'type': 'object',
                        'properties': {'compact': {'type': 'boolean'}},
                        'required': []},
        'handler': _tool_context,
    },
]


# ---- JSON-RPC 主循环 ----

def _handle(msg):
    """单条消息 -> 响应 dict（notification 返回 None）。"""
    method = msg.get('method')
    msg_id = msg.get('id')
    is_request = msg_id is not None
    if method == 'initialize':
        params = msg.get('params') or {}
        return _ok(msg_id, {
            'protocolVersion': params.get('protocolVersion', '2024-11-05'),
            'capabilities': {'tools': {}},
            'serverInfo': {'name': 'qcodemap', 'version': __version__},
        })
    if method in ('notifications/initialized', 'initialized'):
        return None
    if method == 'tools/list':
        return _ok(msg_id, {'tools': [
            {k: v for k, v in t.items() if k != 'handler'} for t in _TOOLS]})
    if method == 'tools/call':
        params = msg.get('params') or {}
        name = params.get('name')
        tool = next((t for t in _TOOLS if t['name'] == name), None)
        if tool is None:
            return _err(msg_id, -32602, 'unknown tool: %s' % name)
        try:
            result = tool['handler'](params.get('arguments') or {})
            return _ok(msg_id, {'content': [
                {'type': 'text', 'text': json.dumps(result, ensure_ascii=False)}]})
        except Exception as e:  # noqa: BLE001 -- 工具错误须回包而非崩 server
            _log('tool %s failed: %r' % (name, e))
            return _ok(msg_id, {'content': [{'type': 'text', 'text': 'ERROR: %s' % e}],
                                'isError': True})
    if method == 'ping':
        return _ok(msg_id, {})
    if is_request:
        return _err(msg_id, -32601, 'method not found: %s' % method)
    return None


def _ok(msg_id, result):
    return {'jsonrpc': '2.0', 'id': msg_id, 'result': result}


def _err(msg_id, code, message):
    return {'jsonrpc': '2.0', 'id': msg_id,
            'error': {'code': code, 'message': message}}


def serve(stdin=None, stdout=None):
    """读行->处理->写行；EOF 结束。参数可注入便于测试。"""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    _log('qcodemap mcp server up, %d tools' % len(_TOOLS))
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            _log('bad json line: %.80s' % line)
            continue
        resp = _handle(msg)
        if resp is not None:
            stdout.write(json.dumps(resp, ensure_ascii=False) + '\n')
            stdout.flush()


if __name__ == '__main__':
    serve()
