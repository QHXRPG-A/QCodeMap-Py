# -*- coding: utf-8 -*-
"""rpc-refs：标准化 RPC 调用、handler、组件宿主与 endpoint 别名配对。"""

import time

from qcodemap import resolve as rmod


def _chan_names(cfg):
    """通道代码 -> 显示名；词表由 custom 配置提供。"""
    return getattr(cfg, 'rpc_channels', None) or {}


def _chan_compatible(cfg, handler_chan, call_chan):
    if handler_chan == call_chan:
        return True
    aliases = getattr(cfg, 'rpc_chan_aliases', None) or {}
    return (call_chan in aliases.get(handler_chan, ())
            or handler_chan in aliases.get(call_chan, ()))


def _alias_closure(store, endpoints):
    """标准 endpoint_alias 事实按等价关系闭包；不解释 alias 的来源语法。"""
    seen = set(e for e in endpoints if e)
    queue = list(seen)
    while queue:
        endpoint = queue.pop(0)
        rows = store.con.execute(
            'SELECT endpoint,alias FROM endpoint_alias '
            'WHERE endpoint=? OR alias=?', (endpoint, endpoint)).fetchall()
        for left, right in rows:
            for item in (left, right):
                if item and item not in seen:
                    seen.add(item)
                    queue.append(item)
    return seen


def endpoint_variants(store, resolver, endpoint, endpoint_file=None):
    """实现类 -> 自身、继承/组件运行时宿主、跨端 alias 的等价端名集合。"""
    endpoints = {endpoint} if endpoint else set()
    if endpoint:
        class_files = resolver._class_files(endpoint, endpoint_file)
        if endpoint_file in class_files:
            class_files = [endpoint_file]
        for class_file in class_files:
            endpoints.update(host for host, _host_file in
                             resolver.runtime_hosts(endpoint, class_file))
    return _alias_closure(store, endpoints)


def _handler_candidates(store, cfg, resolver, method, stub):
    rows = store.con.execute(
        'SELECT file,line,chan,endpoint,confidence,reason FROM rpc_handler '
        'WHERE method=? ORDER BY file,line', (method,)).fetchall()
    out = []
    excluded = 0
    for f, ln, chan, endpoint, confidence, reason in rows:
        variants = endpoint_variants(store, resolver, endpoint, f)
        if stub and stub not in variants:
            excluded += 1
            continue
        out.append({
            'file': f, 'line': ln, 'chan': chan,
            'endpoint': endpoint, 'endpoints': sorted(variants),
            'confidence': confidence, 'reason': reason,
        })
    return out, excluded, len(rows)


def _definition_matches_endpoint(store, resolver, method, file, line, cls, stub):
    """同名 def 是否真是指定运行时 endpoint 上生效的实现。"""
    if not cls:
        return False
    class_files = resolver._class_files(cls, file)
    if file in class_files:
        class_files = [file]
    target = (file, line)
    for class_file in class_files:
        for host, host_file in resolver.runtime_hosts(cls, class_file):
            # host 已是逐个展开后的具体运行时端；这里只看它自身的 alias，
            # 不能再次把其派生宿主并回，否则被组件覆盖的基类同名方法会回流。
            if stub not in _alias_closure(store, {host}):
                continue
            if resolver.mro_has_method(host, host_file, method) == target:
                return True
    return False


