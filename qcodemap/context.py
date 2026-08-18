# -*- coding: utf-8 -*-
"""P4 agent 消费面三命令：find_file / get_file_context / context。

全部基于已建好的索引查表，无 ast 现扫，毫秒级：
- find_file：模糊路径搜索（对齐 codemap find_file 的子串匹配语义）；
- get_file_context：单文件消费面打包（defs/imports/importers/枢纽/框架事实），
  省去 agent 多次往返；
- context：一次性机器可读项目档案，AI 会话冷启动注入用（对齐
  codemap context --compact 的定位，intent/skills 等字段不追齐）。
"""

import time

from qcodemap import structure as st

CONTEXT_SCHEMA_VERSION = 'qcodemap.context/v1'

# context compact 模式各列表截断（token 受限注入场景）
_COMPACT_TREE = 15
_COMPACT_HUBS = 10
_COMPACT_EXTERNAL = 10


def find_file(store, pattern, limit=50, json_out=True):
    """模糊路径搜索：子串匹配（ASCII 大小写不敏感），短路径优先。"""
    t0 = time.time()
    # 转义 LIKE 通配符，保证真子串语义（文件名常见下划线不能当任意字符）
    esc = (pattern.replace('\\', '\\\\').replace('%', '\\%')
           .replace('_', '\\_')).lower()
    rows = store.con.execute(
        'SELECT path, parse_ok FROM files '
        "WHERE LOWER(path) LIKE ? ESCAPE '\\' "
        'ORDER BY LENGTH(path), path LIMIT ?',
        ('%' + esc + '%', limit + 1)).fetchall()
    has_more = len(rows) > limit
    matches = [{'file': p, 'parse_ok': bool(ok)} for (p, ok) in rows[:limit]]
    out = {
        'schema_version': st.SCHEMA_VERSION, 'pattern': pattern,
        'matches': matches, 'count': len(matches), 'truncated': has_more,
        'elapsed': round(time.time() - t0, 3),
    }
    if json_out:
        return out
    lines = ['find %s: %d 个文件%s' % (pattern, len(matches),
                                       '（截断）' if has_more else '')]
    for m in matches:
        lines.append('  %s%s' % (m['file'], '' if m['parse_ok'] else '  [ast失败]'))
    return '\n'.join(lines)


def get_file_context(store, cfg, file, json_out=True):
    """单文件完整消费面：定义 + 依赖双向 + 枢纽判定 + 框架事实。"""
    t0 = time.time()
    file = file.replace('\\', '/')
    row = store.con.execute(
        'SELECT parse_ok FROM files WHERE path=?', (file,)).fetchone()
    if row is None:
        out = {'schema_version': st.SCHEMA_VERSION, 'file': file,
               'note': '目标不在索引内（可用 qcodemap_find_file 定位）',
               'elapsed': round(time.time() - t0, 3)}
        return out if json_out else out['note']
    parse_ok = bool(row[0])

    classes = [{'name': n, 'line': ln, 'bases': (b or '').split(',')}
               for (n, b, ln) in store.con.execute(
                   'SELECT name, bases, line FROM classes WHERE file=? '
                   'ORDER BY line', (file,))]
    defs = [{'name': n, 'line': ln, 'class': c}
            for (ln, c, n) in store.con.execute(
                'SELECT line, class, name FROM defs WHERE file=? ORDER BY line',
                (file,))]

    idx = st.StructureIndex(store)
    imports = sorted({d for (s, d) in idx.edges if s == file})
    external = sorted({m for (s, m) in idx.external if s == file})
    importers = sorted({s for (s, d) in idx.edges if d == file})
    indeg = st.hub_indegrees(idx)
    ordered = sorted(indeg.values(), reverse=True)
    p95 = ordered[max(0, int(len(ordered) * 0.95) - 1)] if ordered else 0
    n_imp = indeg.get(file, 0)
    is_hub = n_imp >= max(p95, 10)

    attr = [{'class': c, 'attr': a, 'type': t} for (c, a, t) in store.con.execute(
        'SELECT class, attr, type FROM attr WHERE file=? ORDER BY class, attr',
        (file,))]
    comps = [{'host': h, 'host_file': hf, 'comp': cp, 'comp_file': cf}
             for (h, hf, cp, cf) in store.con.execute(
                 'SELECT host, host_file, comp, comp_file FROM comp WHERE comp_file=? '
                 'ORDER BY host', (file,))]
    callbacks = [
        {'line': ln, 'class': cls, 'kind': kind, 'source': source,
         'target': target}
        for ln, cls, kind, source, target in store.con.execute(
            'SELECT line,class,kind,source,target FROM callback_raw WHERE file=? '
            'ORDER BY line', (file,))]

    cov = st._coverage(store, [file], len(imports) + len(importers),
                       len(external))
    note = '' if parse_ok else '目标文件 ast 解析失败，定义/事实为空（索引仅 names）'
    out = {
        'schema_version': st.SCHEMA_VERSION, 'file': file,
        'classes': classes, 'defs': defs,
        'imports': imports, 'external': external,
        'importers': importers, 'importer_count': len(importers),
        'hub': is_hub, 'importers_indegree': n_imp,
        'facts': {'attr': attr, 'comp_register': comps,
                  'callbacks': callbacks},
        'coverage': cov,
        'elapsed': round(time.time() - t0, 3),
    }
    if note:
        out['note'] = note
    if json_out:
        return out
    lines = ['file-context %s%s' % (file, '（枢纽）' if is_hub else '')]
    if note:
        lines.append('  %s' % note)
    lines.append('  类 %d / 定义 %d / imports %d（外部 %d）/ importers %d（入度 %d）'
                 % (len(classes), len(defs), len(imports), len(external),
                    len(importers), n_imp))
    for d in defs[:20]:
        lines.append('    def %s' % ('%s.%s' % (d['class'], d['name'])
                                     if d['class'] else d['name']))
    if attr:
        lines.append('  属性事实 %d 条；组件注册 %d 条；约定回调 %d 条'
                     % (len(attr), len(comps), len(callbacks)))
    return '\n'.join(lines)


