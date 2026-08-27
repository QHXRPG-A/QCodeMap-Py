# -*- coding: utf-8 -*-
"""ui-refs：资源绑定双向查询引擎（项目无关）。

core 只提供通用机制，项目词汇与资源库访问全部经 custom 的 profile 注入
（见 custom/ui_profile.py；profile 缺省时仅输出绑定事实，不做树配对）：

- 主库 ui_binding 表（kind/key/receiver/cls/func，由 custom 的 ui_facts
  钩子产出）：本模块不解释具体 kind 名，按 profile 的 kind 分组消费
- 资源树配对（profile.adapter）：roots/named/descend/has_timeline 等
- 词汇：寻访 attr 集、锚点 receiver、wrapper 类型映射、键归一

查询期能力：
- 链式 receiver：panel = self.<寻访>('a') 后 panel.<寻访>('b')，沿赋值链
  逐段下降到子树（懒 AST，只解析结果集涉及文件）
- 列表 item 间接宿主：item 类自身无绑定，经宿主创建点（profile.item_kind）
  的锚点子树配对
- wrapper 类型交叉校验（profile.wrapper_node_types）
- ui_audit：全量分级统计 + MISS/归属失败清单（资源改名安全报告）

分级体系（对资源树查询通用）：EXACT（唯一命中）/ MULTI（候选列表）/
PATTERN（前缀模板）/ INDIRECT（经宿主）/ UNBOUND（类无绑定）/
MISS（绑定树内无此名——改名不同步的高价值诊断）/
DYNAMIC（实参为变量/拼接等静态不可解形态，不参与配对，仅审计可见）。
"""

import ast
import os
import time

_MAX_CHAIN_DEPTH = 8


def like_escape(text):
    """SQL LIKE 参数转义（配合 ESCAPE '\' 使用）。

    资源树按名/按路径匹配的参数可能含 '_'/'%'（节点名下划线高频），
    不转义会被当通配符产生假阳性候选。
    """
    return text.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


class _NoProfile(object):
    """profile 缺省占位：只出事实不做配对。"""

    seek_attrs = frozenset()
    anchor_receivers = frozenset()
    class_bind_kinds = ()
    getter_call_kind = ''
    getter_kind = ''
    node_kinds = ()
    dynamic_kinds = ()
    load_kinds = ()
    anim_load_kind = ''
    anim_play_kind = ''
    item_kind = ''
    declaration_domain = ''
    declaration_resource_relation = ''
    declaration_wrapper_relation = ''
    declaration_level = 'DECLARED'
    field_read_relation = ''
    table_read_level = 'TABLE-READ'
    base_stops = frozenset()
    wrapper_node_types = ()
    kind_labels = {}
    ui_tool_description = ''

    class _Adapter(object):
        def roots(self, res, key):
            return []

        def named(self, res, key, name, pattern=False):
            return []

        def descend(self, res, key, parents, name, pattern=False):
            return []

        def has_file(self, res, key):
            return False

        def has_timeline(self, res, key, timeline):
            return False

        def tree_summary(self, res, key):
            return {}

        def timeline_files(self, res, timeline):
            return []

        def timeline_file_count(self, res, timeline):
            return 0

    adapter = _Adapter()
    pattern_kinds = frozenset()

    @staticmethod
    def norm_res_key(name):
        return name

    @staticmethod
    def split_key_segments(key):
        return tuple(key.split('.'))

    @staticmethod
    def looks_like_key(name):
        return False

    @staticmethod
    def open_resource(cfg):
        return None


def _profile(cfg):
    return getattr(cfg, 'ui_profile', None) or _NoProfile()


def _dotted(node):
    """Name/Attribute 链 -> 点分表达式字符串；其他形态返回 None。"""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return '.'.join(reversed(parts))


