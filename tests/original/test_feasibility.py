# -*- coding: utf-8 -*-
"""QCodeMap 可行性验证脚本（一次性，非正式代码）。

验证链路：SQLite 倒排索引(阶段1查表) + 数据化桩事实(阶段2纯 ast 解析)
能否不依赖 jedi/.pyi 拿到 PoC 六函数的正确调用边。

做法：
1. 对少量目标文件做 ast 扫描，把 names 倒排 + defs/classes/imports 存 SQLite；
2. 桩数据化：Property/@Components/self赋值/genv全局/构造返回 全部进 facts 表；
3. 两阶段查询：SQL 拿候选 -> 自研 ast 解析器判定调用边；
4. 与已知标准答案（六函数六条边）对照。
"""
import ast
import re
import sqlite3
import sys
import time
from pathlib import Path

# 孵化案例代码库根与产物库路径（运行时按环境替换）
ROOT = Path(os.environ.get('QCODEMAP_DEMO_ROOT', '.'))
DB = Path(os.environ.get('QCODEMAP_DEMO_DB', 'cache/feasibility.db'))
TOKEN_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')


def read_source(p):
    data = p.read_bytes()
    for enc in ('utf-8', 'gbk'):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode('utf-8', errors='replace')


def module_of(rel):
    parts = rel[:-3].split('/') if rel.endswith('.py') else rel.split('/')
    if parts and parts[-1] == '__init__':
        parts = parts[:-1]
    return '.'.join(parts)


def dotted(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        b = dotted(node.value)
        return '%s.%s' % (b, node.attr) if b else None
    return None


# ---------- pass1: 单文件扫描 ----------

def scan_file(rel, path):
    text = read_source(path)
    r = {'names': [], 'defs': [], 'classes': [], 'imports': [],
         'attr': [], 'global': [], 'ret': [], 'comp_raw': []}
    for i, line in enumerate(text.splitlines(), 1):
        for m in TOKEN_RE.finditer(line):
            r['names'].append((m.group(0), rel, i, m.start() + 1))
    mod = module_of(rel)
    tree = ast.parse(text)

    def walk_class(cd):
        """类体事实：@Components 参数、Property、self.X=、方法、返回构造。"""
        cname = cd.name
        bases = [dotted(b) for b in cd.bases if not isinstance(b, ast.Starred)]
        bases = [b for b in bases if b]
        r['classes'].append((rel, cname, ','.join(bases), cd.lineno))
        for dec in cd.decorator_list:
            if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name) and dec.func.id == 'Components':
                for arg in dec.args:
                    if isinstance(arg, ast.Name):
                        r['comp_raw'].append((rel, cname, 'ref', arg.id))
                    elif isinstance(arg, ast.Attribute) and dotted(arg.value):
                        r['comp_raw'].append((rel, cname, 'attr', dotted(arg)))
                    elif isinstance(arg, ast.Starred) and isinstance(arg.value, ast.Call) \
                            and isinstance(arg.value.func, ast.Attribute) and arg.value.func.attr == 'importall' \
                            and dotted(arg.value.func.value):
                        r['comp_raw'].append((rel, cname, 'importall', dotted(arg.value.func.value)))
        for sub in ast.walk(cd):
            if isinstance(sub, ast.FunctionDef):
                r['defs'].append((rel, sub.lineno, cname, sub.name))
                for ret in ast.walk(sub):
                    if isinstance(ret, ast.Return) and isinstance(ret.value, ast.Call) \
                            and isinstance(ret.value.func, ast.Name):
                        t = ret.value.func.id
                        if t not in ('str', 'int', 'float', 'bool', 'dict', 'list', 'tuple', 'set'):
                            r['ret'].append((mod, cname + '.' + sub.name if cname else sub.name, t))
            elif isinstance(sub, ast.Assign):
                for tgt in sub.targets:
                    if isinstance(tgt, ast.Attribute) and isinstance(tgt.value, ast.Name):
                        recv, attr = tgt.value.id, tgt.attr
                        if recv == 'self':
                            t = value_type(sub.value)
                            if t:
                                r['attr'].append((rel, cname, attr, t))
                        elif recv == 'genv':
                            if isinstance(sub.value, ast.Name) and sub.value.id == 'self':
                                r['global'].append(('genv', attr, cname, rel))
            elif isinstance(sub, ast.Expr) and isinstance(sub.value, ast.Call) \
                    and isinstance(sub.value.func, ast.Name) and sub.value.func.id == 'Property':
                args = sub.value.args
                if args and isinstance(args[0], ast.Constant) and isinstance(args[0].value, str):
                    r['attr'].append((rel, cname, args[0].value, 'PROP'))

    def value_type(v):
        if isinstance(v, ast.Call) and isinstance(v.func, ast.Name):
            n = v.func.id
            return None if n in ('str', 'int', 'float', 'bool', 'dict', 'list', 'tuple', 'set') else n
        if isinstance(v, ast.Attribute) and isinstance(v.value, ast.Name) and v.value.id == 'genv':
            return 'genv.' + v.attr
        return None

    for stmt in tree.body:
        if isinstance(stmt, ast.ClassDef):
            walk_class(stmt)
        elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
            if isinstance(stmt, ast.Import):
                for a in stmt.names:
                    r['imports'].append((rel, a.name, None, a.asname))
            else:
                base = stmt.module or ''
                if stmt.level:
                    pkg = mod.split('.')
                    for _ in range(stmt.level - 1):
                        if pkg:
                            pkg.pop()
                    base = '.'.join(pkg + ([base] if base else []))
                for a in stmt.names:
                    if a.name != '*':
                        r['imports'].append((rel, base, a.name, a.asname))
        elif isinstance(stmt, ast.FunctionDef):
            r['defs'].append((rel, stmt.lineno, None, stmt.name))
    return r


