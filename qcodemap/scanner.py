# -*- coding: utf-8 -*-
"""单文件 ast 扫描：names 倒排 + defs/classes/imports + 事实原始行。

通用事实（项目无关）：self.X = 构造() -> attr；return 构造() -> ret。
框架事实（声明式属性、组件注入装饰器、运行时全局注入等）：经 hooks 产出，
本模块只负责按协议调用与登记，不认识任何框架名字。
"""

import ast
import io
import keyword
import re
import tokenize
import warnings

from qcodemap.hooks import FactContext, FactsHooks

# bytes 字面量行（如 data = load_blob(b'\\xe4..')，二进制产物行）：
# 转义序列会匹配出海量伪 token，token 化前整段剔除
_BYTES_LINE = re.compile(
    r"(?<![A-Za-z0-9_])b'(?:\\.|[^'\\\r\n])*'"
    r'|(?<![A-Za-z0-9_])b"(?:\\.|[^"\\\r\n])*"')
BUILTIN_CTORS = ('str', 'int', 'float', 'bool', 'dict', 'list', 'tuple', 'set')
FUNC_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)
_TOP_LEVEL_STRUCTURE = re.compile(r'^(?:class |def |async def )', re.MULTILINE)
_SEMANTIC_SKELETON_THRESHOLD = 2 * 1024 * 1024


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


def pattern_prefix(node):
    """动态字符串实参 -> 可提取的字面前缀；提不出返回 None。

    覆盖四种拼接形态：f-string（取首个字面段）、'%s' % x（取 % 前部分）、
    'p_' + x（左操作数为字面量）、'p_{}'.format(x)（取首个 { 前）。
    动态段之后的常量不入前缀（防 'btn_{i}_x' 误配到 'btn_1_x' 之外的形态）。
    """
    if isinstance(node, ast.JoinedStr) and node.values:
        first = node.values[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str) \
                and first.value:
            return first.value
    elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod) \
            and isinstance(node.left, ast.Constant) \
            and isinstance(node.left.value, str) and '%' in node.left.value:
        return node.left.value.split('%', 1)[0]
    elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add) \
            and isinstance(node.left, ast.Constant) \
            and isinstance(node.left.value, str) and node.left.value:
        return node.left.value
    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
            and node.func.attr == 'format' \
            and isinstance(node.func.value, ast.Constant) \
            and isinstance(node.func.value.value, str):
        head = node.func.value.value.split('{', 1)[0]
        if head:
            return head
    return None


def scan_file(rel, path, hooks=None, downsample=False, profile='full'):
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
         'rpc': [], 'receiver_fact': [], 'rpc_handler': [], 'pubsub': [],
         'callback_raw': [], 'ui_binding': [], 'binding': [], 'parse_ok': True}
    if profile == 'full':
        r['names'] = _identifier_names(text, downsample)
    ast_text = text
    if profile == 'semantic-only':
        ast_text = _BYTES_LINE.sub("b''", ast_text)
        if (len(ast_text) >= _SEMANTIC_SKELETON_THRESHOLD
                and not _TOP_LEVEL_STRUCTURE.search(ast_text)):
            ast_text = _semantic_module_skeleton(ast_text)
    try:
        # 目标库老代码有非法转义序列，ast 会告警但不影响解析，压掉保持输出干净
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', SyntaxWarning)
            tree = ast.parse(ast_text)
    except SyntaxError:
        r['parse_ok'] = False
        return r
    mod = module_of(rel)
    # import 先于其余语句预扫：结构图记录全部词法作用域；函数级钩子只拿
    # 模块 + 当前/外层函数可见映射，避免不同函数的同名局部别名互相污染。
    imp_map, func_imp_maps, scope_keys = _collect_imports(tree, r, rel, mod)
    mctx = FactContext(rel, mod, None, imports=imp_map)
    for row in hooks.module_bindings(tree, mctx):
        r['binding'].append((rel,) + tuple(row))
    for stmt in _module_stmts(tree):
        if isinstance(stmt, ast.ClassDef):
            _scan_class(stmt, r, rel, mod, hooks, imp_map,
                        func_imp_maps, scope_keys)
        elif isinstance(stmt, FUNC_NODES):
            r['defs'].append((rel, stmt.lineno, None, stmt.name))
            _scan_function(stmt, r, rel, mod, None, hooks,
                           func_imp_maps.get(stmt, imp_map),
                           func_imp_maps, scope_keys)
    _dedup_pubsub(r)
    return r


