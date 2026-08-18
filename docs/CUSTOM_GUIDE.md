# QCodeMap 二次开发指南

面向两类读者：
- **换项目适配者**：把 QCodeMap 用到自己的 Python 项目（§1-2）
- **语义扩展者**：为本项目补充新的框架习语/查询能力（§3-5）

核心原则：**qcodemap/ 是项目无关的引擎，custom/ 是唯一的定制层**。
核心包不 import custom 的任何具体名字，一切经钩子协议与配置装载连接。

## 1. 换项目适配：三个文件

把 `custom/` 拷一份改三处即可，核心包零改动：

### 1.1 custom/config.py —— 项目档案

```python
ROOT = r'D:\your\project'          # 被分析项目根

TARGETS = ['src', 'lib']           # 纳入索引的顶层目录（空 = ROOT 全量）
EXCLUDE_DIRS = {'__pycache__', ...}  # 目录名排除（任意层级命中即剪枝）
EXCLUDE_FILES = ['*_generated.py']   # 文件名排除（fnmatch）

# 路径级放行：优先级高于 EXCLUDE_DIRS，用于捞回被目录名规则误杀的路径。
# 单文件 include 也支持（会作为独立遍历根）。
INCLUDE_PATHS = ['src/generated_tables']

# 表格/产物目录专用：bytes 字面量剔除 + 每文件每标识符只记首处
NAMES_DOWNSAMPLE_PREFIXES = ['src/generated_tables/']
```

多项目共存：`--custom <目录>` CLI 参数或 `QCODEMAP_CUSTOM` 环境变量改指
不同档案；`--db` 指定不同库文件。

### 1.2 custom/facts.py —— 框架习语钩子（可选）

没有框架习语就删掉此文件，只剩通用事实（self.X=构造、return 构造）。
有的话继承 `qcodemap.hooks.FactsHooks` 覆写四个方法（详见 §3）。

### 1.3 custom/seeds.py —— 人工种子（可选）

ast 扫不出来的类型事实，优先级最高（覆盖自动事实）：

```python
RET_SEEDS = {
    ('模块名', '函数名'): '返回类型',     # 模块级函数
    ('类名', '方法名'): '返回类型',       # 实例方法（语境近似须注释）
}
ATTR_SEEDS = {
    ('类名', '属性名'): '类型',           # 如组件 mixin 里访问宿主属性
}
```

## 2. 配置装载优先级

```
defaults.py（兜底） ← custom/config.py 覆盖 ← CLI --root/--targets/--db（最高）
seeds：custom 键覆盖内置同键种子（dict 合并语义）
```

custom 文件按路径 importlib 动态加载（`qcodemap_custom_config` 等模块名），
缺失哪个文件就用哪个默认——所以三个文件全部可选。

## 3. 扩展框架语义：FactsHooks 七个钩子

先在目标代码里确认习语的真实形态（`grep -n` 看实际写法），再实现：

```python
from qcodemap.hooks import FactsHooks, FactContext

class MyFacts(FactsHooks):
    def assign_value_type(self, node, ctx):
        """Assign 节点 -> 伪类型名或 None。
        典型：self.X = glib.Y -> 'glib.Y'（resolver 查 global_assign 还原）。
        返回 None 则走核心通用规则（构造调用取类名）。"""

    def class_facts(self, cd, ctx):
        """ClassDef 级事实（装饰器解析）-> [(表名, 行元组), ...]。
        表名限定 comp_raw；典型：@MyInject(A, mod.B) -> 组件注册。"""

    def class_stmt_fact(self, stmt, ctx):
        """类子树内单条语句 -> (表名, 行元组) 或 None。
        表名限定 attr / global_assign。
        典型：Declare("x", Type) 声明 -> attr；glib.X = self -> global_assign。"""

    def importall_members(self, host):
        """importall 场景：宿主对应的组件类名列表。
        典型：宿主组件类命名规则 -> [host + 'Member']（孵化案例的约定）。"""

    def rpc_facts(self, call, ctx):
        """函数体内任意 Call 节点 -> [(chan, method, stub), ...]。
        字符串分发 RPC/远端调用提取：按分发表匹配 attr 与参数位，返回
        (通道名, 方法名字符串, 目标类名或 None)。详见孵化案例的
        RPC_DISPATCHERS 写法与 §3.1。"""

    def pubsub_facts(self, call, ctx):
        """函数体内任意 Call 节点 -> [(side, event), ...]。
        事件分发提取（订阅装饰器/发布调用），event 建议用 ctx.imports
        归一成「模块路径.常量名」保证两侧可 join。详见 §3.2。"""

    def callback_facts(self, stmt, ctx):
        """类体声明 -> [(kind, source, target), ...]。
        用于声明式框架产生的动态回调；core 只做共同运行时宿主验证，
        不认识声明函数名或回调命名规则。详见 §3.3。"""
```

