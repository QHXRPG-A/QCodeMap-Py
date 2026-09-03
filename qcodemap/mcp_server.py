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
from qcodemap import diagnostics as diagnostics_mod
from qcodemap import freshness as freshness_mod
from qcodemap import path_query as path_mod
from qcodemap import pubsub_refs as pubsub_mod
from qcodemap import resolve as rmod
from qcodemap import rpc_refs as rpc_mod
from qcodemap import ui_refs as ui_mod
from qcodemap import structure as st_mod
from qcodemap.store import Store


def _log(msg):
    sys.stderr.write('[qcodemap-mcp] %s\n' % msg)
    sys.stderr.flush()


def _force_utf8(stream, errors='strict'):
    """stdio 文本流自举为 UTF-8；注入的 StringIO 等无 reconfigure 时跳过。"""
    reconfigure = getattr(stream, 'reconfigure', None)
    if reconfigure is None:
        return
    try:
        reconfigure(encoding='utf-8', errors=errors)
    except (AttributeError, TypeError, ValueError):
        pass


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


def _ui_refs_tool_desc(tool):
    """tools/list 时注入 profile 的项目词汇描述。

    静态表里是通用占位文案（引擎层不含项目词汇）；有 custom profile 时
    换成 profile.ui_tool_description，参数描述保持通用。
    """
    try:
        cfg = config_mod.load_config()
        desc = getattr(cfg.ui_profile, 'ui_tool_description', '')
    except Exception:  # noqa: BLE001 -- 描述获取失败回退通用文案
        desc = ''
    if desc:
        out = dict(tool)
        out['description'] = desc
        return out
    return tool


def _refresh_if_drifted(cfg, rels):
    """rels 中磁盘 mtime 与库内漂移时增量重建索引，返回 refresh 摘要。

    必须在打开查询 Store 之前调用（build 自开连接，并发写会锁库）。
    rels 为空返回 None；超过检测上限（大目录 scope）静默跳过。
    """
    rels = [r for r in (rels or []) if r]
    if not rels:
        return None
    store = Store.open_reader(cfg.db_path)
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
    index = freshness_mod.ensure_fresh(cfg, mode='auto', throttle_seconds=1.0)
    store = Store.open_reader(cfg.db_path)
    try:
        if args.get('symbol'):
            out = rmod.callers_by_symbol(
                store, cfg, args['symbol'],
                receiver_class=args.get('receiver_class'))
        elif args.get('file') and args.get('func'):
            out = rmod.callers(store, cfg, args['file'], args['func'],
                               receiver_class=args.get('receiver_class'))
        else:
            raise ValueError('callers 需要 symbol，或 file + func')
    finally:
        store.close()
    return freshness_mod.attach_index(out, index)


def _tool_callees(args):
    cfg = config_mod.load_config()
    index = freshness_mod.ensure_fresh(cfg, mode='auto', throttle_seconds=1.0)
    store = Store.open_reader(cfg.db_path)
    try:
        if args.get('symbol'):
            out = rmod.callees_by_symbol(store, cfg, args['symbol'])
        elif args.get('file') and args.get('func'):
            out = rmod.callees(store, cfg, args['file'], args['func'])
        else:
            raise ValueError('callees 需要 symbol，或 file + func')
    finally:
        store.close()
    return freshness_mod.attach_index(out, index)


def _tool_usages(args):
    cfg = config_mod.load_config()
    index = freshness_mod.ensure_fresh(cfg, mode='auto', throttle_seconds=1.0)
    store = Store.open_reader(cfg.db_path)
    try:
        out = rmod.usages(store, cfg, args['symbol'], limit=int(args.get('limit', 200)))
    finally:
        store.close()
    return freshness_mod.attach_index(out, index)


def _tool_deps(args):
    cfg = config_mod.load_config()
    index = freshness_mod.ensure_fresh(cfg, mode='auto', throttle_seconds=1.0)
    store = Store.open_reader(cfg.db_path)
    try:
        out = st_mod.deps(store, args['target'], json_out=True)
    finally:
        store.close()
    return freshness_mod.attach_index(out, index)


