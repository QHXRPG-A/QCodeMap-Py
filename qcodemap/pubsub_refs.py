# -*- coding: utf-8 -*-
"""pubsub-refs：事件分发双端配对查询（P3-1）。

pubsub 表存两侧事实（listen=订阅 handler / publish=发布调用点，事件键为
经 import 归一的「模块路径.常量名」，见 custom/facts.py 的归一约定）。
裸事件名按后缀匹配（多命中分组），完整键全等匹配；输出分级：
EVENT-INFERRED（推断发布点，非语义验证）/ LISTENER（订阅 handler）。
"""

import time


def _match_events(store, event):
    """入参事件名 -> 命中的完整事件键分组。

    含点视为完整键全等匹配；裸名按 '%.<name>' 后缀匹配（防跨端撞名，
    客户端 events.ON_X 与服务端 sconst 的同名常量分开成组）。
    """
    if '.' in event:
        return [event]
    rows = store.con.execute(
        'SELECT DISTINCT event FROM pubsub ORDER BY event').fetchall()
    return [e for (e,) in rows if e.endswith('.' + event)]


def pubsub_refs(store, cfg, event, side=None, json_out=True):
    """事件（名或完整键）-> 双端清单：发布调用点 + 订阅 handler。"""
    t0 = time.time()
    events = _match_events(store, event)
    groups = []
    total_pub = total_lis = 0
    for ev in events:
        q = 'SELECT file, line, side, event, func, cls FROM pubsub WHERE event=?'
        params = [ev]
        if side:
            q += ' AND side=?'
            params.append(side)
        rows = store.con.execute(q + ' ORDER BY file, line', params).fetchall()
        items = []
        n_pub = n_lis = 0
        for (f, ln, s, e, func, cls) in rows:
            who = '%s.%s' % (cls, func) if cls else (func or '?')
            if s == 'publish':
                items.append({'level': 'EVENT-INFERRED', 'event': e,
                              'file': f, 'line': ln, 'caller': who})
                n_pub += 1
            else:
                items.append({'level': 'LISTENER', 'event': e,
                              'file': f, 'line': ln, 'caller': who,
                              'unresolved': e.startswith('?') or None})
                n_lis += 1
        groups.append({'event': ev, 'items': items,
                       'n_publish': n_pub, 'n_listener': n_lis})
        total_pub += n_pub
        total_lis += n_lis
    note = ''
    bad = store.parse_failed_files()
    if bad:
        note = ('全库 %d 个文件 ast 解析失败（仅 names 索引），其中的事件调用'
                '不在结果内，可用 usages 补查' % len(bad))
    out = {
        'schema_version': 'qcodemap.pubsub/v1',
        'event': event, 'matched_events': events,
        'side': side,
        'groups': groups,
        'n_publish': total_pub, 'n_listener': total_lis,
        'note': note,
        'coverage': {'status': 'partial' if bad else 'complete',
                     'parse_failed': len(bad)},
        'elapsed': round(time.time() - t0, 3),
    }
    if json_out:
        return out
    lines = ['pubsub-refs %s: 命中 %d 个事件键  发布点 %d / 订阅 %d'
             % (event, len(events), total_pub, total_lis)]
    for g in groups:
        lines.append('== %s  发布 %d / 订阅 %d'
                     % (g['event'], g['n_publish'], g['n_listener']))
        for i in g['items']:
            tag = ' (unresolved)' if i.get('unresolved') else ''
            lines.append('  [%s]%s %s:%s  in %s'
                         % (i['level'], tag, i['file'], i['line'], i['caller']))
    if note:
        lines.append('  note: %s' % note)
    return '\n'.join(lines)
