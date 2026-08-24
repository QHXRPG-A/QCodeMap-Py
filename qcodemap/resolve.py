# -*- coding: utf-8 -*-
"""两阶段查询解析：阶段1 倒排查表拿候选，阶段2 纯 ast 语义验证。

输出分级（零误报优先）：
- VERIFIED：解析链路（MRO/组件/数据流/返回事实）落到目标定义；
- CANDIDATE：调用形态成立但解析不可达或同名歧义，只列位置不下结论。

全局对象不硬编码：凡在 global_assign 表登记过 (base, attr) 的
名字都视为运行时全局，类型查表还原。
"""

import ast
import json
import os
import re
import time
import warnings

from qcodemap.scanner import FUNC_NODES, dotted, module_of, read_source, scope_ranges
from qcodemap.store import Store  # noqa: F401 -- CLI 经本模块取 Store


class Resolver(object):

    def __init__(self, store, cfg):
        self.con = store.con
        self.store = store
        self.cfg = cfg
        self.root = cfg.root
        self.ret_seeds = dict(cfg.ret_seeds)
        self.attr_seeds = dict(cfg.attr_seeds)
        # 运行时全局：base -> {attr: class}
        self.global_types = {}
        for (base, attr, cls, _f) in self.con.execute(
                'SELECT base, attr, class, file FROM global_assign'):
            self.global_types.setdefault(base, {})[attr] = cls
        self.ret_facts = {}
        for (mod, func, t, _f) in self.con.execute('SELECT module, func, type, file FROM ret'):
            self.ret_facts[(mod, func)] = t
        self.attr_facts = {}
        for (_f, cls, attr, t) in self.con.execute('SELECT file, class, attr, type FROM attr'):
            self.attr_facts[(cls, attr)] = t
        self.comp_hosts = {}
        self.reverse_components = {}
        for (h, hf, c, cf) in self.con.execute(
                'SELECT host, host_file, comp, comp_file FROM comp'):
            self.comp_hosts.setdefault((h, hf), set()).add((c, cf))
            self.reverse_components.setdefault((c, cf), set()).add((h, hf))
        # 模块映射基于全部已索引文件：纯模块（无类）项目里 mod.func() 调用
        # 也要能解析；旧版仅取含类文件的映射在这种项目上整链路静默降级
        self.modmap = {module_of(f): f for (f,) in
                       self.con.execute('SELECT path FROM files')}
        self._tree_cache = {}
        self._imports_cache = {}  # (file,line) -> 可见 import 行，候选验证热路径去重 SQL
        # 语义查询缓存：mro_has_method 递归对同 (class,file) 重复查库是百万次级
        self._bases_cache = {}
        self._cfiles_cache = {}
        self._method_cache = {}
        self._runtime_hosts_cache = {}

    # ---- 基础查表 ----

    def class_bases(self, cls, cls_file):
        key = (cls, cls_file)
        if key not in self._bases_cache:
            row = self.con.execute('SELECT bases FROM classes WHERE name=? AND file=?',
                                   (cls, cls_file)).fetchone()
            self._bases_cache[key] = row[0].split(',') if row and row[0] else []
        return self._bases_cache[key]

    def _class_file(self, cls, from_file=None):
        """类名 -> 定义文件；同名多定义时按 from_file/同目录消歧，仍歧义返回 None。"""
        rows = self._class_files(cls, from_file)
        return rows[0] if len(rows) == 1 else None

    def _class_files(self, cls, from_file=None):
        """类名 -> 全部定义文件（有序：消歧命中的在前）。

        排序规则：同文件 > 同目录 > 同顶层 target（镜像目录同名类场景：
        调用方在哪个 target 就优先哪个定义）> 字典序。
        from_file 为 None 的调用结果可整体缓存（递归热路径百万次级）。
        """
        if from_file is None and cls in self._cfiles_cache:
            return self._cfiles_cache[cls]
        ckey = (cls, from_file)
        if from_file is not None and ckey in self._cfiles_cache:
            return self._cfiles_cache[ckey]
        rows = [r[0] for r in self.con.execute(
            'SELECT file FROM classes WHERE name=? ORDER BY file', (cls,)).fetchall()]
        if from_file and len(rows) > 1:
            def rank(rel):
                if rel == from_file:
                    return 0
                rd = rel.rsplit('/', 1)[0] if '/' in rel else ''
                fd = from_file.rsplit('/', 1)[0] if '/' in from_file else ''
                if rd == fd:
                    return 1
                if rel.split('/', 1)[0] == from_file.split('/', 1)[0]:
                    return 2
                return 3
            rows.sort(key=rank)
        self._cfiles_cache[ckey] = rows
        return rows

    def _ret_fact(self, key):
        # 人工种子优先（可覆盖自动事实），见 custom/seeds.py
        return self.ret_seeds.get(key) or self.ret_facts.get(key)

    def mro_has_method(self, cls, cls_file, method, seen=None):
        """沿 bases + 组件边（comp_raw）找方法定义，返回 (file, line) 或 None。

        同名多定义类（多文件组件 mixin 等）按并集语义查全部定义文件：
        组件注入是 setattr 拷贝，宿主方法来自每一个组件类。
        """
        seen = seen or set()
        if (cls, cls_file) in seen:
            return None
        seen.add((cls, cls_file))
        mkey = (cls, cls_file, method)
        if mkey not in self._method_cache:
            self._method_cache[mkey] = self.con.execute(
                'SELECT file,line FROM defs WHERE class=? AND name=? AND file=?',
                (cls, method, cls_file)).fetchone()
        row = self._method_cache[mkey]
        if row:
            return row
        for b in self.class_bases(cls, cls_file):
            for bf in self._class_files(b, cls_file):
                hit = self.mro_has_method(b, bf, method, seen)
                if hit:
                    return hit
        for comp, cf in self.comp_hosts.get((cls, cls_file), ()):
            hit = self.mro_has_method(comp, cf, method, seen)
            if hit:
                return hit
        return None

    # ---- 解析主入口 ----

    def _file_index(self, file):
        """单文件预分析（缓存）：行 -> 调用节点 / 外层函数 / 外层类。

        callers 一个函数常有数百候选行，逐候选 ast.walk 全树是分钟级根因；
        预分析一次建行索引后，候选按行直取。
        """
        if not hasattr(self, '_fidx_cache'):
            self._fidx_cache = {}
        idx = self._fidx_cache.get(file)
        if idx is not None:
            return idx
        tree = self._parse(file)
        idx = {'tree': tree, 'calls': {}, 'funcs': [], 'classes': [],
               'assigns': {}, 'scopes': []}
        if tree is not None:
            idx['scopes'] = scope_ranges(tree)
            # 类/函数区间：按 (start, end) 收集，查询时取包含行的最内层
            for n in ast.walk(tree):
                if isinstance(n, ast.ClassDef):
                    idx['classes'].append((n.lineno, n.end_lineno or n.lineno, n.name))
                elif isinstance(n, FUNC_NODES):
                    idx['funcs'].append((n.lineno, n.end_lineno or n.lineno, n.name))
                elif isinstance(n, ast.Assign):
                    for t in n.targets:
                        if isinstance(t, ast.Name):
                            idx['assigns'].setdefault(t.id, []).append(n)
            for n in ast.walk(tree):
                if isinstance(n, ast.Call):
                    idx['calls'].setdefault(n.lineno, []).append(n)
        self._fidx_cache[file] = idx
        return idx

    def _enclosing_of(self, file, line):
        """行 -> (最内层类名, 最内层函数名)；不在任何函数内返回 (None, None)。"""
        idx = self._file_index(file)
        cls = func = None
        for (a, b, name) in idx['classes']:
            if a <= line <= b and (cls is None or a > cls[0]):
                cls = (a, b, name)
        for (a, b, name) in idx['funcs']:
            if a <= line <= b and (func is None or a > func[0]):
                func = (a, b, name)
        return (cls[2] if cls else None, func[2] if func else None)

    def resolve_call(self, file, line, name):
        """候选调用位置 -> 定义 (file, line) 或 None（None = 不下结论）。

        性能：调用节点与外层上下文走 _file_index 行索引，不做全树 walk。
        """
        idx = self._file_index(file)
        if idx['tree'] is None:
            return None
        node = None
        for c in idx['calls'].get(line, ()):
            if isinstance(c.func, ast.Attribute) and c.func.attr == name:
                node = c
                break
            if isinstance(c.func, ast.Name) and c.func.id == name:
                node = c
                break
        if node is None:
            return None
        if isinstance(node.func, ast.Attribute):
            recv = node.func.value
            if isinstance(recv, ast.Name):
                if recv.id == 'self':
                    cls = _enclosing_class_from(idx, line)
                    if cls:
                        for cf in self._class_files(cls, file):
                            hit = self.mro_has_method(cls, cf, name)
                            if hit:
                                return hit
                else:
                    typ = self._local_var_type(idx['tree'], line, recv.id, file,
                                               assigns=idx['assigns'])
                    if typ:
                        return self._method_on_type(typ, name, file)
                    # 模块级调用 mod.Func()：recv 是 import 进来的模块名
                    mf = self._module_file_of(file, recv.id, line)
                    if mf:
                        rows = self.con.execute(
                            'SELECT file,line FROM defs WHERE file=? AND name=? '
                            'AND class IS NULL', (mf, name)).fetchall()
                        if len(rows) == 1:
                            return rows[0]
            return None
        if isinstance(node.func, ast.Name):
            rows = self.con.execute(
                'SELECT file,line FROM defs WHERE name=? AND class IS NULL', (name,)).fetchall()
            if len(rows) == 1:
                return rows[0]
            own = [r for r in rows if r[0] == file]
            if len(own) == 1:
                return own[0]
            return None
        return None

    def _imports_of(self, file, line=None):
        """调用点可见的 import 行；模块域 + 外到内函数域，局部绑定覆盖外层。

        同一作用域把同一个名字导向多个目标时剔除该绑定，避免静态分支导致
        错误 VERIFIED。类体 import 不作为方法的裸名词法环境。
        """
        cache_key = (file, line)
        cached = self._imports_cache.get(cache_key)
        if cached is not None:
            return cached
        rows = self.con.execute(
            'SELECT module, name, alias, scope FROM imports WHERE file=? '
            'ORDER BY line, rowid', (file,)).fetchall()
        scopes = ['']
        if line is not None:
            funcs = [(a, b, key) for (a, b, key, kind)
                     in self._file_index(file)['scopes']
                     if kind == 'func' and a <= line <= b]
            funcs.sort(key=lambda x: (x[0], -x[1]))
            scopes.extend(key for (_a, _b, key) in funcs)
        visible = {}
        for scope in scopes:
            local = {}
            for m, n, a, row_scope in rows:
                if row_scope != scope:
                    continue
                bound = a or n or (m.split('.', 1)[0] if m else None)
                if not bound:
                    continue
                row = (m, n, a)
                old = local.get(bound)
                if bound not in local or old == row:
                    local[bound] = row
                else:
                    local[bound] = None
            visible.update(local)
        out = [row for row in visible.values() if row is not None]
        self._imports_cache[cache_key] = out
        return out

    def _module_file_of(self, file, name, line=None):
        """调用点里 name 是否指向已索引模块，是则返回其文件。"""
        for (m, n, a) in self._imports_of(file, line):
            if n == name or a == name:
                if n:
                    for cand in ('%s.%s' % (m, n), m):
                        hit = self.modmap.get(cand)
                        if hit:
                            return hit
                elif m == name:
                    return self.modmap.get(m)
                return None
            if m == name or m.split('.')[-1] == name:
                return self.modmap.get(m)
        return None

    def _parse(self, file):
        if file not in self._tree_cache:
            try:
                # 目标库老代码有非法转义序列，压掉 SyntaxWarning
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore', SyntaxWarning)
                    self._tree_cache[file] = ast.parse(self._read(file))
            except (SyntaxError, OSError):
                self._tree_cache[file] = None
        return self._tree_cache[file]

    def _read(self, rel):
        return read_source(os.path.join(self.root, rel.replace('/', os.sep)))

    def _lines(self, rel):
        if not hasattr(self, '_line_cache'):
            self._line_cache = {}
        if rel not in self._line_cache:
            self._line_cache[rel] = self._read(rel).splitlines()
        return self._line_cache[rel]

    # ---- 局部变量数据流 ----

    def _local_var_type(self, tree, line, var, file=None, depth=0, assigns=None):
        """赋值追踪（够用版）：var = <表达式> 在 line 之前最近一次的可解类型。

        depth 限制二级推导递归（x = x.f() 等自引用形态防无限递归）。
        assigns 是文件级预索引 {var: [Assign 节点]}，避免逐候选全树 walk。
        """
        if depth > 6:
            return None
        if assigns is None and file:
            assigns = self._file_index(file)['assigns']
        nodes = assigns.get(var, ()) if assigns is not None else ()
        if assigns is None:
            return self._local_var_type_scan(tree, line, var, file, depth)
        best = None
        for n in nodes:
            if n.lineno > line:
                continue
            typ = self._value_type(n.value, n, line, tree, file, depth)
            if typ:
                best = typ  # 行序靠后的赋值覆盖（最近一次可解）
        return best

    def _local_var_type_scan(self, tree, line, var, file, depth):
        """无预索引时的兜底全树扫描（tests 等直接传 tree 的路径）。"""
        best = None
        for n in ast.walk(tree):
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Name) and t.id == var:
                        best = self._value_type(n.value, n, line, tree, file, depth)
                        if n.lineno <= line and best:
                            return best
        return best

    def _value_type(self, v, n, line, tree, file, depth):
        """单个赋值右值的类型推导（局部变量/返回事实/全局属性三路）。"""
        best = None
        if isinstance(v, ast.Call):
            if isinstance(v.func, ast.Name):
                if v.func.id in ('str', 'int', 'float', 'bool',
                                 'dict', 'list', 'tuple', 'set'):
                    best = None
                else:
                    # 裸名：先按 import 语境查返回事实，查不到视为构造
                    rt = self._ret_lookup(file, v.func.id, n.lineno) if file else None
                    best = rt or v.func.id
            else:
                d = dotted(v.func)
                best = None
                if d and file:
                    best = self._ret_lookup(file, d, n.lineno)
                    if not best and '.' in d:
                        # head 是局部变量：先解其类型再查 (类型, 方法) 返回事实
                        head, func = d.rsplit('.', 1)
                        if head.isidentifier():
                            ht = self._local_var_type(tree, n.lineno, head,
                                                      file, depth + 1)
                            if ht:
                                best = self._ret_fact((ht, func))
        elif isinstance(v, ast.Attribute) and isinstance(v.value, ast.Name):
            base = v.value.id
            if base in self.global_types:
                best = self.global_types[base].get(v.attr)
            elif base == 'self':
                cls = _enclosing_class_from(self._file_index(file), line) \
                    if file else (None)
                cls = cls or ''
                best = self.attr_seeds.get((cls, v.attr)) \
                    or self.attr_facts.get((cls, v.attr))
                if best and '.' in best:
                    best = self._resolve_global_pseudo(best) or best
        return best

    def _resolve_global_pseudo(self, typ):
        """'gw.X' 形全局伪类型 -> global_assign 还原的真实类名。"""
        base, attr = typ.split('.', 1)
        return self.global_types.get(base, {}).get(attr)

    def _ret_lookup(self, file, dotted_call, line=None):
        """module_or_var.Func(...) -> 返回类型（含种子）；import 别名先归一。"""
        if '.' not in dotted_call:
            head, func = dotted_call, ''
        else:
            head, func = dotted_call.rsplit('.', 1)
        for (m, n, a) in self._imports_of(file, line):
            if n == head or a == head:
                # from X import Y：Y 可能是子模块（查 X.Y.Func）或对象/类
                candidates = ['%s.%s' % (m, n) if n else m, m]
                for mod in candidates:
                    hit = self._ret_fact((mod, func))
                    if hit:
                        return hit
                cls_row = self.con.execute(
                    'SELECT 1 FROM classes WHERE name=? AND file=?', (head, file)).fetchone()
                if not cls_row and n == head:
                    glob = self.ret_facts.get((m, func))
                    if glob:
                        return glob
        return self.ret_seeds.get((head, func)) if func else None

    def _method_on_type(self, typ, name, from_file=None):
        for cf in self._class_files(typ, from_file):
            hit = self.mro_has_method(typ, cf, name)
            if hit:
                return hit
        return None

    # ---- 钩子约定回调 ----

    def _reverse_bases(self):
        if hasattr(self, '_reverse_bases_cache'):
            return self._reverse_bases_cache
        out = {}
        rows = self.con.execute(
            'SELECT file, name, bases FROM classes').fetchall()
        for child_file, child, bases in rows:
            for base in (bases.split(',') if bases else ()):
                cands = self._class_files(base, child_file)
                # 同名基类有歧义时仅接受同文件定义；宁可漏边，不跨镜像误连。
                if len(cands) == 1:
                    out.setdefault((base, cands[0]), set()).add((child, child_file))
                elif child_file in cands:
                    out.setdefault((base, child_file), set()).add((child, child_file))
        self._reverse_bases_cache = out
        return out

    def runtime_hosts(self, cls, cls_file):
        """类/组件在运行时可能落入的精确宿主集合（同类、继承、组件注入）。"""
        key = (cls, cls_file)
        cached = self._runtime_hosts_cache.get(key)
        if cached is not None:
            return cached
        reverse_bases = self._reverse_bases()
        seen = {key}
        queue = [key]
        while queue:
            item = queue.pop(0)
            next_items = set(self.reverse_components.get(item, ()))
            next_items.update(reverse_bases.get(item, ()))
            for nxt in next_items:
                if not _file_mtime_ok(self.store, self.cfg, nxt[1]):
                    continue
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        self._runtime_hosts_cache[key] = seen
        return seen

    def convention_sources(self, def_file, def_line, def_cls, func):
        """目标方法的严格约定调用源；只有运行时宿主相交才成边。"""
        if not def_cls:
            return []
        target_hosts = self.runtime_hosts(def_cls, def_file)
        out = []
        rows = self.con.execute(
            'SELECT file,line,class,kind,source,target FROM callback_raw '
            'WHERE target=? ORDER BY file,line', (func,)).fetchall()
        for sf, sln, scls, kind, source, target in rows:
            if not _file_mtime_ok(self.store, self.cfg, sf):
                continue
            hosts = sorted(target_hosts & self.runtime_hosts(scls, sf))
            if not hosts:
                continue
            out.append({
                'level': '%s-INFERRED' % kind.upper(),
                'symbol': _display(def_cls, func),
                'file': sf,
                'line': sln,
                'caller': '%s %s.%s' % (kind, scls, source),
                'via_callback': {'kind': kind, 'source': source,
                                 'target': target,
                                 'hosts': [{'class': h, 'file': hf}
                                           for h, hf in hosts]},
            })
        return out

    def convention_targets(self, raw):
        """单条 callback_raw -> 严格匹配的目标方法定义。"""
        sf, sln, scls, kind, source, target = raw
        if not _file_mtime_ok(self.store, self.cfg, sf):
            return []
        source_hosts = self.runtime_hosts(scls, sf)
        out = []
        rows = self.con.execute(
            'SELECT file,line,class FROM defs WHERE name=? ORDER BY file,line',
            (target,)).fetchall()
        for tf, tln, tcls in rows:
            if not tcls:
                continue
            if not _file_mtime_ok(self.store, self.cfg, tf):
                continue
            hosts = sorted(source_hosts & self.runtime_hosts(tcls, tf))
            if not hosts:
                continue
            out.append({
                'kind': kind, 'source': source, 'source_class': scls,
                'source_file': sf, 'source_line': sln, 'target': target,
                'target_class': tcls, 'target_file': tf, 'target_line': tln,
                'hosts': [{'class': h, 'file': hf} for h, hf in hosts],
            })
        return out


