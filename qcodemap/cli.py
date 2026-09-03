# -*- coding: utf-8 -*-
"""命令行入口：build / callers / callees / usages。

用法示例：
  python -m qcodemap build [--root X] [--targets a,b] [--rebuild]
  python -m qcodemap callers <symbol> [--json]
  python -m qcodemap callers <file> <func> [--json]
  python -m qcodemap callees <file> <func> [--json]
  python -m qcodemap usages <symbol> [--limit N]
"""

import argparse
import json
import os
import sys

from qcodemap import build as build_mod
from qcodemap import config as config_mod


def _force_utf8(stream, errors='strict'):
    reconfigure = getattr(stream, 'reconfigure', None)
    if reconfigure:
        try:
            reconfigure(encoding='utf-8', errors=errors)
        except (AttributeError, TypeError, ValueError):
            pass


def _complete_query_options(parsers):
    """所有查询命令共用一致的仓库/库/输出/刷新参数。"""
    for parser in parsers:
        dests = {a.dest for a in parser._actions}
        if 'root' not in dests:
            parser.add_argument('--root', default=None)
        if 'db' not in dests:
            parser.add_argument('--db', default=None)
        if 'json' not in dests:
            parser.add_argument('--json', action='store_true')
        parser.add_argument('--refresh', choices=['auto', 'check', 'off'],
                            default='auto')


def _apply_db_root(cfg, db_override):
    """查询库 meta 里记的建库 root 回填 cfg（无库/无 meta 静默跳过）。"""
    import sqlite3
    path = db_override or cfg.db_path
    try:
        con = sqlite3.connect(path)
        try:
            row = con.execute(
                "SELECT value FROM meta WHERE key='root'").fetchone()
            targets_row = con.execute(
                "SELECT value FROM meta WHERE key='targets_json'").fetchone()
            coverage_row = con.execute(
                "SELECT value FROM meta WHERE key='coverage_status'").fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return
    if row and os.path.isdir(row[0]):
        cfg.root = row[0]
    if (coverage_row and coverage_row[0] == 'targeted' and targets_row):
        cfg.targets = json.loads(targets_row[0])
        cfg.targets_overridden = True