def _tool_importers(args):
    cfg = config_mod.load_config()
    index = freshness_mod.ensure_fresh(cfg, mode='auto', throttle_seconds=1.0)
    store = Store.open_reader(cfg.db_path)
    try:
        out = st_mod.importers(store, args['target'], json_out=True)
    finally:
        store.close()
    return freshness_mod.attach_index(out, index)


def _scope_rels_probe(cfg, target):
    """为懒刷新取目标文件集（单开短命连接；空目标/超量由 drift_check 兜底）。"""
    store = Store.open_reader(cfg.db_path)
    try:
        return _scope_rels(store, target)
    finally:
        store.close()


def _tool_hubs(args):
    cfg = config_mod.load_config()
    index = freshness_mod.ensure_fresh(cfg, mode='auto', throttle_seconds=1.0)
    store = Store.open_reader(cfg.db_path)
    try:
        out = st_mod.hubs(store, top=int(args.get('top', 25)), json_out=True)
    finally:
        store.close()
    return freshness_mod.attach_index(out, index)


def _tool_tree(args):
    cfg = config_mod.load_config()
    index = freshness_mod.ensure_fresh(cfg, mode='auto', throttle_seconds=1.0)
    store = Store.open_reader(cfg.db_path)
    try:
        out = st_mod.tree(store, cfg, depth=int(args.get('depth', 2)), json_out=True)
    finally:
        store.close()
    return freshness_mod.attach_index(out, index)


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
    diffs = old_sources = None
    if flist is not None and not args.get('rev'):
        diffs, old_sources = blast_mod.collect_working_diffs(cfg, flist)
    index = freshness_mod.ensure_fresh(cfg, mode='auto', throttle_seconds=1.0)
    store = Store.open_reader(cfg.db_path)
    try:
        out = blast_mod.blast(store, cfg,
                              files=flist,
                              rev=args.get('rev'),
                              depth=int(args.get('depth', 3)), json_out=True,
                              mode=args.get('mode', 'summary'),
                              section=args.get('section', 'callers'),
                              layer=int(args.get('layer', 1)),
                              offset=int(args.get('offset', 0)),
                              limit=int(args.get('limit', 50)),
                              diffs=diffs, old_sources=old_sources)
    finally:
        store.close()
    return freshness_mod.attach_index(out, index)


def _tool_rpc_refs(args):
    cfg = config_mod.load_config()
    index = freshness_mod.ensure_fresh(cfg, mode='auto', throttle_seconds=1.0)
    store = Store.open_reader(cfg.db_path)
    try:
        out = rpc_mod.rpc_refs(store, cfg, args['method'],
                               stub=args.get('stub'), json_out=True)
    finally:
        store.close()
    return freshness_mod.attach_index(out, index)


def _tool_path(args):
    cfg = config_mod.load_config()
    index = freshness_mod.ensure_fresh(cfg, mode='auto', throttle_seconds=1.0)
    store = Store.open_reader(cfg.db_path)
    try:
        out = path_mod.path(
            store, cfg, args['from'], args['to'],
            max_depth=max(0, int(args.get('max_depth', 6))),
            max_nodes=max(1, int(args.get('max_nodes', 2000))),
            json_out=True)
    finally:
        store.close()
    return freshness_mod.attach_index(out, index)


def _tool_pubsub_refs(args):
    cfg = config_mod.load_config()
    index = freshness_mod.ensure_fresh(cfg, mode='auto', throttle_seconds=1.0)
    store = Store.open_reader(cfg.db_path)
    try:
        out = pubsub_mod.pubsub_refs(store, cfg, args['event'],
                                     side=args.get('side'), json_out=True)
    finally:
        store.close()
    return freshness_mod.attach_index(out, index)


def _tool_ui_refs(args):
    cfg = config_mod.load_config()
    index = freshness_mod.ensure_fresh(cfg, mode='auto', throttle_seconds=1.0)
    store = Store.open_reader(cfg.db_path)
    try:
        if args.get('audit'):
            out = ui_mod.ui_audit(store, cfg, json_out=True)
        else:
            out = ui_mod.ui_refs(store, cfg, name=args.get('name'),
                                 kind=args.get('kind'), py_file=args.get('py'),
                                 json_out=True,
                                 limit=int(args.get('limit', 200)),
                                 offset=int(args.get('offset', 0)))
    finally:
        store.close()
    return freshness_mod.attach_index(out, index)


