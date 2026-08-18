# -*- coding: utf-8 -*-
"""单文件 ast 扫描：names 倒排 + defs/classes/imports + 事实原始行。

通用事实（项目无关）：self.X = 构造() -> attr；return 构造() -> ret。
框架事实（Property 声明、@Components、genv 注入等）：经 hooks 产出，
本模块只负责按协议调用与登记，不认识任何框架名字。
"""

import ast
import re
import warnings

from qcodemap.hooks import FactContext, FactsHooks

TOKEN_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')
# bytes 字面量行（如 data = bindict.bindict(b'\\xe4..')，表格二进制产物）：
# 转义序列会匹配出海量伪 token，token 化前整段剔除
_BYTES_LINE = re.compile(r"b'[^']*'|b\"[^\"]*\"")
BUILTIN_CTORS = ('str', 'int', 'float', 'bool', 'dict', 'list', 'tuple', 'set')
FUNC_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)


def read_source(path):
    """utf-8 优先、gbk 兜底的解码链（老文件混编码）。"""
    data = path.read_bytes() if hasattr(path, 'read_bytes') else open(str(path), 'rb').read()
    for enc in ('utf-8', 'gbk'):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode('utf-8', errors='replace')


def module_of(rel):
    """相对路径 -> 点分模块名（__init__.py 归一到包名）。"""
    parts = rel[:-3].split('/') if rel.endswith('.py') else rel.split('/')
    if parts and parts[-1] == '__init__':
        parts = parts[:-1]
    return '.'.join(parts)