def _semantic_module_skeleton(text):
    """大型纯数据模块只保留顶层 import，其余行置空但保持行号。

    core 本来就不存储模块字典值；这里避免为数十 MB 的表值
    构建 AST，同时保留模块依赖结构和准确 import 行号。
    """
    out = []
    for line in text.splitlines(True):
        stripped = line.lstrip()
        if len(stripped) == len(line) and stripped.startswith(('import ', 'from ')):
            out.append(line)
        else:
            out.append('\n' if line.endswith(('\n', '\r')) else '')
    return ''.join(out)


def _identifier_names(text, downsample=False):
    """只索引 Python tokenizer 认定的 NAME，排除字符串/注释伪命中。"""
    out = []
    seen = set() if downsample else None
    try:
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
        for token in tokens:
            if token.type != tokenize.NAME or keyword.iskeyword(token.string):
                continue
            if seen is not None:
                if token.string in seen:
                    continue
                seen.add(token.string)
            out.append((token.string, token.start[0], token.start[1] + 1))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # 老语法/截断文件仍保留已成功 token 化的前缀。
        pass
    return out


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


def _class_stmts(cd):
    """类体声明语句展开；进入控制流，不进入方法与嵌套类。"""
    stack = list(cd.body)
    while stack:
        st = stack.pop(0)
        yield st
        if isinstance(st, (ast.ClassDef,) + FUNC_NODES):
            continue
        if isinstance(st, ast.If):
            stack = st.body + st.orelse + stack
        elif isinstance(st, ast.Try):
            handler_bodies = [s for h in st.handlers for s in h.body]
            stack = st.body + st.orelse + st.finalbody + handler_bodies + stack
        elif isinstance(st, (ast.For, ast.While)):
            stack = st.body + st.orelse + stack
        elif isinstance(st, (ast.With, ast.AsyncWith)):
            stack = st.body + stack


def _record_binding(imp_map, name, target):
    """同一词法作用域的别名冲突降级为 None，宁缺毋错。"""
    old = imp_map.get(name)
    if name not in imp_map or old == target:
        imp_map[name] = target
    else:
        imp_map[name] = None


def _scan_import(stmt, r, rel, mod, imp_map, scope=''):
    """登记一条 import，并返回其当前作用域绑定。"""
    if isinstance(stmt, ast.Import):
        for a in stmt.names:
            r['imports'].append((rel, a.name, None, a.asname,
                                 stmt.lineno, scope))
            if a.asname:
                _record_binding(imp_map, a.asname, a.name)
            else:
                # import a.b 只绑定根名 a（点分引用以根名为准解析）
                root = a.name.split('.', 1)[0]
                _record_binding(imp_map, root, root)
        return
    base = stmt.module or ''
    if stmt.level:
        # 相对导入：文件模块的「当前包」是其父包（rel 为 __init__.py 时
        # mod 本身就是包）。level=1 取当前包，每多一级再退一层。
        pkg = mod.split('.')
        if not rel.endswith('__init__.py'):
            pkg.pop()
        for _ in range(stmt.level - 1):
            if pkg:
                pkg.pop()
        base = '.'.join(pkg + ([base] if base else []))
    for a in stmt.names:
        if a.name != '*':
            r['imports'].append((rel, base, a.name, a.asname,
                                 stmt.lineno, scope))
            _record_binding(imp_map, a.asname or a.name,
                            '%s.%s' % (base, a.name))


