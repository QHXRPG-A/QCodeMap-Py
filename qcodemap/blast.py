# -*- coding: utf-8 -*-
"""blast-radius 调用链版：变更集 -> 函数级影响闭包（codemap 只能做 import 级）。

三个维度：
1. 变更函数的直接调用方（VERIFIED 边）与传递闭包（默认 3 层，防枢纽爆炸）；
2. 变更文件的 import 级 importers（codemap 平价维度，两相对照）；
3. 变更函数清单本身（svn diff hunk 行区间 × defs 行号的启发式 enclosing）。

变更集采集与算法解耦：svn st / svn diff --summarize / 显式 --files 三种来源，
统一归一为 [rel_path]。
"""

import re
import subprocess
import time

from qcodemap import resolve as rmod
from qcodemap import structure as st

MAX_EDGES = 1000  # 闭包安全上限：超过即截断并在输出标注
MAX_NAME_CANDS = 1500  # 超高频名冷验证分钟级且影响面无增量信息，只吃缓存
MAX_OUTPUT_LIMIT = 200
BLAST_SCHEMA_VERSION = 'qcodemap.blast/v2'


def collect_svn_status(cfg):
    """工作副本变更（svn st）：M/A 的 .py 文件。只读操作。"""
    out = _svn(['st', cfg.root])
    files = []
    for line in out.splitlines():
        if len(line) < 8:
            continue
        status, path = line[0], line[7:].strip()
        if status in 'MA' and path.endswith('.py'):
            files.append(_svn_rel(cfg, path))
    return files


def collect_svn_diff(cfg, rev):
    """版本区间变更（svn diff --summarize）：返回 (rel_path -> diff 全文)。"""
    out = _svn(['diff', '--summarize', '-r', rev, cfg.root])
    files = []
    for line in out.splitlines():
        if len(line) > 8 and line[0] in 'MA' and line[8:].strip().endswith('.py'):
            files.append(_svn_rel(cfg, line[8:].strip()))
    return files


def _svn(args):
    r = subprocess.run(['svn'] + args, capture_output=True, text=True,
                       encoding='utf-8', errors='replace', timeout=60)
    return r.stdout or ''


def _svn_rel(cfg, path):
    """svn 输出的绝对/相对路径 -> 库内 rel（posix）。"""
    p = path.replace('\\', '/')
    root = cfg.root.replace('\\', '/')
    if p.startswith(root):
        p = p[len(root):]
    return p.lstrip('/')


def changed_functions(store, cfg, file, diff_text=None):
    """变更文件 -> 变更函数清单。

    有 diff：hunk new 侧行区间 × defs 行号（近似 enclosing，一个 def 的区间
    到同文件下一个 def 前，标注启发式）；无 diff（--files 模式）：全部 def。
    """
    defs = store.con.execute(
        'SELECT line, class, name FROM defs WHERE file=? ORDER BY line',
        (file,)).fetchall()
    if not defs:
        return []
    if diff_text is None:
        return [{'file': file, 'class': c, 'func': n, 'line': ln, 'heuristic': False}
                for (ln, c, n) in defs]
    ranges = _hunk_new_ranges(diff_text)
    result = []
    for i, (ln, c, n) in enumerate(defs):
        end = defs[i + 1][0] - 1 if i + 1 < len(defs) else ln + 2000
        if any(a <= end and b >= ln for (a, b) in ranges):
            result.append({'file': file, 'class': c, 'func': n,
                           'line': ln, 'heuristic': True})
    return result


def changed_callbacks(store, file, diff_text=None):
    """变更文件 -> 钩子声明的通用约定回调事实。"""
    rows = store.con.execute(
        'SELECT file,line,class,kind,source,target FROM callback_raw '
        'WHERE file=? ORDER BY line', (file,)).fetchall()
    if diff_text is None:
        return rows
    ranges = _hunk_new_ranges(diff_text)
    return [row for row in rows if any(a <= row[1] <= b for a, b in ranges)]