# ---- 模块级工具 ----

def _enclosing_class_from(idx, line):
    """行索引版：包含 line 的最内层类名（须同时落在其某方法体内）。"""
    best = None
    for (a, b, name) in idx['classes']:
        if a <= line <= b and (best is None or a > best[0]):
            if any(fa <= line <= fb for (fa, fb, _fn) in idx['funcs']
                   if a <= fa and fb <= b):
                best = (a, b, name)
    return best[2] if best else None


def _find_call_at(tree, line, name):
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            if isinstance(n.func, ast.Attribute) and n.func.attr == name \
                    and n.lineno == line:
                return n
            if isinstance(n.func, ast.Name) and n.func.id == name and n.lineno == line:
                return n
    return None


def _enclosing_class(tree, line):
    """包含 line 的最内层类名（要求 line 落在类的方法体内）。"""
    best = None
    for n in ast.walk(tree):
        if isinstance(n, ast.ClassDef) and n.lineno <= line <= (n.end_lineno or n.lineno):
            for sub in ast.walk(n):
                if isinstance(sub, FUNC_NODES) and sub.lineno <= line <= (sub.end_lineno or sub.lineno):
                    if best is None or n.lineno > best.lineno:
                        best = n
                    break
    return best.name if best else None


def _enclosing_function(tree, line):
    """包含 line 的最内层函数 -> (类名或 None, 函数名)。"""
    fn = None
    for n in ast.walk(tree):
        if isinstance(n, FUNC_NODES) and n.lineno <= line <= (n.end_lineno or n.lineno):
            if fn is None or n.lineno > fn.lineno:
                fn = n
    if fn is None:
        return (None, None)
    cls = None
    for n in ast.walk(tree):
        if isinstance(n, ast.ClassDef) and n.lineno <= fn.lineno <= (n.end_lineno or n.lineno):
            if cls is None or n.lineno > cls.lineno:
                cls = n
    return (cls.name if cls else None, fn.name)