class _ImportCollector(ast.NodeVisitor):
    """一次遍历收集全部 import 与函数词法父链。"""

    def __init__(self, r, rel, mod):
        self.r = r
        self.rel = rel
        self.mod = mod
        self.stack = []
        self.module_map = {}
        self.local_maps = {}
        self.func_parents = {}
        self.scope_keys = {}

    def _scope(self):
        return '.'.join(name for (_kind, name, _node) in self.stack)

    def _binding_map(self):
        if not self.stack:
            return self.module_map
        kind, _name, node = self.stack[-1]
        return self.local_maps[node] if kind == 'func' else {}

    def visit_Import(self, node):
        _scan_import(node, self.r, self.rel, self.mod,
                     self._binding_map(), self._scope())

    def visit_ImportFrom(self, node):
        _scan_import(node, self.r, self.rel, self.mod,
                     self._binding_map(), self._scope())

    def visit_ClassDef(self, node):
        self.stack.append(('class', node.name, node))
        self.scope_keys[node] = self._scope()
        self.generic_visit(node)
        self.stack.pop()

    def _visit_function(self, node):
        # 遇到 class 会切断函数局部名字的普通词法可见性；方法只继承模块 import。
        parents = []
        for kind, _name, parent in reversed(self.stack):
            if kind == 'class':
                break
            if kind == 'func':
                parents.append(parent)
        parents.reverse()
        self.func_parents[node] = parents
        self.local_maps[node] = {}
        self.stack.append(('func', node.name, node))
        self.scope_keys[node] = self._scope()
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function


def _collect_imports(tree, r, rel, mod):
    """返回模块 import、每个函数可见 import、AST 节点作用域键。"""
    collector = _ImportCollector(r, rel, mod)
    collector.visit(tree)
    visible = {}
    for fn, local in collector.local_maps.items():
        merged = dict(collector.module_map)
        for parent in collector.func_parents.get(fn, ()):
            merged.update(collector.local_maps[parent])
        merged.update(local)
        visible[fn] = merged
    return collector.module_map, visible, collector.scope_keys


def scope_ranges(tree):
    """AST -> [(start, end, scope, kind)]，供查询侧恢复词法 import 可见域。"""
    out = []

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.stack = []

        def _visit(self, node, kind):
            self.stack.append(node.name)
            key = '.'.join(self.stack)
            out.append((node.lineno, node.end_lineno or node.lineno, key, kind))
            self.generic_visit(node)
            self.stack.pop()

        def visit_ClassDef(self, node):
            self._visit(node, 'class')

        def visit_FunctionDef(self, node):
            self._visit(node, 'func')

        def visit_AsyncFunctionDef(self, node):
            self._visit(node, 'func')

    Visitor().visit(tree)
    return out


def _scan_class(cd, r, rel, mod, hooks, imp_map, func_imp_maps, scope_keys):
    """单个类：classes 行、钩子类级事实、子树语句事实；嵌套类递归独立登记。"""
    cctx = FactContext(rel, mod, cd.name)
    bases = [dotted(b) for b in cd.bases if not isinstance(b, ast.Starred)]
    r['classes'].append((rel, cd.name, ','.join(b for b in bases if b), cd.lineno))
    for row in hooks.class_facts(cd, cctx):
        r[row[0]].append(row[1])
    # 约定回调只接受类体声明，不把方法里的同形调用误当声明。
    for sub in _class_stmts(cd):
        for kind, source, target in hooks.callback_facts(sub, cctx):
            r['callback_raw'].append(
                (rel, sub.lineno, cd.name, kind, source, target))
    # 直接类体（含 if/try 分支）负责定义扫描；_scan_function 自己递归嵌套 def。
    nested = []
    for sub in _class_stmts(cd):
        if isinstance(sub, ast.ClassDef):
            nested.append(sub)
        elif isinstance(sub, FUNC_NODES):
            r['defs'].append((rel, sub.lineno, cd.name, sub.name))
            _scan_function(
                sub, r, rel, mod, cd.name, hooks,
                func_imp_maps.get(sub, imp_map),
                func_imp_maps, scope_keys)
    # 历史通用事实允许出现在方法体内，继续遍历类子树；定义本身不重复扫描。
    for sub in _walk_no_nested_class(cd):
        if sub is cd:
            continue
        if isinstance(sub, ast.ClassDef):
            continue
        fact = hooks.class_stmt_fact(sub, cctx)
        if fact:
            r[fact[0]].append(fact[1])
        if isinstance(sub, ast.Assign):
            _scan_self_assign(sub, r, rel, cctx, hooks)
    for ncd in nested:
        _scan_class(ncd, r, rel, mod, hooks, imp_map,
                    func_imp_maps, scope_keys)