事实行必须与 store 表列一致：
- `attr`：`(rel, class, attr_name, type_name)`
- `global_assign`：`(base_name, attr, class, rel)`
- `comp_raw`：`(rel, host, kind, value)`，kind ∈ ref/attr/importall
  （ref=同文件/裸名；attr=mod.Cls 前缀；importall=*pkg.importall() 星调用）
- `rpc`：`(rel, line, chan, method, stub)`（stub 可 None；由 rpc_facts
  钩子产出，scanner 负责补 rel/line）
- `pubsub`：`(rel, line, side, event, func, cls)`（由 pubsub_facts 钩子
  产出 (side, event)，scanner 补定位列与所在函数/类）
- `callback_raw`：`(rel, line, class, kind, source, target)`（由
  callback_facts 产出后三项，scanner 补声明定位与所在类）

### 3.1 字符串分发 RPC 的分发表写法

目标项目的 RPC 若是 `x.Dispatch('Method', ...)` 形态（方法名是字符串
参数，无调用表达式），用 `rpc_facts` 钩子 + 一张分发表覆盖：

```python
# {attr 名: (通道名, 方法名参数位 0 基, stub 参数位或 None)}
RPC_DISPATCHERS = {
    'CallServer': ('C2S', 0, None),          # self.CallServer('X', ...)
    'CallClient': ('S2C', 0, None),
    'CallShardStubHostnum': ('STUB', 3, 2),  # (host, key, stub, rpc)
}

class MyFacts(FactsHooks):
    def rpc_facts(self, call, ctx):
        func = call.func
        if not (isinstance(func, ast.Attribute)
                and func.attr in RPC_DISPATCHERS):
            return []
        chan, m_idx, s_idx = RPC_DISPATCHERS[func.attr]
        args = call.args
        if len(args) <= m_idx:
            return []
        marg = args[m_idx]
        if not (isinstance(marg, ast.Constant) and isinstance(marg.value, str)):
            return []   # 变量方法名跳过
        stub = None
        if s_idx is not None and len(args) > s_idx:
            sarg = args[s_idx]
            if isinstance(sarg, ast.Constant) and isinstance(sarg.value, str):
                stub = sarg.value
        return [(chan, marg.value, stub)]
```

配对查询用 `qcodemap rpc-refs <方法名> [--stub X]`；噪音通道删表内
单行即降级。参数位以目标项目分发器的**真实签名**为准（grep 定义处核实，
勿凭调用样例推断——同一族可能带不同前缀参数）。

### 3.2 事件分发 pubsub 的分发表写法

目标项目的事件机制若是「订阅装饰器 + 发布方法」形态（`@ListenTo(EV_X)` ↔
`messenger.Broadcast(EV_X)`），用 `pubsub_facts` 钩子覆盖。要点：**join 键
用 import 归一后的常量全路径**（`ctx.imports` 是 scanner 预扫的本文件
import 映射 {本地名: 目标点分路径}），不要用常量值——常量值常是整数
序号且两端撞号；订阅装饰器的 receiver 有裸名/模块前缀两形态都要认，
发布必须带 receiver 防与本地同名函数混淆：

```python
SUBSCRIBERS = {'ListenTo': 0, 'Subscribe': 0}   # attr -> 事件参数位
PUBLISHERS = {'Publish': 0, 'Broadcast': 0}
BROADCAST_RECEIVER_ENDS = ('messenger',)        # Broadcast 限 receiver

class MyFacts(FactsHooks):
    def pubsub_facts(self, call, ctx):
        func = call.func
        if isinstance(func, ast.Attribute):
            attr, receiver = func.attr, func.value
        elif isinstance(func, ast.Name):
            attr, receiver = func.id, None
        else:
            return []
        if attr in SUBSCRIBERS:            # 订阅：receiver 两形态都认
            out = []
            for arg in call.args:
                ev = self._event_key(arg, ctx)
                if ev:
                    out.append(('listen', ev))
            return out
        if attr in PUBLISHERS and receiver is not None:
            end = receiver.attr if isinstance(receiver, ast.Attribute)                 else getattr(receiver, 'id', None)
            if attr == 'Broadcast' and end not in BROADCAST_RECEIVER_ENDS:
                return []
            ev = self._event_key(call.args[0], ctx) if call.args else None
            return [('publish', ev)] if ev else []
        return []

    @staticmethod
    def _event_key(arg, ctx):
        # events.ON_X -> '模块路径.ON_X'；根名未解析 -> '?'+原文（原文 join）
        ref = dotted(arg)
        if not ref or '.' not in ref:
            return None
        root, rest = ref.split('.', 1)
        target = (ctx.imports or {}).get(root)
        return '%s.%s' % (target, rest) if target else '?' + ref
```