class LazyAst(object):
    """查询期按文件懒解析：寻访站点森林（链式 receiver 的统一结构）。

    每个寻访调用点是一个站点节点，持有自己的段（首参）与 origin 父指针：
    - ('site', 内层站点键)：receiver 是内联寻访 Call（x.seek('a').seek('b')）
    - ('name', 点分表达式)：receiver 是变量/self.X，经赋值记录
      （变量 -> RHS 外层站点）再指向站点节点
    - ('anchor',)：profile.anchor_receivers 锚点（树根）
    站点前缀 = 沿 origin 父指针走到根的路径拼接（记忆化 + 环防护）。
    赋值式、内联式、任意混合、任意深度由此统一为一种递归。
    """

    def __init__(self, cfg, profile):
        self.cfg = cfg
        self.profile = profile
        self._files = {}
        self._bad = set()
        self._memo = {}

    def index(self, rel):
        if rel in self._files:
            return self._files[rel]
        if rel in self._bad:
            return None
        path = os.path.join(self.cfg.root, rel.replace('/', os.sep))
        try:
            with open(path, 'rb') as f:
                data = f.read()
            try:
                text = data.decode('utf-8')
            except UnicodeDecodeError:
                text = data.decode('gbk')
            tree = ast.parse(text)
        except (OSError, ValueError, SyntaxError):
            self._bad.add(rel)
            self._files[rel] = None
            return None
        attrs = self.profile.seek_attrs
        anchors = self.profile.anchor_receivers
        sites = {}      # ast节点id -> [段元组, origin, 类名, 行号]
        by_site = {}    # (行, 首参) -> [(行, 列) 站点键]（外部按行+首参定位）
        var_sites = {}  # (类名, 变量表达式) -> [(站点键, 赋值行)]
        wrappers = {}   # (行, 列) -> wrapper 类名
        wrapper_args = {}  # (行, wrapper 类名) -> 首参节点名（WRAPPER_BIND 反查锚点）

        def _seek_arg(call):
            if call.args and isinstance(call.args[0], ast.Constant) \
                    and isinstance(call.args[0].value, str) and call.args[0].value:
                return call.args[0].value
            return None

        def _is_seek_call(node):
            return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in attrs

        def visit_call(call, cls):
            arg = _seek_arg(call)
            if not arg:
                return
            # 内联链：先递归注册内层站点，外层 origin 才有指向目标。
            # 站点键用 ast 节点 id：py3.8+ 内联链各 Call 的 col_offset
            # 均指最左 receiver 起点，(行,列) 必撞键
            recv_expr = call.func.value
            if _is_seek_call(recv_expr):
                visit_call(recv_expr, cls)
            key = id(call)
            if _is_seek_call(recv_expr):
                inner_arg = _seek_arg(recv_expr)
                if inner_arg:
                    origin = ('site', id(recv_expr))
                else:
                    origin = ('name', '')  # 内层首参非常量：起点未知，按根降级
            else:
                origin = ('name', _dotted(recv_expr) or '')
            sites[key] = [self.profile.split_key_segments(arg), origin, cls, call.lineno]
            by_site.setdefault((call.lineno, arg), []).append(key)
            if len(call.args) >= 2 and isinstance(call.args[1], ast.Name):
                wrappers[key] = call.args[1].id
                wrapper_args[(call.lineno, call.args[1].id)] = arg

        def collect(node, cls):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.ClassDef):
                    collect(child, child.name)
                elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    collect(child, cls)
                elif isinstance(child, ast.Assign):
                    value = child.value
                    if _is_seek_call(value):
                        visit_call(value, cls)
                        # 变量 -> RHS 外层站点（含内联链：外层站点的 origin
                        # 已指向内层，前缀不丢段）
                        vkey = id(value)
                        for tgt in child.targets:
                            tkey = _dotted(tgt)
                            if tkey:
                                var_sites.setdefault((cls, tkey), []).append(
                                    (vkey, child.lineno))
                elif isinstance(child, ast.Call):
                    if _is_seek_call(child):
                        visit_call(child, cls)
                    collect(child, cls)
                else:
                    collect(child, cls)

        collect(tree, None)
        idx = {'sites': sites, 'by_site': by_site, 'vars': var_sites,
               'wrappers': wrappers, 'wrapper_args': wrapper_args,
               'anchors': anchors}
        self._files[rel] = idx
        self._memo.pop(rel, None)
        return idx

    # ---- 前缀解析（站点森林 + 记忆化）----

    def _site_prefix(self, rel, key, seen):
        """站点键 -> 该站点自身及之上的完整前缀段；不可解返回 None。

        origin 名字不可解时按锚点降级（起点视为树根，返回自身段）——
        与既有内联链行为一致；环返回 None。
        """
        memo = self._memo.setdefault(rel, {})
        if key in memo:
            return memo[key]
        idx = self.index(rel)
        if not idx or key not in idx['sites']:
            return None
        if key in seen:
            return None
        segs, origin, cls, line = idx['sites'][key]
        kind = origin[0]
        if kind == 'site':
            pre = self._site_prefix(rel, origin[1], seen | {key})
            out = (pre if pre is not None else ()) + segs
        else:
            name = origin[1]
            if not name or name in idx['anchors']:
                out = segs
            else:
                base = self._name_prefix(rel, line, cls, name, seen | {key})
                out = (base + segs) if base is not None else segs
        memo[key] = out
        return out

    def _name_prefix(self, rel, line, cls, name, seen):
        """receiver 表达式 -> 前缀段；未赋值/不可解返回 None。

        行号在用点之前的最近赋值优先，没有则取最后一条（初始化先行）。
        """
        idx = self.index(rel)
        if not idx:
            return None
        entries = idx['vars'].get((cls, name))
        if not entries:
            return None
        before = [e for e in entries if e[1] <= line]
        vkey = max(before or entries, key=lambda e: e[1])[0]
        return self._site_prefix(rel, vkey, seen)

    def chain_prefix(self, rel, line, cls, receiver):
        """receiver 表达式 -> 前缀段；锚点返回 ()，不可解返回 None。"""
        if not receiver:
            return None
        idx = self.index(rel)
        if not idx:
            return None
        if receiver in idx['anchors']:
            return ()
        return self._name_prefix(rel, line, cls, receiver, set())

    def site_prefix(self, rel, line, cls, arg):
        """按 (行, 首参) 定位站点 -> 该站点之前的前缀段（不含自身段）。

        与 chain_prefix 的约定一致（classify_site 会再拼站点段）；内联/
        混合/普通站点统一。找不到站点返回 None。
        """
        idx = self.index(rel)
        if not idx:
            return None
        keys = idx['by_site'].get((line, arg))
        if not keys:
            return None
        full = self._site_prefix(rel, keys[-1], set())
        # 末段即站点自身（首参单名时 len==1；点分路径时取首参分段数）
        own = len(self.profile.split_key_segments(arg))
        return full[:-own] if full is not None else None

    # ---- wrapper 索引 ----

    def wrapper_at(self, rel, line, arg):
        """寻访站点 (行, 首参) -> wrapper 类名；未命中返回 None。"""
        idx = self.index(rel)
        if not idx:
            return None
        for key in reversed(idx['by_site'].get((line, arg)) or ()):
            w = idx['wrappers'].get(key)
            if w:
                return w
        return None

    def wrapper_anchor(self, rel, line, wrapper_cls):
        """(行, wrapper 类名) -> 首参节点名（WRAPPER_BIND 事实反查锚点用）。"""
        idx = self.index(rel)
        if not idx:
            return None
        return idx['wrapper_args'].get((line, wrapper_cls))