def dotted(node):
    """Name/Attribute 链 -> 'a.b.c'；其余返回 None。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted(node.value)
        return '%s.%s' % (base, node.attr) if base else None
    return None


def scan_file(rel, path, hooks=None, downsample=False):
    """扫描单文件，返回 {表名: [完整行, ...]}；names 行为 (name, line, col)。

    ast 解析失败（老语法/语法错误）时仅保留 names 倒排，事实降级为空，并记
    parse_ok=False 供查询侧透出「索引残缺」——倒排查候选不受影响，语义验证
    自然回落 CANDIDATE。
    downsample=True（表格产物目录）：剔除 bytes 字面量后，每个标识符只记
    每文件首处——表格同一 key 重复上万行，usages 只需知道"出现在这张表"。
    """
    if hooks is None:
        hooks = FactsHooks()
    text = read_source(path)
    r = {'names': [], 'defs': [], 'classes': [], 'imports': [],
         'attr': [], 'global_assign': [], 'ret': [], 'comp_raw': [],
         'rpc': [], 'parse_ok': True}
    seen_names = set() if downsample else None
    for i, line in enumerate(text.splitlines(), 1):
        # 剔除 bytes 字面量后再 token 化（bindict 表格产物的伪 token 源）
        line = _BYTES_LINE.sub(' ', line)
        for m in TOKEN_RE.finditer(line):
            tok = m.group(0)
            if seen_names is not None:
                if tok in seen_names:
                    continue
                seen_names.add(tok)
            r['names'].append((tok, i, m.start() + 1))
    try:
        # 目标库老代码有非法转义序列，ast 会告警但不影响解析，压掉保持输出干净
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', SyntaxWarning)
            tree = ast.parse(text)
    except SyntaxError:
        r['parse_ok'] = False
        return r
    mod = module_of(rel)
    for stmt in _module_stmts(tree):
        if isinstance(stmt, ast.ClassDef):
            _scan_class(stmt, r, rel, mod, hooks)
        elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
            _scan_import(stmt, r, rel, mod)
        elif isinstance(stmt, FUNC_NODES):
            r['defs'].append((rel, stmt.lineno, None, stmt.name))
            _scan_function(stmt, r, rel, mod, None, hooks)
    return r


def _module_stmts(tree):
    """模块级语句展开：进入 if/try/for/while/with 的语句体，不进入函数与类。"""
    stack = list(tree.body)
    while stack:
        st = stack.pop(0)
        yield st
        if isinstance(st, ast.If):
            stack = st.body + st.orelse + stack
        elif isinstance(st, ast.Try):
            handler_bodies = [s for h in st.handlers for s in h.body]
            stack = st.body + st.orelse + st.finalbody + handler_bodies + stack
        elif isinstance(st, (ast.For, ast.While)):
            stack = st.body + st.orelse + stack
        elif isinstance(st, (ast.With, ast.AsyncWith)):
            stack = st.body + stack


def _scan_import(stmt, r, rel, mod):
    if isinstance(stmt, ast.Import):
        for a in stmt.names:
            r['imports'].append((rel, a.name, None, a.asname))
        return
    base = stmt.module or ''
    if stmt.level:
        # 相对导入：按 level 回退包前缀再拼接
        pkg = mod.split('.')
        for _ in range(stmt.level - 1):
            if pkg:
                pkg.pop()
        base = '.'.join(pkg + ([base] if base else []))
    for a in stmt.names:
        if a.name != '*':
            r['imports'].append((rel, base, a.name, a.asname))


def _scan_class(cd, r, rel, mod, hooks):
    """单个类：classes 行、钩子类级事实、子树语句事实；嵌套类递归独立登记。"""
    cctx = FactContext(rel, mod, cd.name)
    bases = [dotted(b) for b in cd.bases if not isinstance(b, ast.Starred)]
    r['classes'].append((rel, cd.name, ','.join(b for b in bases if b), cd.lineno))
    for row in hooks.class_facts(cd, cctx):
        r[row[0]].append(row[1])
    nested = []
    for sub in _walk_no_nested_class(cd):
        if sub is cd:
            continue
        if isinstance(sub, ast.ClassDef):
            nested.append(sub)
            continue
        if isinstance(sub, FUNC_NODES):
            r['defs'].append((rel, sub.lineno, cd.name, sub.name))
            _scan_function(sub, r, rel, mod, cd.name, hooks)
        fact = hooks.class_stmt_fact(sub, cctx)
        if fact:
            r[fact[0]].append(fact[1])
        if isinstance(sub, ast.Assign):
            _scan_self_assign(sub, r, rel, cctx, hooks)
    for ncd in nested:
        _scan_class(ncd, r, rel, mod, hooks)


def _walk_no_nested_class(cd):
    """类子树全部节点；嵌套 ClassDef 只产出（供外层递归）不展开。"""
    stack = [cd]
    while stack:
        n = stack.pop()
        yield n
        if n is cd or not isinstance(n, ast.ClassDef):
            stack.extend(ast.iter_child_nodes(n))


def _scan_self_assign(st, r, rel, cctx, hooks):
    """self.X = <值> -> attr 事实；值类型先问钩子（genv.Y 等），再通用构造规则。"""
    for tgt in st.targets:
        if not (isinstance(tgt, ast.Attribute) and isinstance(tgt.value, ast.Name)
                and tgt.value.id == 'self'):
            continue
        v = st.value
        typ = None
        if isinstance(v, ast.Call) and isinstance(v.func, ast.Name):
            if v.func.id not in BUILTIN_CTORS:
                typ = v.func.id
        if typ is None:
            typ = hooks.assign_value_type(st, cctx)
        if typ:
            r['attr'].append((rel, cctx.cls, tgt.attr, typ))


def _scan_function(fn, r, rel, mod, cls, hooks):
    """函数体一次遍历双产出：return 构造() -> ret；Call 节点 -> rpc 钩子。

    模块函数与方法的 ret 命名空间不同；rpc 钩子不分命名空间（chan 已含方向）。
    """
    if cls:
        func_key = '%s.%s' % (cls, fn.name)
    else:
        func_key = fn.name
    if hooks is None:
        for node in ast.walk(fn):
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Call) \
                    and isinstance(node.value.func, ast.Name):
                t = node.value.func.id
                if t not in BUILTIN_CTORS:
                    r['ret'].append((mod, func_key, t, rel))
        return
    fctx = FactContext(rel, mod, cls)
    for node in ast.walk(fn):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Call) \
                and isinstance(node.value.func, ast.Name):
            t = node.value.func.id
            if t not in BUILTIN_CTORS:
                r['ret'].append((mod, func_key, t, rel))
        elif isinstance(node, ast.Call):
            for (chan, method, stub) in hooks.rpc_facts(node, fctx):
                r['rpc'].append((rel, node.lineno, chan, method, stub))