def _display(cls, func):
    return '%s.%s' % (cls, func) if cls else (func or '?')


# ---- 查询 API ----

# 语义解析行为版本：消歧规则等改动时 +1，旧缓存边整体失效
# v2: 同名类并集语义 + 模块级调用解析；v3: 局部变量二级推导加递归深度防线；
# v4: 行索引化 resolve_call（语义等价，性能重构）；
# v5: modmap 基于全部已索引文件（原仅含类文件）——纯模块项目的
#     mod.func() 调用从静默降级恢复为可验证；
# v6: import 按词法作用域解析 + 钩子约定回调严格宿主连边；
# v7: 约定回调边缓存纳入共同宿主文件 mtime；
# v8: receiver/handler 事实并入解析；v9: receiver 经 MRO/组件归并目标
RESOLVER_VERSION = 9


# 内置函数/常见运行时名：callees 收集时的噪音过滤
_BUILTIN_NAMES = frozenset((
    'bool', 'int', 'float', 'str', 'bytes', 'list', 'dict', 'tuple', 'set',
    'len', 'range', 'enumerate', 'isinstance', 'issubclass', 'getattr',
    'setattr', 'hasattr', 'print', 'sorted', 'min', 'max', 'sum', 'abs',
    'any', 'all', 'map', 'filter', 'zip', 'open', 'super', 'type', 'repr',
    'format', 'round', 'id', 'hash', 'iter', 'next', 'divmod', 'pow',
))