# ---- 类绑定（哪些 kind 构成「类 -> 资源」边由 profile 决定）----

def _class_bindings(store, profile, cls, seen=None):
    if seen is None:
        seen = set()
    if not cls or cls in seen:
        return set()
    seen.add(cls)
    keys = set()
    marks = list(profile.class_bind_kinds)
    qmarks = ','.join('?' * len(marks))
    for key in store.con.execute(
            'SELECT key FROM ui_binding WHERE cls=? AND kind IN (%s)' % qmarks,
            (cls,) + tuple(marks)):
        keys.add(key[0])
    declaration_domain = getattr(profile, 'declaration_domain', '')
    resource_relation = getattr(profile, 'declaration_resource_relation', '')
    wrapper_relation = getattr(profile, 'declaration_wrapper_relation', '')
    if declaration_domain and resource_relation and wrapper_relation:
        for (key,) in store.con.execute(
                'SELECT DISTINCT r.target FROM binding w JOIN binding r '
                'ON r.domain=w.domain AND r.owner=w.owner '
                'AND r.variant=w.variant '
                'WHERE w.domain=? AND w.relation=? AND w.target=? '
                'AND r.relation=?',
                (declaration_domain, wrapper_relation, cls, resource_relation)):
            keys.add(key)
    # 路径覆写调用点（key=方法名）-> 覆写定义（profile.getter_*）
    if profile.getter_call_kind:
        for (method,) in store.con.execute(
                'SELECT key FROM ui_binding WHERE cls=? AND kind=?',
                (cls, profile.getter_call_kind)):
            rows = store.con.execute(
                'SELECT key FROM ui_binding WHERE kind=? AND cls=?',
                (profile.getter_kind, cls)).fetchall()
            if not rows:
                rows = store.con.execute(
                    'SELECT b.key FROM ui_binding b JOIN classes c '
                    'ON b.cls=c.name AND b.file=c.file '
                    'WHERE b.kind=? AND b.receiver=?',
                    (profile.getter_kind, method)).fetchall()
            for (k,) in rows:
                keys.add(k)
    for (bstr,) in store.con.execute(
            'SELECT bases FROM classes WHERE name=?', (cls,)).fetchall():
        for base in (bstr.split(',') if bstr else ()):
            base = base.strip().rsplit('.', 1)[-1]
            if base and base not in profile.base_stops:
                keys |= _class_bindings(store, profile, base, seen)
    return keys


def _cached_class_bindings(store, profile, cls, cache):
    if cls not in cache:
        cache[cls] = _class_bindings(store, profile, cls)
    return cache[cls]


# ---- 树下降配对（引擎语义：段间递归找，段可跳层）----

def _descend_segments(profile, res, key, segments, pattern_last=False):
    parents = profile.adapter.roots(res, key)
    for i, seg in enumerate(segments):
        if not parents:
            return []
        last = i == len(segments) - 1
        hits = profile.adapter.descend(
            res, key, parents, seg, pattern=last and pattern_last)
        if last:
            return hits
        parents = [p for p, _t in hits]
    return []


def _wrapper_allowed(profile, wrapper):
    # 先精确匹配，避免 UIText 抢先命中 UITexture 这类公共前缀名称。
    for prefix, allowed in profile.wrapper_node_types:
        if wrapper == prefix:
            return allowed
    for prefix, allowed in profile.wrapper_node_types:
        if wrapper.startswith(prefix):
            return allowed
    return None


def _wrapper_note(profile, wrapper, hits):
    allowed = _wrapper_allowed(profile, wrapper)
    if not allowed or not hits:
        return None
    types = {t for _, _, t in hits}
    if types and not (types & set(allowed)):
        return 'TYPE-MISMATCH: wrapper %s 允许 %s，实际 %s' % (
            wrapper, '/'.join(allowed), '/'.join(sorted(types)))
    return None


_MAX_ITEM_DEPTH = 3


def _item_host(store, profile, lazy, cls, cache, depth=0):
    """item 类 -> (宿主类链, 宿主绑定集)；无法定位返回 None。

    两条间接边（profile 声明）：列表创建（item_kind，锚点=创建 receiver
    链）与包装构造（wrapper_bind_kind，key=类名，锚点段=寻访节点名）。
    宿主自身也无绑定时（item 套 item），宿主作为 item 向上再找一层
    （深度上限 _MAX_ITEM_DEPTH 防环）。返回宿主链逐层 (锚点前缀, 宿主类)，
    配对时按层下降。
    """
    if 'item_host' not in cache:
        cache['item_host'] = {}
    if (cls, depth) in cache['item_host']:
        return cache['item_host'][(cls, depth)]
    result = None
    edges = []
    for kind in (profile.item_kind, profile.wrapper_bind_kind):
        if kind:
            edges.append(kind)
    if edges and depth <= _MAX_ITEM_DEPTH:
        qmarks = ','.join('?' * len(edges))
        for hfile, hline, hrecv, hkey, hkind, hcls in store.con.execute(
                'SELECT file, line, receiver, key, kind, cls FROM ui_binding '
                'WHERE kind IN (%s) AND key=? ORDER BY file, line' % qmarks,
                tuple(edges) + (cls,)):
            if hkind == profile.wrapper_bind_kind:
                # 包装构造：锚点段就是那次寻访的首参节点名（经 wrapper 行
                # 号索引还原；解不出则退化为整树平铺）
                anchor = None
                if lazy is not None:
                    anchor = lazy.wrapper_anchor(hfile, hline, cls)
                prefix = (anchor,) if anchor else ()
                prefix = prefix + (lazy.chain_prefix(hfile, hline, hcls, hrecv)
                                   if lazy is not None and anchor is None else ())
            else:
                prefix = lazy.chain_prefix(hfile, hline, hcls, hrecv) \
                    if lazy is not None else None
                prefix = prefix or ()
            host_keys = _cached_class_bindings(store, profile, hcls, cache['bind'])
            if host_keys:
                result = ([(prefix, hcls)], host_keys)
                break
            upper = _item_host(store, profile, lazy, hcls, cache, depth + 1)
            if upper is not None:
                chain, keys = upper
                result = (chain + [(prefix, hcls)], keys)
                break
    cache['item_host'][(cls, depth)] = result
    return result


