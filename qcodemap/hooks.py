# -*- coding: utf-8 -*-
"""事实提取钩子协议：core 只认通用 Python 事实，框架习语由 custom 实现。

约定：
- 所有方法允许缺省实现（返回 None / []），core 回退到通用规则；
- 事实行 = (表名, 行元组)，表名限定 attr / global_assign / comp_raw，
  行结构必须与 store 对应表的列一致（见 store.DDL）；rpc/pubsub/callback
  钩子返回裸元组，rel/line 等定位列由 scanner 补齐；
- 伪类型名中含 '.' 且前段是已知全局对象名时，视为运行时全局引用
  （如 'gw.space'），由 resolver 查 global_assign(base, attr) 还原类型，
  core 不硬编码任何具体全局名——哪些名字算全局对象由提取器自己决定。
"""

import ast


class FactContext(object):
    """提取上下文：相对路径（posix）、模块名、所在类名（模块级为 None）。

    func/imports 仅函数级钩子（rpc_facts/pubsub_facts）有值：func=所在函数名，
    imports=本文件模块级 import 预扫映射 {本地名: 目标点分路径}，供框架侧
    把 evt_mod.X 这类引用归一成可 join 的完整常量键。
    """

    __slots__ = ('rel', 'mod', 'cls', 'func', 'imports', 'function_node')

    def __init__(self, rel, mod, cls, func=None, imports=None, function_node=None):
        self.rel = rel
        self.mod = mod
        self.cls = cls
        self.func = func
        self.imports = imports
        self.function_node = function_node


class FactsHooks(object):
    """钩子基类；custom/facts.py 继承并按自家框架习语覆写。"""

    def assign_value_type(self, node, ctx):
        """Assign 赋值语句 -> 伪类型名或 None。

        node 是 ast.Assign；返回 None 时 core 走通用规则（构造调用取类名）。
        典型用途：self.X = gw.Y -> 'gw.Y'。
        """
        return None

    def class_facts(self, cd, ctx):
        """ClassDef 级事实（装饰器等）-> [(表名, 行元组), ...]。

        典型用途：@Inject(...) 一类组件注入装饰器 -> comp_raw 行。
        """
        return []

    def class_stmt_fact(self, stmt, ctx):
        """类子树内单条语句 -> (表名, 行元组) 或 None。

        典型用途：decl("x", Type) 一类声明式属性 -> attr 行；
        gw.X = self -> global_assign 行。
        """
        return None

    def importall_members(self, host):
        """importall 场景：宿主类在组件模块里对应的组件类名列表。

        典型用途：项目命名约定 -> [host + 'Member']。
        """
        return []

    def rpc_facts(self, call, ctx):
        """函数体内任意 Call 节点 -> [(chan, method, stub), ...]。

        用于字符串分发的 RPC/远端调用习语：chan=通道名（如 C2S/S2C/STUB，
        对 core 不透明），method=远端方法名字符串，stub=目标实体类名或 None。
        core 对每个函数体内的 Call 节点各调用一次；返回 [] 走通用规则。
        """
        return []

    def pubsub_facts(self, call, ctx):
        """函数体内任意 Call 节点 -> [(side, event), ...]。

        用于事件分发习语：side=方向名（listen/publish 约定，对 core 不
        透明），event=事件常量键。常量键建议用 ctx.imports 把 evt_mod.X
        归一成「模块路径.常量名」（如 pkg.events.ON_X），保证订阅
        与发布两侧不同 import 写法可 join。订阅装饰器 Call（@on(X)）
        同样经本钩子访问；返回 [] 走通用规则。
        """
        return []

    def ui_facts(self, call, ctx):
        """函数体内任意 Call 节点 -> [(kind, key, receiver), ...]。

        用于资源/字符串绑定习语（UI 资源加载、按名寻节点、动画名播放等）：
        kind 是 custom 自定义的稳定类型名（对 core 不透明，仅作分级标签），
        key 是资源键（资源文件名 / 节点名 / 动画名等），receiver 是调用点的
        源码式 receiver 点分表达式或 None（如 'self.root_widget'）。
        所在类/函数由 scanner 补齐，行结构见 store 的 ui_binding 表。
        """
        return []

    def build_done(self, store, cfg, stats):
        """build 收尾钩子（写完 meta 之后、commit 之前调用）。

        用于 build 期顺带刷新引擎管线之外的项目资源索引（custom 自管库）。
        store 是主库 Store；stats 是 build 的统计 dict（可只读 stage 耗时）。
        默认无操作。
        """
        return None

    def callback_facts(self, stmt, ctx):
        """类体声明语句 -> [(kind, source, target), ...]。

        用于声明式框架约定产生的动态回调边。kind 是供结果分级显示的
        稳定类型名，source 是声明项，target 是目标方法名。core 只负责
        保存和按类/继承/组件共同宿主验证，不认识具体框架语法。
        """
        return []

    def receiver_type_facts(self, call, ctx):
        """调用表达式 -> [(expr, type, confidence, reason), ...]。

        expr 是 receiver 的源码式点分名，type 是类型名；confidence/reason
        对 core 不透明，只用于分级和解释。框架路由、实体查询、类型守卫等
        证据应由 custom 在这里归一，core 不识别任何框架 API。
        """
        return []

    def handler_facts(self, fn, ctx):
        """函数定义 -> [(chan, method, endpoint, confidence, reason), ...]。

        chan 为调用方向，endpoint 通常是宿主类；confidence 建议使用
        verified/inferred。装饰器与 stub 约定由 custom 解释。
        """
        return []

    def project_diagnostics(self, store, cfg):
        """项目级诊断 -> [dict, ...]；core 只负责调用和包装结果。"""
        return []
