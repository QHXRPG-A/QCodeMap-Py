# -*- coding: utf-8 -*-
"""事实提取钩子协议：core 只认通用 Python 事实，框架习语由 custom 实现。

约定：
- 所有方法允许缺省实现（返回 None / []），core 回退到通用规则；
- 事实行 = (表名, 行元组)，表名限定 attr / global_assign / comp_raw，
  行结构必须与 store 对应表的列一致（见 store.DDL）；
- 伪类型名中含 '.' 且前段是已知全局对象名时，视为运行时全局引用
  （如 'genv.space'），由 resolver 查 global_assign(base, attr) 还原类型，
  core 不硬编码任何具体全局名——哪些名字算全局对象由提取器自己决定。
"""

import ast


class FactContext(object):
    """提取上下文：当前文件相对路径（posix）、模块名、所在类名（模块级为 None）。"""

    __slots__ = ('rel', 'mod', 'cls')

    def __init__(self, rel, mod, cls):
        self.rel = rel
        self.mod = mod
        self.cls = cls


class FactsHooks(object):
    """钩子基类；custom/facts.py 继承并按自家框架习语覆写。"""

    def assign_value_type(self, node, ctx):
        """Assign 赋值语句 -> 伪类型名或 None。

        node 是 ast.Assign；返回 None 时 core 走通用规则（构造调用取类名）。
        典型用途：self.X = genv.Y -> 'genv.Y'。
        """
        return None

    def class_facts(self, cd, ctx):
        """ClassDef 级事实（装饰器等）-> [(表名, 行元组), ...]。

        典型用途：@Components(...) 装饰器 -> comp_raw 行。
        """
        return []

    def class_stmt_fact(self, stmt, ctx):
        """类子树内单条语句 -> (表名, 行元组) 或 None。

        典型用途：Property("x", Type) 声明 -> attr 行；genv.X = self -> global_assign 行。
        """
        return None

    def importall_members(self, host):
        """importall 场景：宿主类在组件模块里对应的组件类名列表。

        典型用途：项目命名约定 -> [host + 'Member']。
        """
        return []