def _resolve_module(con, mods, from_file, ref):
    """from_file 的 import 语境下把 ref（模块名/别名）解析为绝对 dotted 模块。"""
    if ref in mods:
        return ref
    fid_mod = module_of(from_file)
    for (m, n, a) in con.execute('SELECT module,name,alias FROM imports WHERE file=?', (from_file,)):
        if n == ref or a == ref:
            full = '%s.%s' % (m, n) if n else m
            return full if full in mods else (m if m in mods else None)
        if m == ref:
            return m
    guess = '%s.%s' % (fid_mod.rsplit('.', 1)[0] if '.' in fid_mod else '', ref)
    return guess if guess in mods else None


def build_db(files):
    if DB.exists():
        DB.unlink()
    con = sqlite3.connect(str(DB))
    con.executescript('''
    CREATE TABLE names(name TEXT, file TEXT, line INT, col INT);
    CREATE INDEX idx_n ON names(name);
    CREATE TABLE defs(file TEXT, line INT, class TEXT, name TEXT);
    CREATE TABLE classes(file TEXT, name TEXT, bases TEXT, line INT);
    CREATE TABLE imports(file TEXT, module TEXT, name TEXT, alias TEXT);
    CREATE TABLE attr(file TEXT, class TEXT, attr TEXT, type TEXT);
    CREATE TABLE global_assign(module TEXT, attr TEXT, class TEXT, file TEXT);
    CREATE TABLE ret(module TEXT, func TEXT, type TEXT);
    CREATE TABLE comp_raw(file TEXT, host TEXT, kind TEXT, value TEXT);
    CREATE TABLE comp(host TEXT, comp TEXT, comp_file TEXT);
    ''')
    for rel, p in files:
        r = scan_file(rel, p)
        con.executemany('INSERT INTO names VALUES(?,?,?,?)', r['names'])
        con.executemany('INSERT INTO defs VALUES(?,?,?,?)', r['defs'])
        con.executemany('INSERT INTO classes VALUES(?,?,?,?)', r['classes'])
        con.executemany('INSERT INTO imports VALUES(?,?,?,?)', r['imports'])
        con.executemany('INSERT INTO attr VALUES(?,?,?,?)', r['attr'])
        con.executemany('INSERT INTO global_assign VALUES(?,?,?,?)', r['global'])
        con.executemany('INSERT INTO ret VALUES(?,?,?)', r['ret'])
        con.executemany('INSERT INTO comp_raw VALUES(?,?,?,?)', r['comp_raw'])
    # pass2: 简化组件解析（ref: 同文件或 import；attr: import 前缀）
    mods = {module_of(rel): rel for rel, _ in files}
    comps = []
    for (file, host, kind, value) in con.execute('SELECT * FROM comp_raw').fetchall():
        if kind == 'ref':
            hit = con.execute('SELECT 1 FROM classes WHERE file=? AND name=?', (file, value)).fetchone()
            if hit:
                comps.append((host, value, file))
                continue
            for (m, n, a) in con.execute('SELECT module,name,alias FROM imports WHERE file=?', (file,)):
                if n == value or a == value:
                    comps.append((host, value, m.replace('.', '/') + '.py'))
                    break
        elif kind == 'attr':
            base, cls = value.rsplit('.', 1)
            for (m, n, a) in con.execute('SELECT module,name,alias FROM imports WHERE file=?', (file,)):
                if n == base or a == base or m.endswith(base):
                    f = m.replace('.', '/') + '.py'
                    hit = con.execute('SELECT 1 FROM classes WHERE file=? AND name=?', (f, cls)).fetchone()
                    if hit:
                        comps.append((host, cls, f))
                    break
        elif kind == 'importall':
            # 在 from_file 的 import 语境把 value 解析成绝对模块，读其 __init__.py
            # 的 from . import X 清单，逐模块找 {Host}Member 类
            pkg_mod = _resolve_module(con, mods, file, value)
            if not pkg_mod:
                continue
            init_rel = mods.get(pkg_mod)
            if not init_rel or not init_rel.endswith('__init__.py'):
                # 包目录下无 __init__.py 映射时，按目录前缀扫全部已索引文件
                pkg_dir = pkg_mod.replace('.', '/')
                for rel2 in mods.values():
                    if rel2.startswith(pkg_dir + '/') and rel2.endswith('.py') and '__init__' not in rel2:
                        if con.execute('SELECT 1 FROM classes WHERE file=? AND name=?',
                                       (rel2, host + 'Member')).fetchone():
                            comps.append((host, host + 'Member', rel2))
                continue
            init_tree = ast.parse(read_source(ROOT / init_rel))
            for st in ast.walk(init_tree):
                if isinstance(st, ast.ImportFrom) and st.module is None and st.level == 1:
                    for a in st.names:
                        sub = '%s.%s' % (pkg_mod, a.name)
                        sub_rel = mods.get(sub)
                        if sub_rel and con.execute('SELECT 1 FROM classes WHERE file=? AND name=?',
                                                   (sub_rel, host + 'Member')).fetchone():
                            comps.append((host, host + 'Member', sub_rel))
    con.executemany('INSERT INTO comp VALUES(?,?,?)', sorted(set(comps)))
    con.commit()
    return con


