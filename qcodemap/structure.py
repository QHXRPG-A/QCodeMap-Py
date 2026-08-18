# -*- coding: utf-8 -*-
"""结构查询四命令：deps / importers / hubs / tree（P2，codemap 平价能力）。

数据全部来自已建好的 files/imports 表，纯 SQL + 内存模块映射，无 ast-grep，
不受 codemap 的 30s 超时限制。JSON 输出带 schema_version 与 coverage
（resolved/unresolved 口径），对齐 codemap.analysis 风格便于消费方兼容。
"""

import os
import time

from qcodemap.scanner import module_of

SCHEMA_VERSION = 'qcodemap.analysis/v1'


class StructureIndex(object):
    """模块映射 + import 边解析的内存索引（单次构建约 1s，命令间复用）。"""

    def __init__(self, store):
        self.con = store.con
        self.modmap = {}
        for (rel,) in self.con.execute('SELECT path FROM files'):
            self.modmap[module_of(rel)] = rel
        self._edges = None  # [(src_file, dst_file)] 已解析边
        self._external = None  # [(src_file, module)] 未解析外部依赖

    def _build_edges(self):
        """imports 表 -> 文件级边。from M import N 先试 M.N（子模块）再试 M。"""
        edges = []
        external = []
        for (file, module, name, _alias) in self.con.execute(
                'SELECT file, module, name, alias FROM imports'):
            if not module:
                continue
            if name:
                rel = self.modmap.get('%s.%s' % (module, name)) \
                    or self.modmap.get(module)
            else:
                rel = self.modmap.get(module)
            if rel and rel != file:
                edges.append((file, rel))
            elif not rel:
                external.append((file, module))
        self._edges = edges
        self._external = external

    @property
    def edges(self):
        if self._edges is None:
            self._build_edges()
        return self._edges

    @property
    def external(self):
        if self._external is None:
            self._build_edges()
        return self._external

    def scope_files(self, target):
        """文件或目录（相对 root，posix）-> 库内文件清单。"""
        if target in self.modmap.values():
            return [target]
        prefix = target.rstrip('/') + '/'
        return [rel for rel in self.modmap.values() if rel.startswith(prefix)]


def _coverage(store, scope=None, resolved=0, unresolved=0):
    """覆盖率契约：scope 内 ast 失败文件数 >0 即 partial，附 issues 出处。

    scope=None 表示全库口径（hubs/tree）；否则限定在该文件集合内
    （deps/importers 的查询目标集）。
    """
    bad = store.parse_failed_files(scope)
    cov = {'status': 'partial' if bad else 'complete',
           'resolved': resolved, 'unresolved': unresolved,
           'parse_failed': len(bad)}
    if bad:
        cov['issues'] = bad[:10]
    return cov


def _append_partial_hint(lines, cov):
    """文本输出：coverage 为 partial 时追加一行索引残缺提示。"""
    if cov.get('status') == 'partial':
        lines.append('  [coverage=partial] %d 个文件 ast 解析失败（仅 names 索引）'
                     % cov['parse_failed'])


def deps(store, target, json_out=False):
    """target（文件/目录）依赖了谁：文件级边 + 外部模块清单。"""
    t0 = time.time()
    idx = StructureIndex(store)
    files = idx.scope_files(target)
    fset = set(files)
    out_edges = sorted({(s, d) for (s, d) in idx.edges if s in fset})
    ext = sorted({(s, m) for (s, m) in idx.external if s in fset})
    cov = _coverage(store, files, len(out_edges), len(ext))
    if json_out:
        return {
            'schema_version': SCHEMA_VERSION, 'target': target,
            'files': [{'file': s, 'imports': [d for (s2, d) in out_edges if s2 == s]}
                      for s in sorted(fset)],
            'external': sorted({m for (_s, m) in ext}),
            'coverage': cov,
            'elapsed': round(time.time() - t0, 3),
        }
    lines = ['deps %s: %d 文件, 内部边 %d, 外部模块 %d'
             % (target, len(files), len(out_edges), len(ext))]
    by_src = {}
    for (s, d) in out_edges:
        by_src.setdefault(s, []).append(d)
    for s in sorted(by_src):
        lines.append('  %s -> %d 个文件' % (s, len(by_src[s])))
    for m in sorted({m for (_s, m) in ext})[:15]:
        lines.append('  [external] %s' % m)
    _append_partial_hint(lines, cov)
    return '\n'.join(lines)


