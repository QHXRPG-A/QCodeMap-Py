# -*- coding: utf-8 -*-
"""命令行入口：build / callers / callees / usages。

用法示例：
  python -m qcodemap build [--root X] [--targets a,b] [--rebuild]
  python -m qcodemap callers <file> <func> [--json]
  python -m qcodemap callees <file> <func> [--json]
  python -m qcodemap usages <symbol> [--limit N]
"""

import argparse
import json
import sys

from qcodemap import build as build_mod
from qcodemap import config as config_mod


def main(argv=None):
    ap = argparse.ArgumentParser(prog='qcodemap',
                                 description='桩语义数据化 + 倒排索引的调用链查询')
    ap.add_argument('--custom', default=None, help='custom 目录路径（默认包平级 custom/）')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p_build = sub.add_parser('build', help='（增量）建索引')
    p_build.add_argument('--root', default=None)
    p_build.add_argument('--targets', default=None, help='逗号分隔目录，覆盖 custom 配置')
    p_build.add_argument('--db', default=None)
    p_build.add_argument('--rebuild', action='store_true', help='删库重建')

    p_callers = sub.add_parser('callers', help='谁调用这个函数')
    p_callers.add_argument('file', help='函数所在文件（相对 root 的路径）')
    p_callers.add_argument('func')
    p_callers.add_argument('--root', default=None)
    p_callers.add_argument('--db', default=None)
    p_callers.add_argument('--json', action='store_true')

    p_callees = sub.add_parser('callees', help='这个函数调了谁')
    for arg in ('file', 'func'):
        p_callees.add_argument(arg)
    p_callees.add_argument('--root', default=None)
    p_callees.add_argument('--db', default=None)
    p_callees.add_argument('--json', action='store_true')

    p_usages = sub.add_parser('usages', help='标识符全仓出现点')
    p_usages.add_argument('symbol')
    p_usages.add_argument('--root', default=None)
    p_usages.add_argument('--db', default=None)
    p_usages.add_argument('--limit', type=int, default=200)
    p_usages.add_argument('--json', action='store_true')

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

    sub.add_parser('mcp', help='启动 MCP server（stdio JSON-RPC）')

    args = ap.parse_args(argv)
    targets = getattr(args, 'targets', None)
    cfg = config_mod.load_config(root=getattr(args, 'root', None),
                                 targets=targets.split(',') if targets else None,
                                 db_path=getattr(args, 'db', None),
                                 custom_dir=args.custom)
    if args.cmd == 'build':
        build_mod.build(cfg, rebuild=args.rebuild)
        return 0
    # 结构四命令：不依赖语义解析，直接查表
    if args.cmd in ('deps', 'importers', 'hubs', 'tree'):
        from qcodemap import structure as st
        from qcodemap.store import Store
        store = Store(args.db or cfg.db_path)
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
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            print(out)
        return 0
    if args.cmd == 'mcp':
        from qcodemap import mcp_server
        mcp_server.serve()
        return 0
    if args.cmd in ('find', 'file-context', 'context'):
        from qcodemap import context as ctx
        from qcodemap.store import Store
        store = Store(args.db or cfg.db_path)
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
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            print(out)
        return 0
    if args.cmd == 'blast-radius':
        from qcodemap import blast
        from qcodemap.store import Store
        store = Store(args.db or cfg.db_path)
        try:
            out = blast.blast(store, cfg,
                              files=args.files.split(',') if args.files else None,
                              rev=args.rev, depth=args.depth or 99, json_out=args.json)
        finally:
            store.close()
        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            print(out)
        return 0
    from qcodemap import resolve
    store = resolve.Store(args.db or cfg.db_path)
    try:
        if args.cmd == 'callers':
            out = resolve.callers(store, cfg, args.file, args.func)
        elif args.cmd == 'callees':
            out = resolve.callees(store, cfg, args.file, args.func)
        else:
            out = resolve.usages(store, cfg, args.symbol, limit=args.limit)
    finally:
        store.close()
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
    print('耗时 %.3fs%s  VERIFIED=%d CANDIDATE=%d'
          % (out['elapsed'], '（缓存命中）' if out.get('cached') else '',
             out['n_verified'], out['n_candidate']))


if __name__ == '__main__':
    sys.exit(main())