def _classify_site(store, profile, res, lazy, row, cache):
    """单个节点寻访站点 -> 分级配对 item dict。"""
    file, line, kind, key, receiver, cls, func = row
    item = {'node': key, 'receiver': receiver, 'file': file, 'line': line,
            'caller': '%s.%s' % (cls, func) if cls else func}
    if res is None:
        item.update(level=kind, note='资源库不可用，无法配对')
        return item
    segs = profile.split_key_segments(key)
    pattern_last = kind in profile.pattern_kinds
    keys = _cached_class_bindings(store, profile, cls, cache['bind'])
    hits = []   # [(资源键, path, node_type)]
    level = None
    host_cls = None
    if keys:
        prefix = None
        if lazy is not None:
            if receiver:
                prefix = lazy.chain_prefix(file, line, cls, receiver)
            else:
                # receiver 为空（Call 形态）：按 (行, 首参) 定位站点，
                # 经站点森林 origin 父指针取前缀（内联/混合通用）
                prefix = lazy.site_prefix(file, line, cls, key)
        if prefix:
            item['chain'] = list(prefix)
        for k in sorted(keys):
            full = list(prefix) if prefix else []
            if len(full) >= _MAX_CHAIN_DEPTH + len(segs):
                continue
            for path, nt in _descend_segments(
                    profile, res, k, full + list(segs), pattern_last):
                hits.append((k, path, nt))
    else:
        host = _item_host(store, profile, lazy, cls, cache)
        if host is None:
            item.update(level='UNBOUND',
                        note='所在类无资源绑定（动态加载/表格路径/未支持的间接形态）')
            return item
        host_chain, host_keys = host
        level = 'INDIRECT'
        item['host'] = '>'.join(h for _, h in host_chain)
        for k in sorted(host_keys):
            # 宿主链逐层下降：上层锚点先定位宿主模板，再走本层 listview
            # 锚点，最后是站点段（各段均可跳层，引擎递归语义）
            layers = [p for p, _ in host_chain]
            segments = [s for layer in layers for s in layer] + list(segs)
            for path, nt in _descend_segments(
                    profile, res, k, segments, pattern_last):
                hits.append((k, path, nt))

    if hits:
        paths = ['%s:%s' % (c, p) for c, p, _ in hits]
        if level == 'INDIRECT':
            item.update(level=level,
                        match=(paths[0] if len(paths) == 1 else paths[:8]),
                        note='经宿主链 %s 配对' % item['host'])
            if len(paths) > 1:
                item['n_match'] = len(paths)
        elif len(paths) == 1:
            item.update(level='EXACT', match=paths[0])
        else:
            item.update(level='MULTI', match=paths[:8], n_match=len(paths))
        if lazy is not None:
            wrapper = lazy.wrapper_at(file, line, key)
            if wrapper:
                item['wrapper'] = wrapper
                note = _wrapper_note(profile, wrapper, hits)
                if note:
                    item['note'] = (item.get('note', '') + ' ' + note).strip()
    else:
        bound = ','.join(sorted(keys))[:80] if keys else (
            '宿主 %s 树' % host_cls if host_cls else '')
        item.update(level='PATTERN' if pattern_last else 'MISS',
                    note='绑定树(%s)内无此名——资源侧改名不同步或已删' % bound)
    return item


def _attributed(store, profile, res, lazy, row):
    """动画播放行 -> 归属资源键；优先级：同函数同 receiver 最近挂载 >
    类级挂载 > 类资源绑定；逐级经 has_timeline 验证，全不中返回 None。"""
    file, line, kind, key, receiver, cls, func = row
    candidates = []
    if profile.anim_load_kind:
        for (k,) in store.con.execute(
                'SELECT key FROM ui_binding WHERE kind=? AND file=? AND func=? '
                'AND receiver IS ? AND line<=? ORDER BY line DESC LIMIT 1',
                (profile.anim_load_kind, file, func, receiver, line)):
            candidates.append(k)
        candidates.extend(r[0] for r in store.con.execute(
            'SELECT key FROM ui_binding WHERE cls=? AND kind=? ORDER BY line',
            (cls, profile.anim_load_kind)))
    candidates.extend(sorted(_class_bindings(store, profile, cls)))
    for k in candidates:
        if profile.adapter.has_timeline(res, k, key):
            return k
    return None