配对查询用 `qcodemap pubsub-refs <事件名>`（裸名后缀匹配分组，完整键
全等）；与机制无关的同名方法（如网络 topic pubsub）在 EXCLUDE_FILES
按文件排除。

**转发别名要归一**：若常量类存在 re-export（如 B 模块 `from A import Cls`
后大家混用 A/B 两种前缀 import），同一事件会裂成两个键、配对率骤降——
维护一张 `{别名前缀: 真实前缀}` 映射在 _event_key 里改写（孵化案例
gshare.consts→gserver.sconst，1686 个文件混用，实测不归一则同事件
订阅裂成两半）。

### 3.3 声明式约定回调

框架若由类体声明动态调用命名回调，只在 custom 识别具体语法：

```python
class MyFacts(FactsHooks):
    def callback_facts(self, stmt, ctx):
        # Declare('state', ...) -> _on_set_state(old)
        name = parse_declaration_name(stmt)
        if name is None:
            return []
        return [('PROPERTY', name, '_on_set_%s' % name)]
```

core 将 `kind` 作为结果标签（如 `PROPERTY-INFERRED`），并仅在声明类与
目标方法类属于同类、继承链或共享精确 `@Components` 宿主时成边。没有
共同宿主证据的同名方法直接丢弃，避免跨玩法误连。

### 参考：孵化案例的四类习语（真实 custom/facts.py 的抽象）

| 习语 | 事实 | 换框架时的对应物 |
| --- | --- | --- |
| `Property("x", Type)` 类体声明 | attr + callback_raw（`_on_set_x`） | 任意声明式属性/动态回调 |
| `genv.X = self` | global_assign | 任意全局命名空间注入 |
| `@Components(A, mod.B, *pkg.importall())` | comp_raw 三形态 | 任意组合/注入装饰器 |
| `{Host}Member` 命名约定 | importall_members 钩子 | 你的组件命名规则 |
| `CallServer('X')` 等 7 族字符串分发 | rpc（RPC_DISPATCHERS 分发表） | 任意字符串分发 RPC/消息总线 |
| `@ListenTo`/`@Subscribe` ↔ `Broadcast`/`Publish` 事件分发 | pubsub（订阅/发布分发表 + import 归一事件键） | 任意事件总线/信号槽 |

## 4. 扩展查询能力

### 4.1 只读查询（不改核心）

大多数需求 = 组合现有表。写在独立脚本里 import qcodemap：

```python
from qcodemap import config as cmod
from qcodemap.store import Store

cfg = cmod.load_config()
store = Store(cfg.db_path)
rows = store.con.execute(
    'SELECT host, host_file, comp, comp_file FROM comp WHERE comp=?',
    ('MyComp',)).fetchall()
```

### 4.2 新增 CLI 子命令

1. 逻辑函数放新模块（或 structure.py），返回 dict（json_out 参数风格对齐）；
2. cli.py 加 subparser + 分发分支（参数读取用 getattr 兜底）；
3. tests/ 加回归脚本（断言至少一条已知正确结果）。

### 4.3 动 resolve.py 语义推导的检查单

- 行为变化（边判定结果可能不同）→ `RESOLVER_VERSION + 1`，必做
- 性能：新推导路径在候选循环里吗？绝不允许逐候选 `ast.walk` 全树，
  需要新索引就进 `_file_index` 并在文档记录
- 回归五个全跑；feasibility 的 5/5 边是底线

## 5. 常见任务速查

| 任务 | 入口 |
| --- | --- |
| 加索引范围 | custom/config.py 的 TARGETS / INCLUDE_PATHS，然后 `build --rebuild` |
| 排除某目录 | EXCLUDE_DIRS（目录名，任意层级）；精确路径用删 INCLUDE 而非加排除 |
| 表格目录入索引 | INCLUDE_PATHS + NAMES_DOWNSAMPLE_PREFIXES（见 ARCHITECTURE §5.5） |
| 某方法找不到 VERIFIED 边 | 先 usages 看定义点是否唯一；同名多处属正常降级 |
| 某返回类型解析不出 | custom/seeds.py 加 RET_SEEDS（语境近似写注释） |
| 组件方法归属 | comp 表：`SELECT host FROM comp WHERE comp=?` |
| 缓存疑似过期 | 改动文件后 build 即失效；解析器升级自动整体失效 |

## 6. 发布与部署注意

- `cache/` 与 `__pycache__` 不入库；qcodemap/ + custom/ + tests/ + docs/
  随仓库走
- MCP 注册条目（项目 .codex/mcp.json）里的 `cwd` 是绝对路径，换机器要改
- 被分析项目完全无感知（零写入）；索引库删了重建即恢复