# ---------- 阶段2: 自研解析 ----------

# 桩数据化验证：以下种子 = 之前 .pyi 桩里手写的类型注解，现在进库为事实
RET_SEEDS = {
    ('gclient.framework.util.replay_util', 'GetPlayer'): 'CombatAvatar',
    ('Space', 'GetEntity'): 'AvatarSceneNode',  # 本 PoC 语境：取回带 toplogo 的场景节点
}
ATTR_SEEDS = {
    ('PlayerCombatAvatarToplogo', 'space'): 'Space',
    ('CombatAvatarMember', 'space'): 'Space',  # 组件 mixin，宿主属性（cimp_replay）
}


class Resolver(object):
    """对单个候选位置判定是否为对目标定义的调用（纯 ast + facts 表）。"""

    def __init__(self, con):
        self.con = con
        self.modmap = {}
        for (f,) in con.execute('SELECT DISTINCT file FROM classes').fetchall():
            self.modmap[module_of(f)] = f
        self.comp_hosts = {}  # host class -> set(comp classes)
        for (h, c, _f) in con.execute('SELECT * FROM comp').fetchall():
            self.comp_hosts.setdefault(h, set()).add(c)
        self.genv_types = {}
        for (mod, attr, cls, f) in con.execute('SELECT * FROM global_assign').fetchall():
            if mod == 'genv':
                self.genv_types[attr] = cls
        self.ret_facts = {}
        for (mod, func, t) in con.execute('SELECT * FROM ret').fetchall():
            self.ret_facts[(mod, func)] = t

    def class_bases(self, cls, cls_file):
        rows = self.con.execute('SELECT bases FROM classes WHERE name=? AND file=?', (cls, cls_file)).fetchall()
        return rows[0][0].split(',') if rows and rows[0][0] else []

    def mro_has_method(self, cls, cls_file, method, seen=None):
        """沿 bases + @Components 组件类找方法定义，返回定义 (file,line) 或 None。"""
        seen = seen or set()
        if (cls, cls_file) in seen:
            return None
        seen.add((cls, cls_file))
        row = self.con.execute(
            'SELECT file,line FROM defs WHERE class=? AND name=? AND file=?',
            (cls, method, cls_file)).fetchone()
        if row:
            return row
        for b in self.class_bases(cls, cls_file):
            bf = self._class_file(b, cls_file)
            if bf:
                hit = self.mro_has_method(b, bf, method, seen)
                if hit:
                    return hit
        for comp in self.comp_hosts.get(cls, ()):
            cf = self._class_file(comp)
            if cf:
                hit = self.mro_has_method(comp, cf, method, seen)
                if hit:
                    return hit
        return None

    def _class_file(self, cls, from_file=None):
        rows = self.con.execute('SELECT file FROM classes WHERE name=?', (cls,)).fetchall()
        if len(rows) == 1:
            return rows[0][0]
        if from_file:
            hit = self.con.execute('SELECT file FROM classes WHERE name=? AND file=?', (cls, from_file)).fetchone()
            if hit:
                return hit[0]
        return rows[0][0] if rows else None

    def resolve_call(self, file, line, text, name):
        """候选位置 -> 定义 (file, line) 或 None。"""
        tree = self._parse(file)
        if tree is None:
            return None
        node = _find_call_at(tree, line, name)
        if node is None:
            return None
        # self.X() -> 外层类
        if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) \
                and node.func.value.id == 'self':
            cls = _enclosing_class(tree, line)
            if cls:
                cf = self._class_file(cls, file)
                if cf:
                    return self.mro_has_method(cls, cf, name)
        # obj.X()：局部数据流（obj = expr）
        if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            var = node.func.value.id
            typ = self._local_var_type(tree, line, var, file)
            if typ:
                return self._method_on_type(typ, name)
        if isinstance(node.func, ast.Name):  # 裸函数
            hit = self.con.execute('SELECT file,line FROM defs WHERE name=? AND class IS NULL', (name,)).fetchone()
            if hit:
                return hit
        return None

    def _parse(self, file):
        if not hasattr(self, '_tree_cache'):
            self._tree_cache = {}
        if file not in self._tree_cache:
            try:
                self._tree_cache[file] = ast.parse(read_source(ROOT / file))
            except SyntaxError:
                self._tree_cache[file] = None
        return self._tree_cache[file]

    def _local_var_type(self, tree, line, var, file=None):
        """模块级+函数内向前赋值追踪（够用版）。"""
        best = None
        for n in ast.walk(tree):
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Name) and t.id == var:
                        v = n.value
                        if isinstance(v, ast.Call):
                            if isinstance(v.func, ast.Name):
                                d2 = v.func.id
                                if d2 in ('str', 'int', 'float', 'bool', 'dict', 'list', 'tuple', 'set'):
                                    best = None
                                else:
                                    # 裸名优先按 import 语境查函数返回，查不到再当构造
                                    rt = self._ret_lookup(file, d2) if file else None
                                    best = rt or d2
                            else:
                                d = dotted(v.func)
                                best = None
                                if d and file:
                                    best = self._ret_lookup(file, d)
                                    if not best and '.' in d:
                                        # head 是局部变量：先解出其类型再查 ret（space.GetEntity 场景）
                                        head, func = d.rsplit('.', 1)
                                        if head.isidentifier():
                                            ht = self._local_var_type(tree, n.lineno, head, file)
                                            if ht:
                                                best = self.ret_facts.get((ht, func)) \
                                                    or RET_SEEDS.get((ht, func))
                        elif isinstance(v, ast.Attribute) and isinstance(v.value, ast.Name):
                            base = v.value.id
                            if base == 'genv':
                                best = self.genv_types.get(v.attr)
                            elif base == 'self':
                                cls = _enclosing_class(tree, line) or ''
                                best = ATTR_SEEDS.get((cls, v.attr))
                        if n.lineno <= line and best:
                            return best
        return best

    def _ret_lookup(self, file, dotted_call):
        """module_or_var.Func(...) -> 查 ret 表（含种子）。import 别名先归一到绝对模块。"""
        if '.' not in dotted_call:
            head, func = dotted_call, ''
        else:
            head, func = dotted_call.rsplit('.', 1)
        for (m, n, a) in self.con.execute('SELECT module,name,alias FROM imports WHERE file=?', (file,)):
            if n == head or a == head:
                # from X import Y：Y 可能是子模块（Y.Func 查 X.Y.Func）或对象/类
                candidates = ['%s.%s' % (m, n) if n else m, m]
                for mod in candidates:
                    hit = self.ret_facts.get((mod, func)) or RET_SEEDS.get((mod, func))
                    if hit:
                        return hit
                # 本文件 import 的名字若在本库有同名类定义，视为构造调用返回类名
                cls_row = self.con.execute('SELECT 1 FROM classes WHERE name=? AND file=?', (head, file)).fetchone()
                if not cls_row and n == head:
                    # 无类定义且非种子 -> 可能是子模块函数，返回 None 交上层处理
                    glob = self.ret_facts.get((m, func))
                    if glob:
                        return glob
        return RET_SEEDS.get((head, func)) if func else None

    def _method_on_type(self, typ, name):
        cf = self._class_file(typ)
        if cf:
            return self.mro_has_method(typ, cf, name)
        return None