# ---- 三视图 + audit + 入口 ----

def _subclass_closure(store, profile, base, cache):
    """基类 -> 全部子类集合（classes 表 bases 边反向闭包，记忆化）。"""
    if 'subcls' not in cache:
        cache['subcls'] = {}
        # 一次性建反向边：base 名 -> [子类名]（同名类跨文件都算）
        for sub, bstr in store.con.execute(
                'SELECT name, bases FROM classes').fetchall():
            for b in (bstr.split(',') if bstr else ()):
                b = b.strip().rsplit('.', 1)[-1]
                if b:
                    cache['subcls'].setdefault(b, set()).add(sub)
    memo = cache.setdefault('subcls_memo', {})
    if base in memo:
        return memo[base]
    memo[base] = set()  # 环防护占位
    out = set()
    for direct in cache['subcls'].get(base, ()):
        out.add(direct)
        out |= _subclass_closure(store, profile, direct, cache)
    memo[base] = out
    return out


def _file_view(store, profile, cfg, res, name):
    items = []
    cache = {}
    declaration_domain = getattr(profile, 'declaration_domain', '')
    resource_relation = getattr(profile, 'declaration_resource_relation', '')
    declaration_level = getattr(profile, 'declaration_level', 'DECLARED')
    if declaration_domain and resource_relation:
        wrapper_relation = getattr(profile, 'declaration_wrapper_relation', '')
        if wrapper_relation:
            rows = store.con.execute(
                'SELECT r.file,r.line,r.owner,r.variant,r.confidence,r.reason,'
                'w.target FROM binding r LEFT JOIN binding w '
                'ON w.domain=r.domain AND w.owner=r.owner '
                'AND w.variant=r.variant AND w.relation=? '
                'WHERE r.domain=? AND r.relation=? AND r.target=? '
                'ORDER BY r.file,r.line,w.target',
                (wrapper_relation, declaration_domain,
                 resource_relation, name)).fetchall()
        else:
            rows = [(f, ln, owner, variant, confidence, reason, None)
                    for f, ln, owner, variant, confidence, reason
                    in store.con.execute(
                        'SELECT file,line,owner,variant,confidence,reason '
                        'FROM binding WHERE domain=? AND relation=? AND target=? '
                        'ORDER BY file,line',
                        (declaration_domain, resource_relation, name))]
        for f, ln, owner, variant, confidence, reason, wrapper in rows:
            note = reason or ''
            if wrapper is None and wrapper_relation:
                note = (note + '; ' if note else '') + '未声明 wrapper'
            items.append({
                'level': declaration_level,
                'key': name, 'file': f, 'line': ln,
                'caller': wrapper or owner, 'owner': owner,
                'variant': variant, 'confidence': confidence,
                'note': note,
            })
    view_kinds = tuple(profile.load_kinds) + (
        (profile.getter_kind,) if profile.getter_kind else ())
    qmarks = ','.join('?' * len(view_kinds))
    for ln, k, key, receiver, cls, func, f in store.con.execute(
            'SELECT line, kind, key, receiver, cls, func, file FROM ui_binding '
            'WHERE key=? AND kind IN (%s) ORDER BY file, line' % qmarks,
            (name,) + view_kinds):
        items.append({'level': k, 'key': key, 'receiver': receiver,
                      'file': f, 'line': ln,
                      'caller': '%s.%s' % (cls, func) if cls else func})
        # 继承反闭包：直接绑定行所在的类 -> 未写绑定行的子类也使用该
        # 资源（A 继承 B、B 绑 c，则 c 的视图同时列出 A）
        if cls:
            for sub in sorted(_subclass_closure(store, profile, cls, cache)):
                sub_keys = _class_bindings(store, profile, sub)
                if name in sub_keys:
                    items.append({'level': 'BIND_INHERIT', 'key': key,
                                  'file': f, 'line': ln, 'caller': sub,
                                  'note': '继承自 %s 使用该资源' % cls})
    # 表驱动消费点：数据表字段声明（relation='resource'）按 owner join
    # 运行时读取行（relation='field_read'），得到「该资源经哪个表字段、
    # 被哪些加载点消费」的完整链路
    field_read_relation = getattr(profile, 'field_read_relation', '')
    table_read_level = getattr(profile, 'table_read_level', 'TABLE-READ')
    if field_read_relation and declaration_domain and resource_relation:
        for f, ln, owner, variant, confidence, reason in store.con.execute(
                'SELECT rd.file, rd.line, rd.owner, rd.variant, '
                'rd.confidence, rd.reason '
                'FROM binding d JOIN binding rd '
                'ON rd.domain=d.domain AND rd.owner=d.owner '
                'WHERE d.domain=? AND d.relation=? AND d.target=? '
                'AND rd.relation=? ORDER BY rd.file, rd.line',
                (declaration_domain, resource_relation, name,
                 field_read_relation)):
            items.append({'level': table_read_level, 'key': name,
                          'file': f, 'line': ln, 'caller': owner,
                          'owner': owner, 'variant': variant,
                          'confidence': confidence, 'note': reason})
    if res is not None:
        if not profile.adapter.has_file(res, name):
            items.append({'level': 'RES-MISS', 'key': name,
                          'note': '资源不存在（已删或改名）'})
        else:
            if declaration_domain and not items:
                items.append({
                    'level': 'DECLARATION-MISSING', 'key': name,
                    'note': '资源存在，但索引中没有代码加载或声明关系',
                })
            summary = profile.adapter.tree_summary(res, name)
            summary.update({'level': 'RES-TREE', 'key': name})
            items.append(summary)
    return items