def _tool_find_file(args):
    cfg = config_mod.load_config()
    index = freshness_mod.ensure_fresh(cfg, mode='auto', throttle_seconds=1.0)
    store = Store.open_reader(cfg.db_path)
    try:
        out = ctx_mod.find_file(store, args['pattern'],
                                limit=int(args.get('limit', 50)), json_out=True)
    finally:
        store.close()
    return freshness_mod.attach_index(out, index)


def _tool_get_file_context(args):
    cfg = config_mod.load_config()
    index = freshness_mod.ensure_fresh(cfg, mode='auto', throttle_seconds=1.0)
    store = Store.open_reader(cfg.db_path)
    try:
        out = ctx_mod.get_file_context(store, cfg, args['file'], json_out=True)
    finally:
        store.close()
    return freshness_mod.attach_index(out, index)


def _tool_context(args):
    cfg = config_mod.load_config()
    index = freshness_mod.ensure_fresh(cfg, mode='auto', throttle_seconds=1.0)
    store = Store.open_reader(cfg.db_path)
    try:
        out = ctx_mod.context(store, cfg, compact=bool(args.get('compact')),
                              json_out=True)
    finally:
        store.close()
    return freshness_mod.attach_index(out, index)


def _tool_defs(args):
    cfg = config_mod.load_config()
    index = freshness_mod.ensure_fresh(cfg, mode='auto', throttle_seconds=1.0)
    store = Store.open_reader(cfg.db_path)
    try:
        out = rmod.defs(store, args['symbol'], limit=int(args.get('limit', 200)),
                        cfg=cfg)
    finally:
        store.close()
    return freshness_mod.attach_index(out, index)