def _find_call_at(tree, line, name):
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr == name and n.lineno == line:
            return n
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                and n.func.id == name and n.lineno == line:
            return n
    return None


def _enclosing_class(tree, line):
    for n in ast.walk(tree):
        if isinstance(n, ast.ClassDef):
            if n.lineno <= line <= (n.end_lineno or n.lineno):
                for sub in ast.walk(n):
                    if isinstance(sub, ast.FunctionDef) and sub.lineno <= line <= (sub.end_lineno or sub.lineno):
                        return n.name
    return None


# ---------- 主流程 ----------

FILES = [
    'gclient/gameplay/logic_base/entities/combat_avatar.py',
    'gclient/gameplay/logic_base/entities/combatavatarmembers/__init__.py',
    'gclient/gameplay/logic_base/entities/combatavatarmembers/cimp_combat_unit.py',
    'gclient/gameplay/logic_base/entities/combatavatarmembers/cimp_replay.py',
    'gclient/framework/entities/space.py',
    'gclient/gameplay/logic_base/entities/combat_team.py',
    'gclient/gameplay/logic_base/comps/avatar_scene_node.py',
    'gclient/gameplay/logic_base/comps/comp_toplogo.py',
    'gclient/gameplay/logic_base/comps/comp_mark.py',
    'gclient/framework/util/replay_util.py',
]