def _node_view(store, profile, cfg, res, name, limit, offset):
    lazy = LazyAst(cfg, profile)
    cache = {'bind': {}, 'lazy': lazy}
    items = []
    qmarks = ','.join('?' * len(profile.node_kinds))
    # 点分路径行按段召回：首段/末段/中间段三种形态；LIKE 参数须转义
    esc = like_escape(name)
    where = ('kind IN (%s) AND (key=? OR key LIKE ? ESCAPE \'\\\' '
             'OR key LIKE ? ESCAPE \'\\\' OR key LIKE ? ESCAPE \'\\\')') % qmarks
    params = tuple(profile.node_kinds) + (
        name, esc + '.%', '%.' + esc, '%.' + esc + '.%')
    total = store.con.execute(
        'SELECT COUNT(*) FROM ui_binding WHERE ' + where, params).fetchone()[0]
    sql = ('SELECT file, line, kind, key, receiver, cls, func FROM ui_binding '
           'WHERE ' + where + ' ORDER BY file, line LIMIT ? OFFSET ?')
    for row in store.con.execute(sql, params + (limit, offset)):
        items.append(_classify_site(store, profile, res, lazy, row, cache))
    return items, total


def _anim_view(store, profile, cfg, res, name, limit, offset):
    items = []
    lazy = LazyAst(cfg, profile)
    n_defs = 0
    if res is not None and name and profile.anim_play_kind:
        count_fn = getattr(profile.adapter, 'timeline_file_count', None)
        if count_fn is not None:
            n_defs = count_fn(res, name)
            def_offset = min(offset, n_defs)
            def_limit = min(limit, max(0, n_defs - def_offset))
            keys = profile.adapter.timeline_files(
                res, name, limit=def_limit, offset=def_offset)
        else:
            all_keys = profile.adapter.timeline_files(res, name)
            n_defs = len(all_keys)
            keys = all_keys[offset:offset + limit]
        for key in keys:
            items.append({'level': 'ANIM-DEF', 'name': name, 'key': key,
                          'note': '资源内定义该时间线'})
    n_plays = 0
    if profile.anim_play_kind:
        n_plays = store.con.execute(
            'SELECT COUNT(*) FROM ui_binding WHERE kind=? AND key=?',
            (profile.anim_play_kind, name)).fetchone()[0]
        play_offset = max(0, offset - n_defs)
        play_limit = max(0, limit - len(items))
        for row in store.con.execute(
                'SELECT file, line, kind, key, receiver, cls, func '
                'FROM ui_binding WHERE kind=? AND key=? ORDER BY file, line '
                'LIMIT ? OFFSET ?',
                (profile.anim_play_kind, name, play_limit, play_offset)):
            attributed = _attributed(store, profile, res, lazy, row) \
                if res is not None else None
            entry = {'level': 'ANIM-PLAY', 'name': row[3],
                     'receiver': row[4], 'file': row[0], 'line': row[1],
                     'caller': '%s.%s' % (row[5], row[6]) if row[5] else row[6],
                     'attributed': attributed}
            if res is not None and attributed is None:
                entry['note'] = '类绑定与挂载内均未见该时间线（ATTRIBUTION-MISS）'
            items.append(entry)
    return items, n_defs + n_plays