def _result(query, items, t0, cached=False, note='', store=None):
    out = {
        'query': query, 'items': items, 'cached': cached, 'note': note,
        'n_verified': sum(1 for i in items if i['level'] == 'VERIFIED'),
        'n_candidate': sum(1 for i in items if i['level'] == 'CANDIDATE'),
        'n_inferred': sum(1 for i in items if i['level'].endswith('-INFERRED')),
        'elapsed': round(time.time() - t0, 3),
    }
    if store is not None:
        n_bad = store.parse_failed_count()
        out['coverage'] = {'status': 'partial' if n_bad else 'complete',
                           'parse_failed': n_bad}
    return out


def _file_mtime_ok(store, cfg, rel, snapshot=None):
    """索引里的 mtime 与磁盘一致才算未变更；snapshot 给出则还要等于快照值。"""
    row = store.con.execute('SELECT mtime FROM files WHERE path=?', (rel,)).fetchone()
    if row is None:
        return False
    try:
        disk = os.path.getmtime(os.path.join(cfg.root, rel.replace('/', os.sep)))
    except OSError:
        return False
    return disk == row[0] and (snapshot is None or snapshot == row[0])


def _load_edges(store, cfg, name, def_file, def_line, kind):
    """读缓存边；payload 记录解析器版本与各源文件的 mtime 快照，任一不符即失效。"""
    row = store.con.execute(
        'SELECT payload FROM edges WHERE name=? AND def_file=? AND def_line=? AND kind=?',
        (name, def_file, def_line, kind)).fetchone()
    if row is None:
        return None
    data = json.loads(row[0])
    if data.get('resolver_version') != RESOLVER_VERSION:
        return None
    for rel, mt in data['mtimes'].items():
        if not _file_mtime_ok(store, cfg, rel, mt):
            return None
    return data['items']


