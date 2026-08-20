# -*- coding: utf-8 -*-
"""rpc-refs：字符串分发 RPC 的双端配对查询（P3-2）。

rpc 表存调用点事实（file/line/chan/method/stub，来自 custom 钩子提取）；
handler 侧优先消费 custom 产出的装饰器/方向事实；其余同名 def
仅作 NAME-ONLY 兜底，不伪装成精确配对。
"""

import time

from qcodemap import resolve as rmod


def _chan_names(cfg):
    """通道显示名（custom/config.py 的 RPC_CHANNELS 可覆盖）。"""
    default = {'C2S': '客户端→服务端', 'S2C': '服务端→客户端',
               'MAILBOX': 'mailbox 回调', 'STUB': '跨服 stub', 'DES': 'Des 协议'}
    return getattr(cfg, 'rpc_channels', None) or default


def rpc_refs(store, cfg, method, stub=None, json_out=True):
    """RPC 方法名 -> 双端清单：调用点（rpc 表）+ handler 定义（defs 表）。"""
    t0 = time.time()
    items = []
    if stub:
        rows = store.con.execute(
            'SELECT file, line, chan, stub FROM rpc WHERE method=? AND stub=? '
            'ORDER BY file, line', (method, stub)).fetchall()
    else:
        rows = store.con.execute(
            'SELECT file, line, chan, stub FROM rpc WHERE method=? '
            'ORDER BY file, line', (method,)).fetchall()
    r = rmod.Resolver(store, cfg)
    for (f, ln, chan, rstub) in rows:
        cls, fname = r._enclosing_of(f, ln)
        items.append({'level': 'RPC-INFERRED', 'chan': chan,
                      'stub': rstub, 'file': f, 'line': ln,
                      'caller': rmod._display(cls, fname)})
    call_chans = {i['chan'] for i in items if i['level'] == 'RPC-INFERRED'}
    handler_rows = store.con.execute(
        'SELECT file,line,chan,endpoint,confidence,reason FROM rpc_handler '
        'WHERE method=? ORDER BY file,line', (method,)).fetchall()
    handler_locations = set()
    for f, ln, chan, endpoint, confidence, reason in handler_rows:
        endpoint_match = not stub or endpoint == stub
        direction_match = not call_chans or chan in call_chans \
            or (chan == 'SERVER' and 'STUB' in call_chans)
        if not endpoint_match or not direction_match:
            continue
        level = ('HANDLER-VERIFIED' if confidence == 'verified'
                 else 'HANDLER-INFERRED')
        items.append({
            'level': level, 'chan': chan, 'stub': endpoint,
            'file': f, 'line': ln,
            'caller': '%s.%s' % (endpoint, method) if endpoint else method,
            'match': 'direction+endpoint' if stub else 'direction',
            'note': reason,
        })
        handler_locations.add((f, ln))
    defs = store.con.execute(
        'SELECT file, line, class FROM defs WHERE name=? ORDER BY file, line',
        (method,)).fetchall()
    for (f, ln, dcls) in defs:
        if (f, ln) in handler_locations:
            continue
        items.append({'level': 'NAME-ONLY', 'stub': dcls, 'file': f,
                      'line': ln,
                      'caller': '%s.%s' % (dcls, method) if dcls else method,
                      'match': 'name-only'})
    # 调用点落在 ast 失败文件的场景（如 cimp_replay.py 的 CallServerNew）
    # 只索引 names 不产 rpc 行——note 提示走 usages 补漏
    note = ''
    bad = store.parse_failed_files()
    if bad:
        note = ('全库 %d 个文件 ast 解析失败（仅 names 索引），其中的 RPC 调用'
                '不在结果内，可用 usages %s 补查' % (len(bad), method))
    items.sort(key=lambda i: (i['level'], i['file'], i['line']))
    out = {
        'schema_version': 'qcodemap.rpc/v2',
        'method': method, 'stub': stub,
        'channels': _chan_names(cfg),
        'items': items,
        'n_rpc': sum(1 for i in items if i['level'] == 'RPC-INFERRED'),
        'n_handler': sum(1 for i in items if i['level'].startswith('HANDLER-')),
        'n_name_only': sum(1 for i in items if i['level'] == 'NAME-ONLY'),
        'note': note,
        'coverage': {'status': 'partial' if bad else 'complete',
                     'parse_failed': len(bad)},
        'elapsed': round(time.time() - t0, 3),
    }
    if json_out:
        return out
    chans = _chan_names(cfg)
    lines = ['rpc-refs %s%s: 调用点 %d / handler %d'
             % (method, (' @' + stub) if stub else '',
                out['n_rpc'], out['n_handler'])]
    for i in items:
        tag = chans.get(i['chan'], i['chan']) if i.get('chan') else ''
        lines.append('  [%s]%s %s:%s  in %s'
                     % (i['level'], ('(%s)' % tag) if tag else '',
                        i['file'], i['line'], i.get('caller') or '?'))
    if note:
        lines.append('  note: %s' % note)
    return '\n'.join(lines)