def importers(store, file, json_out=False):
    """谁 import 这个文件：反向边 + 枢纽判定。"""
    t0 = time.time()
    idx = StructureIndex(store)
    # 目标可能是文件或目录：目录按前缀聚合
    dst_set = set(idx.scope_files(file))
    if not dst_set:
        tip = {'schema_version': SCHEMA_VERSION, 'file': file, 'importers': [],
               'hub': False,
               'coverage': _coverage(store, None, 0, 0),
               'elapsed': 0.0,
               'note': '目标不在索引内'}
        return tip if json_out else '目标不在索引内: %s' % file
    rev = {}
    for (s, d) in idx.edges:
        if d in dst_set:
            rev.setdefault(d, set()).add(s)
    all_importers = set()
    for v in rev.values():
        all_importers |= v
    # 枢纽口径：入度 >= 全库 P95 视为枢纽（与 codemap 的 hub 判定目的一致）
    indeg = hub_indegrees(idx)
    ordered = sorted(indeg.values(), reverse=True)
    p95 = ordered[max(0, int(len(ordered) * 0.95) - 1)] if ordered else 0
    is_hub = len(all_importers) >= max(p95, 10)
    if json_out:
        return {
            'schema_version': SCHEMA_VERSION, 'file': file,
            'importers': sorted(all_importers), 'count': len(all_importers),
            'hub': is_hub,
            'coverage': _coverage(store, sorted(dst_set), len(all_importers), 0),
            'elapsed': round(time.time() - t0, 3),
        }
    lines = ['importers %s: %d 个文件引用%s'
             % (file, len(all_importers), '（枢纽）' if is_hub else '')]
    for s in sorted(all_importers):
        lines.append('  %s' % s)
    _append_partial_hint(lines, _coverage(store, sorted(dst_set),
                                          len(all_importers), 0))
    return '\n'.join(lines)


def hub_indegrees(idx):
    """文件 -> distinct 源文件入度。"""
    agg = {}
    for (s, d) in idx.edges:
        agg.setdefault(d, set()).add(s)
    return {d: len(v) for d, v in agg.items()}


def hubs(store, top=25, json_out=False):
    """import 入度排行（distinct 源文件数）。"""
    t0 = time.time()
    idx = StructureIndex(store)
    indeg = hub_indegrees(idx)
    ranked = sorted(indeg.items(), key=lambda kv: -kv[1])[:top]
    if json_out:
        return {
            'schema_version': SCHEMA_VERSION,
            'hubs': [{'file': d, 'importers': n} for d, n in ranked],
            'total_files_with_indegree': len(indeg),
            'coverage': _coverage(store, None, len(idx.edges), len(idx.external)),
            'elapsed': round(time.time() - t0, 3),
        }
    lines = ['hubs Top%d（全库 %d 文件有入度）:' % (top, len(indeg))]
    for d, n in ranked:
        lines.append('  %4d  %s' % (n, d))
    _append_partial_hint(lines, _coverage(store, None,
                                          len(idx.edges), len(idx.external)))
    return '\n'.join(lines)


def tree(store, cfg, depth=2, json_out=False):
    """目录树聚合（文件数 + 磁盘字节数），depth 限制目录层级。"""
    t0 = time.time()
    root = cfg.root
    dirs = {}
    total_files = 0
    total_bytes = 0
    for (rel,) in store.con.execute('SELECT path FROM files'):
        total_files += 1
        try:
            size = os.path.getsize(os.path.join(root, rel.replace('/', os.sep)))
        except OSError:
            size = 0
        total_bytes += size
        parts = rel.split('/')
        shown = parts[:depth + 1] if len(parts) > depth + 1 else parts[:-1]
        dkey = '/'.join(shown)
        agg = dirs.setdefault(dkey, {'files': 0, 'bytes': 0})
        agg['files'] += 1
        agg['bytes'] += size
    cov = _coverage(store, None, total_files, 0)
    if json_out:
        return {
            'schema_version': SCHEMA_VERSION, 'root': root, 'depth': depth,
            'dirs': [{'path': d, 'files': v['files'], 'bytes': v['bytes']}
                     for d, v in sorted(dirs.items())],
            'total_files': total_files, 'total_bytes': total_bytes,
            'coverage': cov,
            'elapsed': round(time.time() - t0, 3),
        }
    lines = ['tree depth=%d: %d 文件, %.1f MB'
             % (depth, total_files, total_bytes / 1048576)]
    for d in sorted(dirs):
        v = dirs[d]
        lines.append('  %-60s %5d 文件 %8.1f MB' % (d, v['files'], v['bytes'] / 1048576))
    _append_partial_hint(lines, cov)
    return '\n'.join(lines)