def _save_edges(store, name, def_file, def_line, kind, items, ref_files):
    # CLI/MCP 查询连接严格只读；边缓存只是加速项，不能反向制造写锁。
    if getattr(store, 'read_only', False):
        return
    mtimes = {}
    for rel in ref_files:
        row = store.con.execute('SELECT mtime FROM files WHERE path=?', (rel,)).fetchone()
        if row:
            mtimes[rel] = row[0]
    store.con.execute(
        'INSERT OR REPLACE INTO edges VALUES(?,?,?,?,?)',
        (name, def_file, def_line, kind,
         json.dumps({'resolver_version': RESOLVER_VERSION, 'mtimes': mtimes,
                     'items': items}, ensure_ascii=False)))
    store.commit()


def _find_defs(store, file, func):
    return store.con.execute(
        'SELECT file, line, class FROM defs WHERE file=? AND name=? ORDER BY line',
        (file, func)).fetchall()


def _is_call_form(resolver, rel, line, name):
    """names 候选行是否呈调用形态（token 后最近字符为 '('）。"""
    try:
        text = resolver._lines(rel)[line - 1]
        idx = text.index(name)
    except (IndexError, ValueError):
        return False
    after = text[idx + len(name):].lstrip()[:1]
    return after == '('


def _receiver_fact(resolver, file, line, func):
    expr = None
    for call in resolver._file_index(file)['calls'].get(line, ()):
        if isinstance(call.func, ast.Attribute) and call.func.attr == func:
            expr = dotted(call.func.value)
            break
    if not expr:
        return []
    return resolver.con.execute(
        'SELECT type,confidence,reason FROM receiver_fact '
        'WHERE file=? AND line=? AND expr=? ORDER BY type',
        (file, line, expr)).fetchall()