def main(argv=None):
    _force_utf8(sys.stdout)
    _force_utf8(sys.stderr, errors='backslashreplace')
    ap = argparse.ArgumentParser(prog='qcodemap',
                                 description='桩语义数据化 + 倒排索引的调用链查询')
    ap.add_argument('--custom', default=None, help='custom 目录路径（默认包平级 custom/）')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p_build = sub.add_parser('build', help='（增量）建索引')
    p_build.add_argument('--root', default=None, help='被分析项目根目录')
    p_build.add_argument('--targets', default=None,
                         help='只索引这些顶层目录（逗号分隔，如 src,lib），'
                              '跳过 tests/docs 等无关目录；不传则 ROOT 全量')
    p_build.add_argument('--db', default=None, help='索引库路径（缺省 cache/qcodemap.db）')
    p_build.add_argument('--rebuild', action='store_true', help='删库重建')
    p_build.add_argument('--vacuum', action='store_true', help='显式回收 SQLite 空闲页')
    p_build.add_argument('--json', action='store_true')

    p_callers = sub.add_parser('callers', help='谁调用这个函数')
    p_callers.add_argument(
        'target', help='符号名，或兼容旧用法时的函数所在文件（相对 root）')
    p_callers.add_argument(
        'func', nargs='?', help='兼容旧用法：与前一个 file 参数配对的函数名')
    p_callers.add_argument('--root', default=None)
    p_callers.add_argument('--db', default=None)
    p_callers.add_argument('--json', action='store_true')
    p_callers.add_argument('--receiver-class', default=None,
                           help='限定 receiver 类型证据')

    p_callees = sub.add_parser('callees', help='这个函数调了谁')
    p_callees.add_argument(
        'target', help='符号名，或兼容旧用法时的函数所在文件（相对 root）')
    p_callees.add_argument(
        'func', nargs='?', help='兼容旧用法：与前一个 file 参数配对的函数名')
    p_callees.add_argument('--root', default=None)
    p_callees.add_argument('--db', default=None)
    p_callees.add_argument('--json', action='store_true')

    p_usages = sub.add_parser('usages', help='标识符全仓出现点')
    p_usages.add_argument('symbol')
    p_usages.add_argument('--root', default=None)
    p_usages.add_argument('--db', default=None)
    p_usages.add_argument('--limit', type=int, default=200)
    p_usages.add_argument('--json', action='store_true')

    p_rpc = sub.add_parser('rpc-refs', help='RPC 方法名双端配对（调用点+handler）')
    p_rpc.add_argument('method')
    p_rpc.add_argument('--stub', default=None, help='限定目标实体类名（可选）')
    p_rpc.add_argument('--root', default=None)
    p_rpc.add_argument('--db', default=None)
    p_rpc.add_argument('--json', action='store_true')

    p_path = sub.add_parser('path', help='跨普通调用/RPC 的最短符号路径')
    p_path.add_argument('--from', dest='from_symbol', required=True,
                        help='起点符号（Func/Class.Func/path.py:Class.Func）')
    p_path.add_argument('--to', dest='to_symbol', required=True,
                        help='终点符号（Func/Class.Func/path.py:Class.Func）')
    p_path.add_argument('--max-depth', type=int, default=6,
                        help='最大边数（默认 6）')
    p_path.add_argument('--max-nodes', type=int, default=2000,
                        help='最大探索定义数（默认 2000）')

    p_pubsub = sub.add_parser('pubsub-refs',
                              help='事件名双端配对（发布点+订阅 handler）')
    p_pubsub.add_argument('event', help='事件常量名（裸名或完整键）')
    p_pubsub.add_argument('--side', default=None, choices=['listen', 'publish'],
                          help='限定方向（可选）')
    p_pubsub.add_argument('--root', default=None)
    p_pubsub.add_argument('--db', default=None)
    p_pubsub.add_argument('--json', action='store_true')

    p_ui = sub.add_parser('ui-refs', help='UI 资源绑定查询（资源名/节点名/动画名 ↔ Python）')
    p_ui.add_argument('name', nargs='?', default=None,
                      help='资源名（带不带路径前缀/后缀均可）、节点名或动画名')
    p_ui.add_argument('--kind', default=None, choices=['file', 'anim'],
                      help='视图类型：file=资源视图（缺省按名字形态推断）/ anim=动画名')
    p_ui.add_argument('--py', default=None, help='改为按 Python 文件列出全部 UI 事实')
    p_ui.add_argument('--audit', action='store_true',
                      help='全量审计：分级统计 + MISS/归属失败清单（改名安全报告）')
    p_ui.add_argument('--limit', type=int, default=200,
                      help='单页最多返回条数（1-1000，默认 200）')
    p_ui.add_argument('--offset', type=int, default=0,
                      help='分页起点（默认 0）')
    p_ui.add_argument('--root', default=None)
    p_ui.add_argument('--db', default=None)
    p_ui.add_argument('--json', action='store_true')

    p_deps = sub.add_parser('deps', help='文件/目录依赖了谁（文件级边）')
    p_deps.add_argument('target')
    p_deps.add_argument('--db', default=None)
    p_deps.add_argument('--json', action='store_true')

    p_imp = sub.add_parser('importers', help='谁 import 这个文件（反向边+枢纽判定）')
    p_imp.add_argument('target')
    p_imp.add_argument('--db', default=None)
    p_imp.add_argument('--json', action='store_true')

    p_hubs = sub.add_parser('hubs', help='import 入度排行（枢纽识别）')
    p_hubs.add_argument('--top', type=int, default=25)
    p_hubs.add_argument('--db', default=None)
    p_hubs.add_argument('--json', action='store_true')

    p_tree = sub.add_parser('tree', help='目录树聚合（文件数+体积）')
    p_tree.add_argument('--depth', type=int, default=2)
    p_tree.add_argument('--db', default=None)
    p_tree.add_argument('--json', action='store_true')

    p_blast = sub.add_parser('blast-radius', help='变更影响面（调用链闭包+import 级）')
    p_blast.add_argument('--files', default=None, help='逗号分隔文件清单（缺省走 svn st）')
    p_blast.add_argument('--rev', default=None, help='svn 版本区间 X:Y')
    p_blast.add_argument('--depth', type=int, default=3, help='闭包深度（0 不限）')
    p_blast.add_argument('--mode', choices=['full', 'summary', 'page'], default='full',
                         help='输出模式；CLI 默认 full 保持兼容')
    p_blast.add_argument('--section', choices=['callers', 'importers'],
                         default='callers', help='page 模式的分页部分')
    p_blast.add_argument('--layer', type=int, default=1,
                         help='page callers 的层级（1=直接调用方）')
    p_blast.add_argument('--offset', type=int, default=0)
    p_blast.add_argument('--limit', type=int, default=50,
                         help='page 每页数量（1..200）')
    p_blast.add_argument('--root', default=None)
    p_blast.add_argument('--db', default=None)
    p_blast.add_argument('--json', action='store_true')

    p_find = sub.add_parser('find', help='模糊路径搜索（子串匹配，短路径优先）')
    p_find.add_argument('pattern')
    p_find.add_argument('--limit', type=int, default=50)
    p_find.add_argument('--db', default=None)
    p_find.add_argument('--json', action='store_true')

    p_fctx = sub.add_parser('file-context', help='单文件完整消费面（defs/依赖双向/枢纽/事实）')
    p_fctx.add_argument('file')
    p_fctx.add_argument('--root', default=None)
    p_fctx.add_argument('--db', default=None)
    p_fctx.add_argument('--json', action='store_true')

    p_ctx = sub.add_parser('context', help='一次性项目档案（AI 会话冷启动注入用）')
    p_ctx.add_argument('--compact', action='store_true', help='各列表截断的紧凑版')
    p_ctx.add_argument('--root', default=None)
    p_ctx.add_argument('--db', default=None)
    p_ctx.add_argument('--json', action='store_true')

    p_defs = sub.add_parser('defs', help='精确查询符号定义')
    p_defs.add_argument('symbol')
    p_defs.add_argument('--limit', type=int, default=200)

    p_diagnose = sub.add_parser('diagnose', help='运行 custom 项目级诊断')

    _complete_query_options((
        p_callers, p_callees, p_usages, p_rpc, p_pubsub, p_ui, p_deps, p_imp,
        p_hubs, p_tree, p_blast, p_find, p_fctx, p_ctx, p_defs, p_diagnose,
        p_path,
    ))

    sub.add_parser('mcp', help='启动 MCP server（stdio JSON-RPC）')

    args = ap.parse_args(argv)
    targets = getattr(args, 'targets', None)
    cfg = config_mod.load_config(root=getattr(args, 'root', None),
                                 targets=targets.split(',') if targets else None,
                                 db_path=getattr(args, 'db', None),
                                 custom_dir=args.custom)
    if args.cmd == 'build':
        stats = build_mod.build(cfg, rebuild=args.rebuild, vacuum=args.vacuum)
        if args.json:
            print(json.dumps(stats, ensure_ascii=False, indent=2))
        return 0
    if args.cmd == 'mcp':
        from qcodemap import mcp_server
        mcp_server.serve()
        return 0
    # 查询命令：--root 未显式给出时优先用建库时落的 meta.root。
    # 跨项目场景（--db 指别的库）漏带 --root，默认 cwd 是错的根目录，
    # 语义验证读源码会 FileNotFoundError
    if not getattr(args, 'root', None):
        _apply_db_root(cfg, getattr(args, 'db', None))
    from qcodemap import freshness
    index_meta = None
    blast_snapshot = None
    if args.cmd == 'blast-radius' and not args.rev:
        from qcodemap import blast as blast_probe
        changed = (args.files.split(',') if args.files
                   else blast_probe.collect_svn_status(cfg))
        diffs, old_sources = blast_probe.collect_working_diffs(cfg, changed)
        blast_snapshot = (changed, diffs, old_sources)
    index_meta = freshness.ensure_fresh(cfg, mode=args.refresh)
    # 结构四命令：不依赖语义解析，直接查表
    if args.cmd in ('deps', 'importers', 'hubs', 'tree'):
        from qcodemap import structure as st
        from qcodemap.store import Store
        store = Store.open_reader(args.db or cfg.db_path)
        try:
            if args.cmd == 'deps':
                out = st.deps(store, args.target.replace('\\', '/'), args.json)
            elif args.cmd == 'importers':
                out = st.importers(store, args.target.replace('\\', '/'), args.json)
            elif args.cmd == 'hubs':
                out = st.hubs(store, args.top, args.json)
            else:
                out = st.tree(store, cfg, args.depth, args.json)
        finally:
            store.close()
        if args.json:
            freshness.attach_index(out, index_meta)
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            print(out)
        return 0
    if args.cmd in ('find', 'file-context', 'context'):
        from qcodemap import context as ctx
        from qcodemap.store import Store
        store = Store.open_reader(args.db or cfg.db_path)
        try:
            if args.cmd == 'find':
                out = ctx.find_file(store, args.pattern, limit=args.limit,
                                    json_out=args.json)
            elif args.cmd == 'file-context':
                out = ctx.get_file_context(store, cfg, args.file, json_out=args.json)
            else:
                out = ctx.context(store, cfg, compact=args.compact,
                                  json_out=args.json)
        finally:
            store.close()
        if args.json:
            freshness.attach_index(out, index_meta)
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            print(out)
        return 0
    if args.cmd == 'blast-radius':
        from qcodemap import blast
        from qcodemap.store import Store
        store = Store.open_reader(args.db or cfg.db_path)
        try:
            bfiles = blast_snapshot[0] if blast_snapshot else (
                args.files.split(',') if args.files else None)
            out = blast.blast(store, cfg,
                              files=bfiles,
                              rev=args.rev, depth=args.depth or 99, json_out=args.json,
                              mode=args.mode, section=args.section, layer=args.layer,
                              offset=args.offset, limit=args.limit,
                              diffs=blast_snapshot[1] if blast_snapshot else None,
                              old_sources=blast_snapshot[2] if blast_snapshot else None)
        finally:
            store.close()
        if args.json:
            freshness.attach_index(out, index_meta)
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            print(out)
        return 0
    if args.cmd == 'rpc-refs':
        from qcodemap import rpc_refs
        from qcodemap.store import Store
        store = Store.open_reader(args.db or cfg.db_path)
        try:
            out = rpc_refs.rpc_refs(store, cfg, args.method, stub=args.stub,
                                    json_out=args.json)
        finally:
            store.close()
        if args.json:
            freshness.attach_index(out, index_meta)
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            print(out)
        return 0
    if args.cmd == 'path':
        from qcodemap import path_query
        from qcodemap.store import Store
        store = Store.open_reader(args.db or cfg.db_path)
        try:
            out = path_query.path(
                store, cfg, args.from_symbol, args.to_symbol,
                max_depth=max(0, args.max_depth),
                max_nodes=max(1, args.max_nodes), json_out=args.json)
        finally:
            store.close()
        if args.json:
            freshness.attach_index(out, index_meta)
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            print(out)
        return 0
    if args.cmd == 'pubsub-refs':
        from qcodemap import pubsub_refs
        from qcodemap.store import Store
        store = Store.open_reader(args.db or cfg.db_path)
        try:
            out = pubsub_refs.pubsub_refs(store, cfg, args.event,
                                          side=args.side, json_out=args.json)
        finally:
            store.close()
        if args.json:
            freshness.attach_index(out, index_meta)
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            print(out)
        return 0
    if args.cmd == 'ui-refs':
        from qcodemap import ui_refs
        from qcodemap.store import Store
        store = Store.open_reader(args.db or cfg.db_path)
        try:
            if args.audit:
                out = ui_refs.ui_audit(store, cfg, json_out=args.json)
            else:
                out = ui_refs.ui_refs(store, cfg, name=args.name, kind=args.kind,
                                      py_file=args.py, json_out=args.json,
                                      limit=args.limit, offset=args.offset)
        finally:
            store.close()
        if args.json:
            freshness.attach_index(out, index_meta)
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            print(out)
        return 0
    from qcodemap import resolve
    if args.cmd == 'diagnose':
        from qcodemap import diagnostics
        from qcodemap.store import Store
        store = Store.open_reader(args.db or cfg.db_path)
        try:
            out = diagnostics.diagnose(store, cfg)
        finally:
            store.close()
        freshness.attach_index(out, index_meta)
        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            print('diagnose: %d issue(s)' % out['count'])
            for item in out['issues']:
                print('  [%s] %s:%s %s' % (
                    item.get('code'), item.get('file'), item.get('line'),
                    item.get('message')))
        return 0
    store = resolve.Store.open_reader(args.db or cfg.db_path)
    try:
        if args.cmd == 'callers':
            if args.func is None:
                out = resolve.callers_by_symbol(
                    store, cfg, args.target,
                    receiver_class=args.receiver_class)
            else:
                out = resolve.callers(store, cfg, args.target, args.func,
                                      receiver_class=args.receiver_class)
        elif args.cmd == 'callees':
            if args.func is None:
                out = resolve.callees_by_symbol(store, cfg, args.target)
            else:
                out = resolve.callees(store, cfg, args.target, args.func)
        elif args.cmd == 'defs':
            out = resolve.defs(store, args.symbol, limit=args.limit, cfg=cfg)
        else:
            out = resolve.usages(store, cfg, args.symbol, limit=args.limit)
    finally:
        store.close()
    freshness.attach_index(out, index_meta)
    _print_result(out, args.json)
    return 0


def _print_result(out, as_json):
    if as_json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return
    for item in out['items']:
        print('[%s] %s' % (item['level'], item['symbol']))
        print('       %s:%s  in %s' % (item['file'], item['line'], item.get('caller') or '?'))
        if item.get('note'):
            print('       (%s)' % item['note'])
    if out['note']:
        print('note: %s' % out['note'])
    cov = out.get('coverage')
    if cov and cov.get('status') == 'partial':
        print('coverage: partial（全库 %d 个文件 ast 解析失败，索引仅 names）'
              % cov['parse_failed'])
        for issue in cov.get('issues', ()):
            print('  [%s] %s（%s）' % (
                issue['code'], issue['file'], issue['impact']))
    print('耗时 %.3fs%s  VERIFIED=%d CANDIDATE=%d INFERRED=%d'
          % (out['elapsed'], '（缓存命中）' if out.get('cached') else '',
             out['n_verified'], out['n_candidate'], out.get('n_inferred', 0)))


if __name__ == '__main__':
    sys.exit(main())