_HUNK_RE = re.compile(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@')


def _hunk_new_ranges(diff_text):
    """unified diff -> new 侧行区间 [(start, end)]（1 基闭区间，由 hunk 头给出）。"""
    ranges = []
    for line in diff_text.splitlines():
        m = _HUNK_RE.match(line)
        if m:
            start = int(m.group(1))
            count = int(m.group(2) or 1)
            if count > 0:
                ranges.append((start, start + count - 1))
    return ranges


def blast(store, cfg, files=None, rev=None, depth=3, use_svn_status=True,
          json_out=False, mode='full', section='callers', layer=1,
          offset=0, limit=50):
    """主入口：变更集 -> 影响报告。files/rev 均未给时走 svn st。"""
    t0 = time.time()
    depth = depth or 99
    _validate_output_args(mode, section, layer, offset, limit)
    # 1. 变更集
    if files:
        changed = [f.replace('\\', '/') for f in files]
        diffs = {}
    elif rev:
        changed = collect_svn_diff(cfg, rev)
        diffs = {f: _svn(['diff', '-r', rev,
                          cfg.root.rstrip('/\\') + '/' + f]) for f in changed}
    elif use_svn_status:
        changed = collect_svn_status(cfg)
        diffs = {}
    else:
        changed = []
        diffs = {}
    changed = [f for f in changed if not f.startswith('cache/')]

    # 2. 变更函数
    funcs = []
    callbacks = []
    for f in changed:
        funcs.extend(changed_functions(store, cfg, f, diffs.get(f)))
        callbacks.extend(changed_callbacks(store, f, diffs.get(f)))

    # 3. 调用链闭包（VERIFIED 边）
    direct, transitive, truncated = _impact_closure(
        store, cfg, funcs, depth, callbacks=callbacks)

    # 4. import 级维度
    idx = st.StructureIndex(store)
    imp_by_file = {}
    for f in changed:
        dsts = set(idx.scope_files(f))
        if not dsts:
            continue
        for (s, d) in idx.edges:
            if d in dsts and s not in imp_by_file.get(f, set()) | {f}:
                imp_by_file.setdefault(f, set()).add(s)

    report = {
        'schema_version': BLAST_SCHEMA_VERSION,
        'changed_files': sorted(changed),
        'changed_functions': [{'file': _func_file(store, fn), **fn} for fn in funcs],
        'changed_callbacks': [
            {'file': f, 'line': ln, 'class': cls, 'kind': kind,
             'source': source, 'target': target}
            for f, ln, cls, kind, source, target in callbacks],
        'direct_callers': sorted(
            direct, key=lambda i: (i['layer'], i['caller_file'],
                                   i['caller_line'], i['target'])),
        'transitive_callers': sorted(
            transitive, key=lambda i: (i['layer'], i['caller_file'],
                                       i['caller_line'], i['target'])),
        'truncated': truncated,
        'importers': {f: sorted(v) for f, v in imp_by_file.items()},
        'elapsed': round(time.time() - t0, 3),
    }
    projected = _project_report(report, mode, section, layer, offset, limit, depth)
    if json_out:
        return projected
    if mode != 'full':
        return _format_projection(projected)
    lines = ['blast-radius: 变更 %d 文件 / %d 函数, 闭包深度 %d%s'
             % (len(changed), len(funcs), depth, '（截断）' if truncated else '')]
    lines.append('  变更文件:')
    for f in sorted(changed):
        lines.append('    %s' % f)
    lines.append('  直接调用方（VERIFIED）: %d' % len(direct))
    for item in direct[:30]:
        lines.append('    %s  <- %s:%s in %s'
                     % (item['target'], item['caller_file'], item['caller_line'],
                        item['caller']))
    if transitive:
        lines.append('  传递调用方（%d 层内）: %d' % (depth, len(transitive)))
        for item in transitive[:20]:
            lines.append('    %s  (via %s)' % (item['caller_loc'], item['via']))
    imp_total = sum(len(v) for v in imp_by_file.values())
    lines.append('  import 级 importers: %d' % imp_total)
    return '\n'.join(lines)


def _validate_output_args(mode, section, layer, offset, limit):
    if mode not in ('full', 'summary', 'page'):
        raise ValueError('mode 必须是 full/summary/page')
    if mode == 'page':
        if section not in ('callers', 'importers'):
            raise ValueError('section 必须是 callers/importers')
        if layer < 1:
            raise ValueError('layer 必须 >= 1')
        if offset < 0:
            raise ValueError('offset 必须 >= 0')
        if limit < 1 or limit > MAX_OUTPUT_LIMIT:
            raise ValueError('limit 必须在 1..%d' % MAX_OUTPUT_LIMIT)


def _project_report(report, mode, section, layer, offset, limit, depth):
    """完整计算结果 -> full/summary/page 输出；分页不改变闭包计算。"""
    _validate_output_args(mode, section, layer, offset, limit)
    callers = report['direct_callers'] + report['transitive_callers']
    by_layer = {}
    for item in callers:
        by_layer[item['layer']] = by_layer.get(item['layer'], 0) + 1
    importer_items = [
        {'changed_file': changed_file, 'importer': importer}
        for changed_file, importers in sorted(report['importers'].items())
        for importer in importers]
    summary = {
        'changed_files': len(report['changed_files']),
        'changed_functions': len(report['changed_functions']),
        'changed_callbacks': len(report['changed_callbacks']),
        'callers_total': len(callers),
        'caller_layers': [{'layer': n, 'total': by_layer[n]}
                          for n in sorted(by_layer)],
        'importers_total': len(importer_items),
        'depth': depth,
        'truncated': report['truncated'],
        'max_edges': MAX_EDGES,
    }
    if mode == 'full':
        out = dict(report)
        out['mode'] = mode
        out['summary'] = summary
        return out
    out = {
        'schema_version': BLAST_SCHEMA_VERSION,
        'mode': mode,
        'summary': summary,
        'elapsed': report['elapsed'],
    }
    if mode == 'summary':
        return out
    if section == 'callers':
        source = [item for item in callers if item['layer'] == layer]
    else:
        source = importer_items
    items = source[offset:offset + limit]
    next_offset = offset + len(items)
    out['page'] = {
        'section': section,
        'layer': layer if section == 'callers' else None,
        'offset': offset,
        'limit': limit,
        'total': len(source),
        'returned': len(items),
        'has_more': next_offset < len(source),
        'next_offset': next_offset if next_offset < len(source) else None,
        'items': items,
    }
    return out


def _format_projection(out):
    summary = out['summary']
    lines = ['blast-radius %s: 变更 %d 文件 / %d 函数 / %d 约定声明'
             % (out['mode'], summary['changed_files'],
                summary['changed_functions'], summary['changed_callbacks'])]
    lines.append('  callers=%d importers=%d%s'
                 % (summary['callers_total'], summary['importers_total'],
                    '（闭包截断）' if summary['truncated'] else ''))
    if out['mode'] == 'page':
        page = out['page']
        lines.append('  page %s%s offset=%d limit=%d: %d/%d%s'
                     % (page['section'],
                        (' layer=%d' % page['layer']) if page['layer'] else '',
                        page['offset'], page['limit'], page['returned'], page['total'],
                        '（还有下一页）' if page['has_more'] else ''))
        for item in page['items']:
            if page['section'] == 'callers':
                lines.append('    L%d %s:%s in %s'
                             % (item['layer'], item['caller_file'],
                                item['caller_line'], item['caller']))
            else:
                lines.append('    %s <- %s'
                             % (item['changed_file'], item['importer']))
    return '\n'.join(lines)


def _func_file(store, fn):
    return fn.get('file', '')


def _impact_closure(store, cfg, funcs, depth, callbacks=None):
    """变更函数 -> {直接, 传递} 调用方。visited 防环，MAX_EDGES 截断。

    性能要点：共享一个 Resolver（callers 每次冷路径新建会全表初始化）；
    frontier 按 (class, func, file) 去重，同名函数不重复展开。
    """
    direct = []
    seen_direct = set()
    transitive = []
    seen_trans = set()
    n_edges = 0
    truncated = False
    resolver = None
    queued = set()
    frontier = []

    def enqueue(fn, level):
        key = (fn['class'], fn['func'], fn.get('file'))
        if key in queued:
            return
        queued.add(key)
        frontier.append((fn, level))

    for fn in funcs:
        # 变更函数的定义限定在其文件内（--files 模式下 file 一定有值）
        if fn.get('file'):
            rows = store.con.execute(
                'SELECT line FROM defs WHERE class IS ? AND name=? AND file=?',
                (fn['class'], fn['func'], fn['file'])).fetchall()
            targets = [(fn['file'], r[0]) for r in rows]
        else:
            targets = []
        for (df, dl) in targets:
            enqueue({'class': fn['class'], 'func': fn['func'], 'file': df}, 0)

    # 声明式框架边：由 custom 钩子产出通用 callback_raw，core 只按
    # 同类/继承/@Components 宿主验证。声明变更时，目标回调属于第一层影响。
    if callbacks:
        resolver = rmod.Resolver(store, cfg)
        for raw in callbacks:
            for cb in resolver.convention_targets(raw):
                n_edges += 1
                if n_edges > MAX_EDGES:
                    truncated = True
                    break
                key = (cb['target_file'], cb['target_line'])
                if key in seen_direct:
                    continue
                seen_direct.add(key)
                direct.append({
                    'layer': 1,
                    'target': '%s %s.%s' % (
                        cb['kind'], cb['source_class'], cb['source']),
                    'caller': rmod._display(cb['target_class'], cb['target']),
                    'caller_file': cb['target_file'],
                    'caller_line': cb['target_line'],
                    'via_callback': {'kind': cb['kind'],
                                     'source': cb['source'],
                                     'hosts': cb['hosts']},
                })
                enqueue({'class': cb['target_class'], 'func': cb['target'],
                         'file': cb['target_file']}, 1)
            if truncated:
                break

    while frontier:
        (fn, level) = frontier.pop(0)
        if level >= depth:
            continue
        if resolver is None:
            resolver = rmod.Resolver(store, cfg)
        # 超高频名（__init__/Create 等）：上万候选的冷验证是分钟级且对影响面
        # 无增量信息；只吃 edges 缓存，没有缓存就跳过该节点
        n_cands = store.con.execute(
            'SELECT COUNT(*) FROM names WHERE name=?', (fn['func'],)).fetchone()[0]
        if n_cands > MAX_NAME_CANDS:
            def_line = store.con.execute(
                'SELECT line FROM defs WHERE file=? AND name=? AND class IS ? '
                'ORDER BY line LIMIT 1',
                (fn['file'], fn['func'], fn['class'])).fetchone()
            items = rmod._load_edges(store, cfg, fn['func'], fn['file'],
                                     def_line[0] if def_line else -1, 'callers') \
                if def_line else None
            out = {'items': items} if items is not None else None
        else:
            out = rmod.callers(store, cfg, fn['file'], fn['func'], resolver=resolver)
        if out is not None:
            for item in out['items']:
                if item['level'] != 'VERIFIED':
                    continue
                n_edges += 1
                if n_edges > MAX_EDGES:
                    truncated = True
                    frontier = []
                    break
                key = (item['file'], item['line'])
                entry = {
                    'layer': level + 1,
                    'target': ('%s.%s' % (fn['class'], fn['func'])) if fn['class'] else fn['func'],
                    'caller': item['caller'], 'caller_file': item['file'],
                    'caller_line': item['line'],
                }
                if level == 0:
                    if key not in seen_direct:
                        seen_direct.add(key)
                        direct.append(entry)
                else:
                    if key not in seen_trans:
                        seen_trans.add(key)
                        transitive.append({
                            'layer': level + 1,
                            'target': entry['target'],
                            'caller': item['caller'],
                            'caller_file': item['file'],
                            'caller_line': item['line'],
                            'caller_loc': '%s:%s in %s'
                                          % (item['file'], item['line'], item['caller']),
                            'via': entry['target']})
                # 调用点外层函数：查其真实定义（class+name 维度）后继续向上
                caller_cls, caller_func = _split_display(item['caller'])
                if not caller_func:
                    continue
                enqueue({'class': caller_cls, 'func': caller_func, 'file': item['file']},
                        level + 1)
        # RPC 边穿透：变更/途经函数是 handler（名匹配 rpc.method，stub 不符或
        # NULL 均算），其远端调用点进闭包并继续向上（RPC-INFERRED 入边）
        rpc_rows = store.con.execute(
            'SELECT file, line, chan, stub FROM rpc WHERE method=?',
            (fn['func'],)).fetchall()
        for (rf, rln, chan, rstub) in rpc_rows:
            if fn['class'] and rstub and rstub != fn['class']:
                continue  # stub 已知且指向别的类，不是本 handler 的边
            if rf == fn['file'] and rln == fn.get('line'):
                continue  # 自身文件内的定义行兜底（rpc 行应为调用点，防御）
            n_edges += 1
            if n_edges > MAX_EDGES:
                truncated = True
                frontier = []
                break
            key = (rf, rln)
            if resolver is None:
                resolver = rmod.Resolver(store, cfg)
            rcls, rfunc = resolver._enclosing_of(rf, rln)
            target = ('%s.%s' % (fn['class'], fn['func'])) if fn['class'] else fn['func']
            entry = {'layer': level + 1,
                     'target': target, 'caller': rmod._display(rcls, rfunc),
                     'caller_file': rf, 'caller_line': rln,
                     'via_rpc': chan}
            if level == 0:
                if key not in seen_direct:
                    seen_direct.add(key)
                    direct.append(entry)
            else:
                if key not in seen_trans:
                    seen_trans.add(key)
                    transitive.append({
                        'layer': level + 1,
                        'target': target,
                        'caller': entry['caller'],
                        'caller_file': rf,
                        'caller_line': rln,
                        'caller_loc': '%s:%s in %s' % (rf, rln, entry['caller']),
                        'via': '%s(rpc:%s)' % (target, chan),
                        'via_rpc': chan})
            if rfunc:
                enqueue({'class': rcls, 'func': rfunc, 'file': rf}, level + 1)
        # pubsub 事件边穿透（双向）：frontier 函数是订阅 handler -> 同事件的
        # 发布点入影响面；frontier 是发布调用点所在函数 -> 同事件全部订阅
        # handler 入影响面。事件键匹配即成边（非语义验证）；订阅行按
        # file+func+cls 全等配（防同名 handler 误配），发布行按 file+func 配
        lis = store.con.execute(
            'SELECT event FROM pubsub WHERE side=? AND file=? AND func=? AND '
            'cls IS ?',
            ('listen', fn['file'], fn['func'], fn['class'])).fetchall()
        pub = store.con.execute(
            'SELECT event FROM pubsub WHERE side=? AND file=? AND func=?',
            ('publish', fn['file'], fn['func'])).fetchall()
        edges = []
        for (e,) in lis:
            edges.extend(
                (e, rf, rln, rfunc, rcls) for (rf, rln, rfunc, rcls) in
                store.con.execute(
                    'SELECT file, line, func, cls FROM pubsub '
                    'WHERE side=? AND event=? ORDER BY file, line',
                    ('publish', e)))
        for (e,) in pub:
            edges.extend(
                (e, rf, rln, rfunc, rcls) for (rf, rln, rfunc, rcls) in
                store.con.execute(
                    'SELECT file, line, func, cls FROM pubsub '
                    'WHERE side=? AND event=? ORDER BY file, line',
                    ('listen', e)))
        target = ('%s.%s' % (fn['class'], fn['func'])) if fn['class'] else fn['func']
        for (e, pf, pln, pfunc, pcls) in edges:
            n_edges += 1
            if n_edges > MAX_EDGES:
                truncated = True
                frontier = []
                break
            key = (pf, pln)
            entry = {'layer': level + 1,
                     'target': target,
                     'caller': '%s.%s' % (pcls, pfunc) if pcls else (pfunc or '?'),
                     'caller_file': pf, 'caller_line': pln,
                     'via_pubsub': e}
            if level == 0:
                if key not in seen_direct:
                    seen_direct.add(key)
                    direct.append(entry)
            else:
                if key not in seen_trans:
                    seen_trans.add(key)
                    transitive.append({
                        'layer': level + 1,
                        'target': target,
                        'caller': entry['caller'],
                        'caller_file': pf,
                        'caller_line': pln,
                        'caller_loc': '%s:%s in %s' % (pf, pln, entry['caller']),
                        'via': '%s(pubsub:%s)' % (target, e),
                        'via_pubsub': e})
            if pfunc:
                enqueue({'class': pcls, 'func': pfunc, 'file': pf}, level + 1)
    return direct, transitive, truncated


def _split_display(display):
    """'Class.func' -> (Class, func)；无类 -> (None, func)。"""
    if '.' in display:
        cls, func = display.rsplit('.', 1)
        return cls, func
    return None, display
