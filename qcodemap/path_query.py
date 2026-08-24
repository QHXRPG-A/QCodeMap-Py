# -*- coding: utf-8 -*-
"""跨普通调用与标准化 RPC 边的最短符号路径查询。"""

import time
from collections import deque

from qcodemap import resolve
from qcodemap import rpc_refs


def _node(store, file, line):
    row = store.con.execute(
        'SELECT class,name FROM defs WHERE file=? AND line=?',
        (file, line)).fetchone()
    if row is None:
        return None
    cls, name = row
    return {'file': file, 'line': line, 'class': cls, 'name': name,
            'symbol': resolve._display(cls, name)}


def _normal_edges(store, cfg, resolver, node):
    out = resolve.callees(
        store, cfg, node['file'], node['name'], def_line=node['line'],
        resolver=resolver)
    edges = []
    unresolved = 0
    for item in out['items']:
        if item['level'] == 'CANDIDATE':
            unresolved += 1
            continue
        if item['level'] != 'VERIFIED' and not item['level'].endswith('-INFERRED'):
            continue
        target = _node(store, item['file'], item['line'])
        if target is None:
            unresolved += 1
            continue
        edges.append((target, {
            'kind': 'call', 'level': item['level'],
            'from': node['symbol'], 'to': target['symbol'],
            'site': item.get('caller'), 'note': item.get('note'),
        }))
    return edges, unresolved


def _rpc_edges(store, cfg, resolver, node):
    edges = []
    seen = set()
    rows = store.con.execute(
        'SELECT line,chan,method,stub FROM rpc WHERE file=? ORDER BY line',
        (node['file'],)).fetchall()
    for line, chan, method, stub in rows:
        cls, func = resolver._enclosing_of(node['file'], line)
        if cls != node['class'] or func != node['name']:
            continue
        refs = rpc_refs.rpc_refs(
            store, cfg, method, stub=stub, json_out=True, resolver=resolver)
        for item in refs['items']:
            if not item['level'].startswith('HANDLER-'):
                continue
            key = (item['file'], item['line'], line, chan)
            if key in seen:
                continue
            seen.add(key)
            target = _node(store, item['file'], item['line'])
            if target is None:
                continue
            edges.append((target, {
                'kind': 'rpc', 'level': 'RPC-INFERRED',
                'from': node['symbol'], 'to': target['symbol'],
                'site': '%s:%d' % (node['file'], line),
                'method': method, 'chan': chan,
                'endpoint_match': item.get('match'),
                'endpoints': item.get('endpoints', []),
            }))
    return edges


def path(store, cfg, from_symbol, to_symbol, max_depth=6, max_nodes=2000,
         json_out=True):
    """返回跨普通调用和 RPC 的一条最短路径；歧义端点先返回候选。"""
    t0 = time.time()
    source_resolution = resolve.resolve_symbol(store, cfg, from_symbol)
    target_resolution = resolve.resolve_symbol(store, cfg, to_symbol)
    source = source_resolution['selected']
    target = target_resolution['selected']
    base = {
        'schema_version': 'qcodemap.path/v1',
        'from': from_symbol, 'to': to_symbol,
        'from_resolution': source_resolution,
        'to_resolution': target_resolution,
        'max_depth': max_depth,
    }
    if source is None or target is None:
        base.update({
            'found': False, 'path': None, 'explored': 0,
            'truncated': False, 'unresolved_edges': 0,
            'note': '起点或终点未唯一解析；请按 resolution.candidates 消歧',
            'coverage': resolve.coverage_details(
                store,
                relevant_files=[i['file'] for i in
                                source_resolution['candidates']
                                + target_resolution['candidates']],
                symbols=[from_symbol.rsplit('.', 1)[-1],
                         to_symbol.rsplit('.', 1)[-1]]),
            'elapsed': round(time.time() - t0, 3),
        })
        return base if json_out else _format(base)

    source_node = _node(store, source['file'], source['line'])
    target_key = (target['file'], target['line'])
    source_key = (source['file'], source['line'])
    queue = deque([(source_node, [source_node], [])])
    seen = {source_key}
    resolver = resolve.Resolver(store, cfg)
    found_nodes = found_edges = None
    unresolved_edges = 0
    touched_files = {source['file'], target['file']}
    touched_symbols = {source['name'], target['name']}
    truncated = False
    while queue:
        current, nodes, edges = queue.popleft()
        current_key = (current['file'], current['line'])
        if current_key == target_key:
            found_nodes, found_edges = nodes, edges
            break
        if len(edges) >= max_depth:
            continue
        normal, n_unresolved = _normal_edges(store, cfg, resolver, current)
        unresolved_edges += n_unresolved
        outgoing = normal + _rpc_edges(store, cfg, resolver, current)
        for next_node, edge in outgoing:
            key = (next_node['file'], next_node['line'])
            touched_files.add(next_node['file'])
            touched_symbols.add(next_node['name'])
            if key in seen:
                continue
            if len(seen) >= max_nodes:
                truncated = True
                queue.clear()
                break
            seen.add(key)
            queue.append((next_node, nodes + [next_node], edges + [edge]))

    base.update({
        'found': found_nodes is not None,
        'path': ({'nodes': found_nodes, 'edges': found_edges,
                  'length': len(found_edges)} if found_nodes is not None else None),
        'explored': len(seen), 'truncated': truncated,
        'unresolved_edges': unresolved_edges,
        'note': ('' if found_nodes is not None else
                 '在深度/节点上限内未找到由已验证或标准化推断边组成的路径'),
        'coverage': resolve.coverage_details(
            store, relevant_files=touched_files, symbols=touched_symbols),
        'elapsed': round(time.time() - t0, 3),
    })
    return base if json_out else _format(base)


def _format(out):
    if not out['found']:
        lines = ['path %s -> %s: 未找到' % (out['from'], out['to'])]
        if out.get('note'):
            lines.append('  note: %s' % out['note'])
        for side in ('from_resolution', 'to_resolution'):
            resolution = out.get(side) or {}
            if resolution.get('status') == 'ambiguous':
                lines.append('  %s candidates:' % side.split('_', 1)[0])
                for item in resolution['candidates']:
                    lines.append('    %s  %s:%s' % (
                        item['symbol'], item['file'], item['line']))
        return '\n'.join(lines)
    lines = ['path %s -> %s: %d edge(s)' % (
        out['from'], out['to'], out['path']['length'])]
    for index, edge in enumerate(out['path']['edges']):
        lines.append('  %d. [%s/%s] %s -> %s%s' % (
            index + 1, edge['kind'], edge['level'], edge['from'], edge['to'],
            (' @ ' + edge['site']) if edge.get('site') else ''))
    return '\n'.join(lines)