def _receiver_fact_targets(resolver, facts, from_file, func, direct_target=None,
                           direct_class=None):
    """receiver 类型事实 -> 方法定义；继承和组件注入统一走 mro_has_method。"""
    out = []
    for fact in facts:
        typ = fact[0]
        hits = set()
        if direct_target is not None and typ == direct_class:
            hits.add(direct_target)
        for cls_file in resolver._class_files(typ, from_file):
            hit = resolver.mro_has_method(typ, cls_file, func)
            if hit:
                hits.add(hit)
        for hit in sorted(hits):
            out.append((hit, fact))
    return out


def callers(store, cfg, file, func, resolver=None, receiver_class=None):
    """谁调用 <file> 的 <func>：候选 -> 调用形态过滤 -> 语义验证 -> 分级输出。

    resolver 可注入复用（blast 闭包逐函数调用时避免重复全表初始化）。
    """
    t0 = time.time()
    defs = _find_defs(store, file, func)
    if receiver_class:
        defs = [row for row in defs if row[2] == receiver_class]
    if not defs:
        note = '定义未找到（文件未索引或函数不存在）'
        row = store.con.execute(
            'SELECT parse_ok FROM files WHERE path=?',
            (file.replace('\\', '/'),)).fetchone()
        if row is not None and not row[0]:
            note = '定义未找到：目标文件 ast 解析失败，索引仅 names'
        return _result({'file': file, 'func': func}, [], t0, note=note, store=store)
    def_file, def_line, def_cls = defs[0]
    note = ('同名定义 %d 处，取首处' % len(defs)) if len(defs) > 1 else ''
    cache_kind = 'callers:%s' % (receiver_class or '')
    cached = _load_edges(store, cfg, func, def_file, def_line, cache_kind)
    if cached is not None:
        return _result({'file': file, 'func': func}, cached, t0, cached=True,
                       note=note, store=store)
    r = resolver or Resolver(store, cfg)
    items = []
    bad_files = set(store.parse_failed_files())
    cands = store.con.execute(
        'SELECT f.path, n.line FROM names n JOIN files f ON n.file=f.id '
        'WHERE n.name=? ORDER BY f.path, n.line', (func,)).fetchall()
    all_def_sites = set(store.con.execute(
        'SELECT file,line FROM defs WHERE name=?', (func,)).fetchall())
    seen = set()
    for (f, ln) in cands:
        if (f, ln) in all_def_sites or (f, ln) in seen:
            continue
        if not _is_call_form(r, f, ln, func):
            continue
        seen.add((f, ln))
        cls, fname = r._enclosing_of(f, ln)
        got = r.resolve_call(f, ln, func)
        receiver_facts = _receiver_fact(r, f, ln, func)
        wanted_class = receiver_class or def_cls
        if receiver_facts:
            target = (def_file, def_line)
            matching = [fact for hit, fact in _receiver_fact_targets(
                r, receiver_facts, f, func,
                direct_target=target, direct_class=wanted_class)
                if hit == target]
            if not matching:
                continue
            if got != (def_file, def_line):
                typ, _confidence, reason = matching[0]
                items.append({
                    'level': 'FRAMEWORK-INFERRED',
                    'symbol': _display(typ, func), 'file': f, 'line': ln,
                    'caller': _display(cls, fname), 'note': reason,
                    'receiver_class': typ,
                })
                continue
        if got == (def_file, def_line):
            items.append({'level': 'VERIFIED', 'symbol': _display(def_cls, func),
                          'file': f, 'line': ln, 'caller': _display(cls, fname)})
        elif got is not None:
            other = store.con.execute(
                'SELECT class FROM defs WHERE file=? AND line=?', got).fetchone()
            items.append({'level': 'CANDIDATE', 'symbol': _display(def_cls, func),
                          'file': f, 'line': ln, 'caller': _display(cls, fname),
                          'note': '解析到同名另一定义 %s:%d' % got})
        elif f in bad_files:
            items.append({'level': 'CANDIDATE', 'symbol': _display(def_cls, func),
                          'file': f, 'line': ln, 'caller': _display(cls, fname),
                          'note': '所在文件 ast 解析失败，无法验证（索引仅 names）'})
        else:
            items.append({'level': 'CANDIDATE', 'symbol': _display(def_cls, func),
                          'file': f, 'line': ln, 'caller': _display(cls, fname),
                          'note': '语义验证不可达（同名候选）'})
    items.extend(r.convention_sources(def_file, def_line, def_cls, func))
    items.sort(key=lambda i: (i['level'], i['file'], i['line']))
    ref_files = set(i['file'] for i in items) | {def_file}
    for item in items:
        for host in item.get('via_callback', {}).get('hosts', ()):
            ref_files.add(host['file'])
    _save_edges(store, func, def_file, def_line, cache_kind, items, ref_files)
    return _result({'file': file, 'func': func,
                    'receiver_class': receiver_class}, items, t0,
                   note=note, store=store)