def _walk_no_nested_class(cd):
    """类子树全部节点；嵌套 ClassDef 只产出（供外层递归）不展开。"""
    stack = [cd]
    while stack:
        n = stack.pop()
        yield n
        if n is cd or not isinstance(n, ast.ClassDef):
            stack.extend(ast.iter_child_nodes(n))


def _scan_self_assign(st, r, rel, cctx, hooks):
    """self.X = <值> -> attr 事实；值类型先问钩子（全局伪类型等），再通用构造规则。"""
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


def _scan_function(fn, r, rel, mod, cls, hooks, imp_map=None,
                   func_imp_maps=None, scope_keys=None):
    """函数体一次遍历多产出：return 构造() -> ret；Call 节点 -> rpc/pubsub 钩子。

    模块函数与方法的 ret 命名空间不同；rpc/pubsub 钩子不分命名空间
    （chan/side 已含方向）。每个函数只扫描自己的词法体，嵌套 def 递归登记，
    因而局部 import 映射与 caller 归属不会串到外层函数。
    """
    if cls:
        func_key = '%s.%s' % (cls, fn.name)
    else:
        func_key = fn.name
    if hooks is None:
        for node in _walk_function_scope(fn):
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Call) \
                    and isinstance(node.value.func, ast.Name):
                t = node.value.func.id
                if t not in BUILTIN_CTORS:
                    r['ret'].append((mod, func_key, t, rel))
        return
    fctx = FactContext(rel, mod, cls, fn.name, imp_map, fn)
    for chan, method, endpoint, confidence, reason in hooks.handler_facts(fn, fctx):
        r['rpc_handler'].append(
            (rel, fn.lineno, chan, method, endpoint, confidence, reason))
    for node in _walk_function_scope(fn):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Call) \
                and isinstance(node.value.func, ast.Name):
            t = node.value.func.id
            if t not in BUILTIN_CTORS:
                r['ret'].append((mod, func_key, t, rel))
        elif isinstance(node, ast.Call):
            for (chan, method, stub) in hooks.rpc_facts(node, fctx):
                r['rpc'].append((rel, node.lineno, chan, method, stub))
            for expr, typ, confidence, reason in hooks.receiver_type_facts(node, fctx):
                r['receiver_fact'].append(
                    (rel, node.lineno, expr, typ, confidence, reason))
            for (side, event) in hooks.pubsub_facts(node, fctx):
                r['pubsub'].append((rel, node.lineno, side, event, fn.name, cls))
            for (kind, key, receiver) in hooks.ui_facts(node, fctx):
                r['ui_binding'].append(
                    (rel, node.lineno, kind, key, receiver, cls, fn.name))
    for nested in _direct_nested_functions(fn):
        r['defs'].append((rel, nested.lineno, cls, nested.name))
        nested_map = imp_map
        if func_imp_maps is not None and scope_keys is not None:
            nested_map = func_imp_maps.get(nested, imp_map)
        _scan_function(nested, r, rel, mod, cls, hooks, nested_map,
                       func_imp_maps, scope_keys)


def _walk_function_scope(fn):
    """函数自身词法体全部节点；嵌套 def/class 作为边界不下钻。"""
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.ClassDef,) + FUNC_NODES):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _direct_nested_functions(fn):
    """函数内直接嵌套 def；不重复返回更深层定义。"""
    out = []
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        if isinstance(node, FUNC_NODES):
            out.append(node)
            continue
        if isinstance(node, ast.ClassDef):
            continue
        stack.extend(ast.iter_child_nodes(node))
    return sorted(out, key=lambda n: n.lineno)


def _dedup_pubsub(r):
    """pubsub 行去重：同 (file,line,side,event) 保留最后一条。"""
    rows = r.get('pubsub')
    if not rows:
        return
    seen = {}
    for row in rows:
        seen[row[:4]] = row
    r['pubsub'] = list(seen.values())