TARGETS = [
    # (函数, 定义文件, 定义行, 已知调用方 [file:line], 预期难点)
    ('RefreshAiTakeoverToplogo', 'gclient/gameplay/logic_base/comps/avatar_scene_node.py', 362,
     ['gclient/gameplay/logic_base/comps/avatar_scene_node.py:272',
      'gclient/gameplay/logic_base/comps/comp_toplogo.py:368'], 'avatar 经 space.GetEntity 链'),
    ('RemoveDummyEntity', 'gclient/framework/entities/space.py', 1683,
     ['gclient/gameplay/logic_base/entities/combat_team.py:359',
      'gclient/gameplay/logic_base/entities/combatavatarmembers/cimp_replay.py:80'], 'genv.space 运行时注入'),
    ('GetTeammateInfo', 'gclient/gameplay/logic_base/entities/combatavatarmembers/cimp_combat_unit.py', 450,
     ['gclient/gameplay/logic_base/comps/avatar_scene_node.py:366'], 'replay_util.GetPlayer 返回链+组件MRO'),
]


def main():
    t0 = time.time()
    files = [(f, ROOT / f) for f in FILES]
    con = build_db(files)
    n_names = con.execute('SELECT COUNT(*) FROM names').fetchone()[0]
    n_comp = con.execute('SELECT COUNT(*) FROM comp').fetchone()[0]
    n_genv = con.execute('SELECT COUNT(*) FROM global_assign').fetchone()[0]
    print('索引: %d 文件 names=%d 组件解析=%d genv事实=%d  %.1fs  db=%.1fMB'
          % (len(files), n_names, n_comp, n_genv, time.time() - t0, DB.stat().st_size / 1048576))

    r = Resolver(con)
    t1 = time.time()
    total_hit = total_ans = 0
    for (name, def_file, def_line, answers, note) in TARGETS:
        hits = []
        cands = con.execute(
            'SELECT file,line FROM names WHERE name=? ORDER BY file,line', (name,)).fetchall()
        for (f, ln) in cands:
            if (f, ln) == (def_file, def_line):
                continue
            text = read_source(ROOT / f).splitlines()[ln - 1]
            after = text[text.index(name) + len(name):][:2].lstrip()
            if not after.startswith('('):
                continue  # 非调用形态
            got = r.resolve_call(f, ln, text, name)
            if got and got[0] == def_file and got[1] == def_line:
                hits.append('%s:%d' % (f, ln))
        ah = [a for a in answers if a in hits]
        total_hit += len(ah)
        total_ans += len(answers)
        print('%s  难点=%s' % (name, note))
        print('   候选=%d 命中标准答案 %d/%d: %s' % (len(cands), len(ah), len(answers), ah))
        extra = [h for h in hits if h not in answers]
        if extra:
            print('   额外解析边: %s' % extra)
    print('查询耗时 %.1fs；总命中 %d/%d' % (time.time() - t1, total_hit, total_ans))


if __name__ == '__main__':
    main()