def ui_refs(store, cfg, name=None, kind=None, py_file=None, json_out=True,
            limit=200, offset=0):
    """资源绑定双向查询主入口（节点/资源/动画名三视图 + 按文件清单）。"""
    t0 = time.time()
    limit = max(1, min(int(limit), 1000))
    offset = max(0, int(offset))
    profile = _profile(cfg)
    res = profile.open_resource(cfg)
    items = []
    total = 0
    paged_in_query = False
    view = None

    if py_file:
        view = 'file'
        # 节点寻访行走分类配对（带树路径），其余 kind 原样列出；
        # 一个文件的清单即「成员变量/调用行 <-> 资源节点」映射表
        lazy = LazyAst(cfg, profile)
        cache = {'bind': {}, 'lazy': lazy}
        for row in store.con.execute(
                'SELECT line, kind, key, receiver, cls, func FROM ui_binding '
                'WHERE file=? ORDER BY line', (py_file,)):
            ln, k, key, receiver, cls, func = row
            if res is not None and k in profile.node_kinds:
                items.append(_classify_site(store, profile, res, lazy,
                                            (py_file,) + row, cache))
            else:
                items.append({'level': k, 'key': key, 'receiver': receiver,
                              'file': py_file, 'line': ln,
                              'caller': '%s.%s' % (cls, func) if cls else func})
        declaration_domain = getattr(profile, 'declaration_domain', '')
        if declaration_domain:
            declaration_level = getattr(profile, 'declaration_level', 'DECLARED')
            # 表驱动字段读取行由下方 field_read 通道渲染（带资源键还原），
            # 声明通道跳过避免同站点双行
            field_read_relation = getattr(profile, 'field_read_relation', '')
            table_read_level = getattr(profile, 'table_read_level', 'TABLE-READ')
            resource_relation = getattr(
                profile, 'declaration_resource_relation', '')
            skip_rel = ('AND relation != ?' if field_read_relation else '')
            decl_params = [py_file, declaration_domain]
            if field_read_relation:
                decl_params.append(field_read_relation)
            for ln, owner, relation, target, variant, confidence, reason in \
                    store.con.execute(
                        'SELECT line,owner,relation,target,variant,confidence,reason '
                        'FROM binding WHERE file=? AND domain=? ' + skip_rel +
                        ' ORDER BY line,relation', decl_params):
                items.append({
                    'level': declaration_level,
                    'key': target, 'file': py_file, 'line': ln,
                    'caller': owner, 'owner': owner,
                    'relation': relation, 'variant': variant,
                    'confidence': confidence, 'note': reason,
                })
            if field_read_relation:
                for ln, owner, variant, confidence, reason, targets in \
                        store.con.execute(
                            'SELECT rd.line, rd.owner, rd.variant, '
                            'rd.confidence, rd.reason, '
                            'GROUP_CONCAT(d.target) FROM binding rd '
                            'LEFT JOIN binding d ON d.domain=rd.domain '
                            'AND d.owner=rd.owner AND d.relation=? '
                            'WHERE rd.file=? AND rd.domain=? AND rd.relation=? '
                            'GROUP BY rd.line, rd.owner ORDER BY rd.line',
                            (resource_relation, py_file, declaration_domain,
                             field_read_relation)):
                    keys = sorted(set(targets.split(','))) if targets else []
                    items.append({
                        'level': table_read_level, 'key': owner,
                        'file': py_file, 'line': ln, 'caller': owner,
                        'owner': owner, 'variant': variant,
                        'confidence': confidence,
                        'match': keys or None,
                        'note': reason or '表字段无资源声明，未参与配对',
                    })
    elif kind == 'anim' and profile.anim_play_kind:
        view = 'anim'
        items, total = _anim_view(
            store, profile, cfg, res, name, limit, offset)
        paged_in_query = True
    elif name and (kind == 'file' or profile.looks_like_key(name)):
        view = 'file'
        name = profile.norm_res_key(name)
        items = _file_view(store, profile, cfg, res, name)
    elif name:
        view = 'node'
        items, total = _node_view(
            store, profile, cfg, res, name, limit, offset)
        paged_in_query = True

    if not paged_in_query:
        total = len(items)
        items = items[offset:offset + limit]

    note = ''
    if isinstance(profile, _NoProfile):
        note = '该项目未提供 UI 查询 profile（custom/ui_profile.py）'
    elif res is None:
        note = '资源库未建（资源根未配置或未 build）；树配对不可用，仅显示绑定事实'
    bad = store.parse_failed_files()
    if bad and not note:
        note = '全库 %d 个文件 ast 解析失败，其中的调用不在结果内' % len(bad)
    out = {
        'schema_version': 'qcodemap.ui/v3',
        'view': view, 'name': name, 'kind': kind, 'py_file': py_file,
        'items': items, 'n_items': len(items),
        'total': total, 'offset': offset, 'limit': limit,
        'truncated': offset + len(items) < total,
        'next_offset': (offset + len(items)
                        if offset + len(items) < total else None),
        'resource_index': 'ok' if res is not None else 'unavailable',
        'note': note,
        'coverage': {'status': 'partial' if bad else 'complete',
                     'parse_failed': len(bad)},
        'elapsed': round(time.time() - t0, 3),
    }
    if res is not None:
        res.close()
    if json_out:
        return out
    return _render(out)