def context(store, cfg, compact=False, json_out=True):
    """一次性项目档案：统计 + 目录树 + 枢纽 + 外部依赖排行 + 覆盖率。"""
    t0 = time.time()
    n_files = store.count('files')
    parse_failed = store.parse_failed_count()
    built_at = store.get_meta('built_at')

    tree_out = st.tree(store, cfg, depth=2, json_out=True)
    dirs = tree_out['dirs']
    total_bytes = tree_out['total_bytes']

    idx = st.StructureIndex(store)
    indeg = st.hub_indegrees(idx)
    hubs_top = sorted(indeg.items(), key=lambda kv: -kv[1])
    n_hubs = len(hubs_top)
    hubs_top = hubs_top[:_COMPACT_HUBS if compact else 25]

    ext_count = {}
    for (_s, m) in idx.external:
        ext_count[m] = ext_count.get(m, 0) + 1
    ext_top = sorted(ext_count.items(), key=lambda kv: -kv[1])
    ext_top = ext_top[:_COMPACT_EXTERNAL if compact else 25]

    dirs = sorted(dirs, key=lambda d: -d['files'])
    dirs = dirs[:_COMPACT_TREE if compact else 40]

    cov = st._coverage(store, None, len(idx.edges), len(idx.external))
    out = {
        'schema_version': CONTEXT_SCHEMA_VERSION,
        'root': cfg.root, 'targets': cfg.targets,
        'built_at': int(built_at) if built_at else None,
        'stats': {'files': n_files, 'parse_failed': parse_failed,
                  'total_bytes': total_bytes,
                  'names': store.count('names'),
                  'defs': store.count('defs'),
                  'import_edges': len(idx.edges)},
        'top_dirs': dirs,
        'hubs': [{'file': f, 'importers': n} for f, n in hubs_top],
        'total_files_with_indegree': n_hubs,
        'external_top': [{'module': m, 'refs': n} for m, n in ext_top],
        'coverage': cov,
        'compact': compact,
        'elapsed': round(time.time() - t0, 3),
    }
    if json_out:
        return out
    lines = ['context %s: %d 文件 / %.1f MB / 建库时间戳 %s%s'
             % (cfg.root, n_files, total_bytes / 1048576, built_at,
                '（compact）' if compact else '')]
    lines.append('  目录 Top（按文件数）:')
    for d in dirs:
        lines.append('    %-60s %5d 文件' % (d['path'], d['files']))
    lines.append('  枢纽 Top%d:' % len(hubs_top))
    for f, n in hubs_top:
        lines.append('    %4d  %s' % (n, f))
    lines.append('  外部模块 Top%d:' % len(ext_top))
    for m, n in ext_top:
        lines.append('    %4d  %s' % (n, m))
    st._append_partial_hint(lines, cov)
    return '\n'.join(lines)