def _tool_diagnose(args):
    cfg = config_mod.load_config()
    index = freshness_mod.ensure_fresh(cfg, mode='auto', throttle_seconds=1.0)
    store = Store.open_reader(cfg.db_path)
    try:
        out = diagnostics_mod.diagnose(store, cfg)
    finally:
        store.close()
    return freshness_mod.attach_index(out, index)


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
        'description': '谁调用这个函数；可直接传 symbol，唯一时自动定位，歧义时返回候选。',
        'inputSchema': {'type': 'object',
                                       'properties': {'symbol': {'type': 'string',
                                                  'description': 'Func/Class.Func/'
                                                                 'path.py:Func/'
                                                                 'path.py:Class.Func'},
                                       'file': {'type': 'string',
                                                'description': '函数所在文件（相对项目根）'},
                                       'func': {'type': 'string'},
                                       'receiver_class': {
                                           'type': 'string',
                                           'description': '限定 receiver 类型证据'}},
                        'required': [],
                        'anyOf': [{'required': ['symbol']},
                                  {'required': ['file', 'func']}]},
        'handler': _tool_callers,
    },
    {
        'name': 'qcodemap_callees',
        'description': '这个函数调了谁；可传统一 symbol 或兼容的 file + func。',
        'inputSchema': {'type': 'object',
                        'properties': {'symbol': {'type': 'string',
                                                  'description': 'Func/Class.Func/'
                                                                 'path.py:Class.Func'},
                                       'file': {'type': 'string'},
                                       'func': {'type': 'string'}},
                        'required': [],
                        'anyOf': [{'required': ['symbol']},
                                  {'required': ['file', 'func']}]},
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
                                       'depth': {'type': 'integer', 'default': 3},
                                       'mode': {'type': 'string',
                                                'enum': ['summary', 'page', 'full'],
                                                'default': 'summary'},
                                       'section': {'type': 'string',
                                                   'enum': ['callers', 'importers'],
                                                   'default': 'callers'},
                                       'layer': {'type': 'integer', 'minimum': 1,
                                                 'default': 1},
                                       'offset': {'type': 'integer', 'minimum': 0,
                                                  'default': 0},
                                       'limit': {'type': 'integer', 'minimum': 1,
                                                 'maximum': 200, 'default': 50}},
                        'required': []},
        'handler': _tool_blast,
    },
    {
        'name': 'qcodemap_rpc_refs',
        'description': 'RPC 方法名双端配对：字符串分发调用点（RPC-INFERRED，含通道'
                       '与 stub）+ handler 定义（HANDLER）。适用于按方法名字符串'
                       '分发的 RPC 调用形态的跨端跳转。',
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
        'name': 'qcodemap_path',
        'description': '查询两个符号间跨普通调用和标准化 RPC 的最短路径。',
        'inputSchema': {'type': 'object',
                        'properties': {'from': {'type': 'string'},
                                       'to': {'type': 'string'},
                                       'max_depth': {'type': 'integer',
                                                     'default': 6},
                                       'max_nodes': {'type': 'integer',
                                                     'default': 2000}},
                        'required': ['from', 'to']},
        'handler': _tool_path,
    },
    {
        'name': 'qcodemap_pubsub_refs',
        'description': '事件名双端配对：发布调用点（EVENT-INFERRED）+ 订阅'
                       'handler（LISTENER）。适用于订阅装饰器 ↔ 广播/发布调用'
                       '的事件分发形态跨模块跳转。',
        'inputSchema': {'type': 'object',
                        'properties': {'event': {'type': 'string',
                                                 'description': '事件常量名'
                                                                '（裸名或完整键）'},
                                       'side': {'type': 'string',
                                                'description': 'listen/publish'
                                                               '（可选）'}},
                        'required': ['event']},
        'handler': _tool_pubsub_refs,
    },
    {
        'name': 'qcodemap_ui_refs',
        'description': 'UI 资源绑定双向查询（项目词汇描述由 custom 的 '
                       'ui_profile 提供；未提供 profile 时仅输出绑定事实）',
        'inputSchema': {'type': 'object',
                        'properties': {'name': {'type': 'string',
                                                'description': '资源名/'
                                                               '节点名/'
                                                               '动画名'},
                                       'kind': {'type': 'string',
                                                'enum': ['file', 'anim'],
                                                'description': '视图：file=资源'
                                                               '（缺省按名字形态'
                                                               '推断）/anim=动画名'},
                                       'py': {'type': 'string',
                                              'description': 'Python 相对路径：'
                                                             '列该文件全部 UI '
                                                             '事实（与 name 二选一）'},
                                       'audit': {'type': 'boolean',
                                                 'description': '全量审计模式：'
                                                                '分级统计 + '
                                                                'MISS/归属失败/'
                                                                'TYPE-MISMATCH/'
                                                                'DYNAMIC 清单'
                                                                '（资源改名'
                                                                '安全报告）'},
                                       'limit': {'type': 'integer',
                                                 'default': 200,
                                                 'description': '单页条数，最大 1000'},
                                       'offset': {'type': 'integer',
                                                  'default': 0,
                                                  'description': '分页起点'}},
                        'required': []},
        'handler': _tool_ui_refs,
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
                       'importers+枢纽判定、声明式属性/组件等框架事实'
                       '（custom 钩子定义），一次拿全省往返。',
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
    {
        'name': 'qcodemap_defs',
        'description': '精确查询 Python 符号定义，不混入普通出现点。',
        'inputSchema': {'type': 'object',
                        'properties': {'symbol': {'type': 'string'},
                                       'limit': {'type': 'integer', 'default': 200}},
                        'required': ['symbol']},
        'handler': _tool_defs,
    },
    {
        'name': 'qcodemap_diagnose',
        'description': '运行 custom hook 提供的项目级诊断。',
        'inputSchema': {'type': 'object', 'properties': {}, 'required': []},
        'handler': _tool_diagnose,
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
            {k: v for k, v in _ui_refs_tool_desc(t).items() if k != 'handler'}
            if t['name'] == 'qcodemap_ui_refs'
            else {k: v for k, v in t.items() if k != 'handler'}
            for t in _TOOLS]})
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
    if stdin is None:
        _force_utf8(sys.stdin)
        stdin = sys.stdin
    if stdout is None:
        _force_utf8(sys.stdout)
        stdout = sys.stdout
    _force_utf8(sys.stderr, errors='backslashreplace')
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