def ui_audit(store, cfg, json_out=True, miss_cap=100):
    """全量审计：全部节点站点分级统计 + MISS/归属失败/资源缺失清单。

    用途：资源改名/删节点/删时间线前的安全检查（影响面报告）。
    """
    t0 = time.time()
    profile = _profile(cfg)
    res = profile.open_resource(cfg)
    lazy = LazyAst(cfg, profile)
    cache = {'bind': {}, 'lazy': lazy}
    tally = {}
    miss_list = []
    type_mismatch = []
    unbound_classes = set()
    dynamic_list = []

    # 动态形态（profile.dynamic_kinds）：不可静态解的实参，按 kind 计数 +
    # 清单留痕（不参与配对，规模即「静态分析盲区」的可见性）
    if profile.dynamic_kinds:
        dqmarks = ','.join('?' * len(profile.dynamic_kinds))
        for dkind, dkey, dfile, dline, dcls, dfunc in store.con.execute(
                'SELECT kind, key, file, line, cls, func FROM ui_binding '
                'WHERE kind IN (%s) ORDER BY file, line' % dqmarks,
                tuple(profile.dynamic_kinds)):
            tally['DYNAMIC'] = tally.get('DYNAMIC', 0) + 1
            if len(dynamic_list) < miss_cap:
                dynamic_list.append({'kind': dkind, 'arg': dkey,
                                     'file': dfile, 'line': dline,
                                     'caller': '%s.%s' % (dcls, dfunc)
                                     if dcls else dfunc})

    if profile.node_kinds:
        qmarks = ','.join('?' * len(profile.node_kinds))
        for row in store.con.execute(
                'SELECT file, line, kind, key, receiver, cls, func '
                'FROM ui_binding WHERE kind IN (%s) ORDER BY file, line'
                % qmarks, tuple(profile.node_kinds)):
            item = _classify_site(store, profile, res, lazy, row, cache)
            level = item['level']
            tally[level] = tally.get(level, 0) + 1
            if level == 'MISS' and len(miss_list) < miss_cap:
                miss_list.append({'node': item['node'], 'file': item['file'],
                                  'line': item['line'], 'caller': item['caller'],
                                  'note': item.get('note', '')})
            if level == 'UNBOUND':
                unbound_classes.add(row[5] or '?<module>')
            if 'TYPE-MISMATCH' in item.get('note', '') and len(type_mismatch) < miss_cap:
                type_mismatch.append({'node': item['node'], 'file': item['file'],
                                      'line': item['line'],
                                      'wrapper': item.get('wrapper'),
                                      'note': item['note']})

    anim_miss = []
    n_anim = n_anim_attr = 0
    if res is not None and profile.anim_play_kind:
        for row in store.con.execute(
                'SELECT file, line, kind, key, receiver, cls, func '
                'FROM ui_binding WHERE kind=? ORDER BY file, line',
                (profile.anim_play_kind,)):
            n_anim += 1
            if _attributed(store, profile, res, lazy, row) is not None:
                n_anim_attr += 1
            elif len(anim_miss) < miss_cap:
                anim_miss.append({'name': row[3], 'file': row[0],
                                  'line': row[1],
                                  'caller': '%s.%s' % (row[5], row[6])
                                  if row[5] else row[6]})

    res_missing = []
    if res is not None and profile.class_bind_kinds:
        bind_kinds = tuple(k for k in profile.class_bind_kinds)
        qmarks = ','.join('?' * len(bind_kinds))
        for (key,) in store.con.execute(
                'SELECT DISTINCT key FROM ui_binding WHERE kind IN (%s) '
                'AND kind != ? ORDER BY key' % qmarks,
                bind_kinds + (profile.getter_call_kind,)):
            if not profile.adapter.has_file(res, key):
                res_missing.append(key)

    n_sites = sum(tally.values())
    # 表驱动字段读取规模（扫描期提取、build_done 已修剪至有资源声明的字段）
    table_driven = {}
    field_read_relation = getattr(profile, 'field_read_relation', '')
    declaration_domain = getattr(profile, 'declaration_domain', '')
    if field_read_relation and declaration_domain:
        table_driven['n_reads'] = store.con.execute(
            'SELECT COUNT(*) FROM binding WHERE domain=? AND relation=?',
            (declaration_domain, field_read_relation)).fetchone()[0]
        table_driven['n_fields'] = store.con.execute(
            'SELECT COUNT(DISTINCT owner) FROM binding '
            'WHERE domain=? AND relation=?',
            (declaration_domain, field_read_relation)).fetchone()[0]
    out = {
        'schema_version': 'qcodemap.ui-audit/v1',
        'resource_index': 'ok' if res is not None else 'unavailable',
        'sites': tally, 'n_sites': n_sites,
        'n_unbound_classes': len(unbound_classes),
        'unbound_classes': sorted(unbound_classes)[:40],
        'anim': {'n_play': n_anim, 'n_attributed': n_anim_attr,
                 'miss': anim_miss},
        'res_missing': res_missing,
        'type_mismatch': type_mismatch,
        'miss': miss_list,
        'dynamic': dynamic_list,
        'table_driven': table_driven,
        'miss_cap': miss_cap,
        'elapsed': round(time.time() - t0, 3),
    }
    if res is not None:
        res.close()
    if json_out:
        return out
    lines = ['ui-audit: %d 个节点站点  分级 %s' % (
        n_sites, dict(sorted(tally.items())))]
    lines.append('  播放点 %d，归属成功 %d，失败清单 %d 条' % (
        n_anim, n_anim_attr, len(anim_miss)))
    lines.append('  UNBOUND 类 %d 个；RES-MISS %d 个；TYPE-MISMATCH %d 条'
                 % (len(unbound_classes), len(res_missing), len(type_mismatch)))
    if out['table_driven']:
        lines.append('  表驱动字段读取 %d 处（%d 个字段）' % (
            out['table_driven']['n_reads'], out['table_driven']['n_fields']))
    for m in miss_list[:20]:
        lines.append('  [MISS] %s  %s:%s  (%s)' % (
            m['node'], m['file'], m['line'], m['caller']))
    for m in type_mismatch[:10]:
        lines.append('  [TYPE-MISMATCH] %s(%s)  %s:%s' % (
            m['node'], m.get('wrapper'), m['file'], m['line']))
    for m in anim_miss[:10]:
        lines.append('  [ANIM-MISS] %s  %s:%s' % (m['name'], m['file'], m['line']))
    for key in res_missing[:10]:
        lines.append('  [RES-MISS] %s' % key)
    for m in dynamic_list[:10]:
        lines.append('  [DYNAMIC:%s] %s  %s:%s  (%s)' % (
            m['kind'], m['arg'], m['file'], m['line'], m['caller']))
    return '\n'.join(lines)


def _render(out):
    lines = ['ui-refs %s view=%s: %d 条' % (out.get('name') or out.get('py_file'),
                                            out['view'], out['n_items'])]
    for i in out['items']:
        key = i.get('key') or i.get('node')
        head = '  [%s%s] %s' % (i['level'],
                                ' ' + key if key else '',
                                i.get('caller') or '')
        loc = ' %s:%s' % (i.get('file'), i.get('line')) if i.get('file') else ''
        extra = ''
        if i.get('chain'):
            extra += '  chain=%s' % '.'.join(i['chain'])
        if i.get('match'):
            m = i['match']
            extra += '  match=%s' % (m if isinstance(m, str)
                                     else '(%d) %s' % (i.get('n_match', len(m)),
                                                       ','.join(m[:3])))
        if i.get('note'):
            extra += '  note=%s' % i['note']
        recv = '  recv=%s' % i['receiver'] if i.get('receiver') else ''
        lines.append(head + loc + recv + extra)
    if out.get('note'):
        lines.append('  note: %s' % out['note'])
    return '\n'.join(lines)