def callees(store, cfg, file, func):
    """<file> 的 <func> 调了谁：定位函数体 -> 收集调用点 -> 逐个反向解析。"""
    t0 = time.time()
    file = file.replace('\\', '/')
    defs = _find_defs(store, file, func)
    if not defs:
        note = '定义未找到（文件未索引或函数不存在）'
        row = store.con.execute(
            'SELECT parse_ok FROM files WHERE path=?', (file,)).fetchone()
        if row is not None and not row[0]:
            note = '定义未找到：目标文件 ast 解析失败，索引仅 names'
        return _result({'file': file, 'func': func}, [], t0, note=note, store=store)
    def_file, def_line, def_cls = defs[0]
    r = Resolver(store, cfg)
    tree = r._parse(def_file)
    items = []
    if tree is not None:
        fn_node = None
        for n in ast.walk(tree):
            if isinstance(n, FUNC_NODES) and n.name == func and n.lineno == def_line:
                fn_node = n
                break
        if fn_node is not None:
            got_defs, seen_sites = set(), set()
            for call in ast.walk(fn_node):
                if not isinstance(call, ast.Call):
                    continue
                if isinstance(call.func, ast.Attribute):
                    cname = call.func.attr
                elif isinstance(call.func, ast.Name):
                    cname = call.func.id
                else:
                    continue
                if cname in _BUILTIN_NAMES:
                    continue
                site = (call.lineno, cname)
                if site in seen_sites:
                    continue
                seen_sites.add(site)
                got = r.resolve_call(def_file, call.lineno, cname)
                if got is None:
                    facts = _receiver_fact(r, def_file, call.lineno, cname)
                    targets = {}
                    for inferred, fact in _receiver_fact_targets(
                            r, facts, def_file, cname):
                        targets.setdefault(inferred, []).append(fact)
                    inferred = next(iter(targets)) if len(targets) == 1 else None
                    if inferred:
                        typ, _confidence, reason = targets[inferred][0]
                        if inferred in got_defs:
                            continue
                        got_defs.add(inferred)
                        items.append({
                            'level': 'FRAMEWORK-INFERRED',
                            'symbol': _display(typ, cname),
                            'file': inferred[0], 'line': inferred[1],
                            'caller': '%s:%d' % (def_file, call.lineno),
                            'note': reason, 'receiver_class': typ,
                        })
                        continue
                    items.append({'level': 'CANDIDATE', 'symbol': cname,
                                  'file': def_file, 'line': call.lineno,
                                  'caller': '%s:%d' % (def_file, call.lineno),
                                  'note': '未解析到定义'})
                    continue
                if got in got_defs:
                    continue
                got_defs.add(got)
                row = store.con.execute(
                    'SELECT class FROM defs WHERE file=? AND line=?', got).fetchone()
                items.append({'level': 'VERIFIED', 'symbol': _display(row[0] if row else None,
                                                                     cname),
                              'file': got[0], 'line': got[1],
                              'caller': '%s:%d' % (def_file, call.lineno)})
    note = ('同名定义 %d 处，取首处' % len(defs)) if len(defs) > 1 else ''
    items.sort(key=lambda i: (i['level'], i['symbol']))
    return _result({'file': file, 'func': func}, items, t0, note=note, store=store)