def rpc_refs(store, cfg, method, stub=None, json_out=True, resolver=None):
    """RPC 方法名 -> 调用点 + 严格 handler 配对；stub 是 endpoint 过滤器。"""
    t0 = time.time()
    resolver = resolver or rmod.Resolver(store, cfg)
    raw_calls = store.con.execute(
        'SELECT file,line,chan,stub FROM rpc WHERE method=? ORDER BY file,line',
        (method,)).fetchall()
    handlers, excluded_handlers, total_handlers = _handler_candidates(
        store, cfg, resolver, method, stub)

    # 先按显式 endpoint / 未定域调用形成过滤候选，再用通道验证 handler。
    call_candidates = []
    excluded_calls = 0
    for f, ln, chan, call_stub in raw_calls:
        if not stub or call_stub is None:
            call_candidates.append((f, ln, chan, call_stub, 'unscoped'))
            continue
        variants = endpoint_variants(store, resolver, call_stub)
        if stub in variants:
            call_candidates.append((f, ln, chan, call_stub, 'explicit'))
        else:
            excluded_calls += 1
    call_chans = {row[2] for row in call_candidates}
    direction_handlers = []
    for handler in handlers:
        if call_chans and not any(_chan_compatible(
                cfg, handler['chan'], call_chan) for call_chan in call_chans):
            excluded_handlers += 1
            continue
        direction_handlers.append(handler)
    handlers = direction_handlers

    items = []
    for f, ln, chan, call_stub, scope in call_candidates:
        if stub and call_stub is None and not any(
                _chan_compatible(cfg, handler['chan'], chan)
                for handler in handlers):
            excluded_calls += 1
            continue
        cls, fname = resolver._enclosing_of(f, ln)
        item = {
            'level': 'RPC-INFERRED', 'chan': chan, 'stub': call_stub,
            'file': f, 'line': ln, 'caller': rmod._display(cls, fname),
            'match': ('method+endpoint' if scope == 'explicit'
                      else ('method+direction-via-handler' if stub
                            else 'method')),
        }
        if stub and call_stub is None:
            item['note'] = ('调用点未携带 endpoint；仅因存在 endpoint 与通道均匹配的 '
                            'handler 而纳入')
        items.append(item)

    all_handler_locations = set(tuple(row) for row in store.con.execute(
        'SELECT file,line FROM rpc_handler WHERE method=?',
        (method,)).fetchall())
    for handler in handlers:
        endpoint = handler['endpoint']
        level = ('HANDLER-VERIFIED' if handler['confidence'] == 'verified'
                 else 'HANDLER-INFERRED')
        items.append({
            'level': level, 'chan': handler['chan'], 'stub': endpoint,
            'declared_endpoint': endpoint,
            'endpoints': handler['endpoints'],
            'matched_endpoint': stub if stub else None,
            'file': handler['file'], 'line': handler['line'],
            'caller': '%s.%s' % (endpoint, method) if endpoint else method,
            'match': ('direction+endpoint' if stub else 'direction'),
            'note': handler['reason'],
        })

    defs = store.con.execute(
        'SELECT file,line,class FROM defs WHERE name=? ORDER BY file,line',
        (method,)).fetchall()
    excluded_name_only = 0
    for f, ln, dcls in defs:
        if (f, ln) in all_handler_locations:
            continue
        variants = endpoint_variants(store, resolver, dcls, f) if dcls else set()
        if stub and not _definition_matches_endpoint(
                store, resolver, method, f, ln, dcls, stub):
            excluded_name_only += 1
            continue
        items.append({
            'level': 'NAME-ONLY', 'stub': dcls,
            'endpoints': sorted(variants), 'file': f, 'line': ln,
            'caller': '%s.%s' % (dcls, method) if dcls else method,
            'match': 'name+endpoint' if stub else 'name-only',
        })

    coverage = rmod.coverage_details(
        store, relevant_files=[i['file'] for i in items], symbols=[method])
    note = ''
    if coverage['status'] == 'partial':
        if coverage['issues']:
            note = ('全库 %d 个文件 ast 解析失败；coverage.issues 已列出与 %s '
                    '相关的缺失文件' % (coverage['parse_failed'], method))
        else:
            note = ('全库 %d 个文件 ast 解析失败；未发现与 %s 同名倒排命中的'
                    '失败文件' % (coverage['parse_failed'], method))
    unmatched = []
    if stub:
        if total_handlers and not handlers:
            unmatched.append({
                'code': 'NO_MATCHING_HANDLER',
                'reason': '存在同名 handler，但 endpoint 或通道与过滤条件不匹配',
            })
        elif not total_handlers:
            unmatched.append({
                'code': 'NO_HANDLER_FACT',
                'reason': '没有该方法的标准化 handler 事实',
            })
        if not any(i['level'] == 'RPC-INFERRED' for i in items):
            unmatched.append({
                'code': 'NO_MATCHING_CALL',
                'reason': '没有显式 endpoint 匹配或可由匹配 handler 定域的调用点',
            })
        if not items:
            unmatched.append({
                'code': 'NO_RESULT',
                'reason': 'stub 过滤为严格模式，不回退展示其他 endpoint 的同名项',
            })

    items.sort(key=lambda i: (i['level'], i['file'], i['line']))
    out = {
        'schema_version': 'qcodemap.rpc/v3',
        'method': method, 'stub': stub,
        'channels': _chan_names(cfg),
        'items': items,
        'n_rpc': sum(1 for i in items if i['level'] == 'RPC-INFERRED'),
        'n_handler': sum(1 for i in items if i['level'].startswith('HANDLER-')),
        'n_name_only': sum(1 for i in items if i['level'] == 'NAME-ONLY'),
        'filter': {
            'strict': bool(stub), 'endpoint': stub,
            'excluded_rpc': excluded_calls,
            'excluded_handler': excluded_handlers,
            'excluded_name_only': excluded_name_only,
        },
        'unmatched': unmatched,
        'note': note, 'coverage': coverage,
        'elapsed': round(time.time() - t0, 3),
    }
    if json_out:
        return out
    chans = _chan_names(cfg)
    lines = ['rpc-refs %s%s: 调用点 %d / handler %d / name-only %d'
             % (method, (' @' + stub) if stub else '', out['n_rpc'],
                out['n_handler'], out['n_name_only'])]
    for item in items:
        tag = chans.get(item.get('chan'), item.get('chan')) if item.get('chan') else ''
        lines.append('  [%s]%s %s:%s  in %s'
                     % (item['level'], ('(%s)' % tag) if tag else '',
                        item['file'], item['line'], item.get('caller') or '?'))
    for item in unmatched:
        lines.append('  unmatched[%s]: %s' % (item['code'], item['reason']))
    if note:
        lines.append('  note: %s' % note)
    return '\n'.join(lines)