def usages(store, cfg, symbol, limit=200):
    """标识符全仓出现点（倒排原始行，不做逐行语义验证）+ 定义点摘要。"""
    t0 = time.time()
    if not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', symbol):
        return _result(
            {'symbol': symbol}, [], t0,
            note='usages 只接受单个 Python 标识符；查类定义请用 defs X，'
                 '搜文本请用 rg', store=store)
    rows = store.con.execute(
        'SELECT f.path, n.line, n.col FROM names n JOIN files f ON n.file=f.id '
        'WHERE n.name=? ORDER BY f.path, n.line LIMIT ?', (symbol, limit + 1)).fetchall()
    has_more = len(rows) > limit
    items = [{'level': 'OCCUR', 'symbol': symbol, 'file': f, 'line': ln, 'col': col}
             for (f, ln, col) in rows[:limit]]
    defs = store.con.execute(
        'SELECT file, line, class FROM defs WHERE name=? ORDER BY file LIMIT 20',
        (symbol,)).fetchall()
    note = '定义 %d 处: %s%s' % (len(defs),
                                ', '.join('%s:%d' % (d[0], d[1]) for d in defs[:5]),
                                ' …' if len(defs) > 5 else '')
    if has_more:
        note += '；出现点超 %d 截断' % limit
    return _result({'symbol': symbol}, items, t0, note=note, store=store)


def defs(store, symbol, limit=200):
    """精确定义查询；不混入普通 identifier 出现点。"""
    t0 = time.time()
    if not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', symbol):
        return _result({'symbol': symbol}, [], t0,
                       note='defs 只接受单个 Python 标识符', store=store)
    rows = store.con.execute(
        'SELECT file,line,class FROM defs WHERE name=? ORDER BY file,line LIMIT ?',
        (symbol, limit)).fetchall()
    items = [
        {'level': 'DEFINITION', 'symbol': _display(cls, symbol),
         'file': file, 'line': line, 'caller': _display(cls, symbol)}
        for file, line, cls in rows]
    return _result({'symbol': symbol}, items, t0, store=store)
