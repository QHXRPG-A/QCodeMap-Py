# QCodeMap 架构文档

面向接手开发者：模块职责、数据流、表结构、关键设计决策与其成因。
入门顺序建议：README → 本文 → docs/CUSTOM_GUIDE.md → 直接读代码。

## 1. 总体数据流

```
建库（一次性全量分钟级，随库规模与机器负载波动；增量亚秒）
  collect_files（build.py，磁盘遍历 + include/exclude 规则）
    → scan_file（scanner.py，单文件 ast 扫描 + custom 钩子事实）
    → store.py 落 SQLite（cache/qcodemap.db）
    → pass2（build.py _resolve_comps：组件注入三形态 → comp 边）
    → build_done 钩子（custom 自管资源库刷新，如 ui-refs 的 tbui 索引）
    → rebuild/大删改后 VACUUM（SQLite 删除不还页，防库虚胖）

查询（两阶段）
  阶段1  names 倒排 SQL 拿候选（毫秒）
  阶段2  resolve.py Resolver 语义验证：
           候选行 → 文件级行索引取调用节点 → 接收者类型推导
           （self→外层类MRO+组件边；局部变量→赋值追踪→返回事实；
             mod.Func→import 归一）→ 定义比对 → VERIFIED/CANDIDATE
  缓存   已验证边进 edges 表（mtime 快照 + RESOLVER_VERSION 双失效）
```

## 2. 模块职责

| 模块 | 职责 | 关键点 |
| --- | --- | --- |
| `cli.py` | argparse 入口，全部子命令接线 | 子命令参数用 getattr 兜底（不是每个 subparser 都有全部参数） |
| `config.py` | 配置装载：defaults ← custom ← CLI 三级覆盖 | custom 按文件路径 importlib 动态加载，核心包不 import custom 的任何名字 |
| `defaults.py` | 内建默认配置 | 保证裸核心（无 custom）对任意 Python 目录可用 |
| `hooks.py` | 事实提取钩子协议（FactsHooks/FactContext） | 通用 receiver/handler/diagnostics 协议是框架习语与核心的唯一接缝，见 CUSTOM_GUIDE |
| `scanner.py` | 单文件扫描：names 倒排 + defs/classes/词法 imports + 事实原始行 | Python tokenizer 只收 NAME；semantic-only 不写 names；ast 失败保留可用事实 |
| `store.py` | SQLite 封装：DDL / reader-writer 隔离 / files 登记与级联删除 | reader 用只读 URI；writer 用 WAL；names.file 用 file_id |
| `build.py` | 遍历 + mtime/profile 增量 + pass2 组件解析 | 跨进程单 writer 锁；partial targets 只清理范围内；大库并行 scan |
| `resolve.py` | 两阶段语义验证 + callers/callees/usages + 约定回调 + edges 缓存 | 性能敏感区，见 §4 |
| `path_query.py` | 符号消歧后跨普通调用/RPC 的最短路径 | 只消费通用定义、调用与 RPC 事实，不解释项目语法 |
| `structure.py` | deps/importers/hubs/tree 结构四命令 | 纯 SQL + 内存 modmap，无 ast-grep；coverage 按 scope 内 parse_ok 给 partial |
| `blast.py` | 变更影响面：变更集采集 + 调用链闭包 + 输出投影 | 完整计算后提供 summary/page/full；caller 按 layer、importers 独立分页 |
| `rpc_refs.py` | RPC 双端配对查询：调用 + handler + 组件宿主 + endpoint alias | RPC-INFERRED/HANDLER 分级；stub 严格过滤并解释未命中 |
| `pubsub_refs.py` | 事件双端配对查询：pubsub 表两侧事实 | EVENT-INFERRED/LISTENER 分级；裸事件名按后缀匹配分组（防跨端撞名） |
| `ui_refs.py` | UI 资源绑定双向查询：ui_binding/binding 事实 × custom 资源库 | 声明关系可配 wrapper；EXACT/MULTI/MISS/INDIRECT/UNBOUND/PATTERN/DYNAMIC 分级；普通查询分页 |
| `context.py` | agent 消费面三命令：find_file / get_file_context / context | 全查表无 ast 现扫；context 为一次性项目档案（qcodemap.context/v1） |
| `freshness.py` | 查询前新鲜度契约 | auto/check/off；全仓文件集+mtime 增量刷新；配置/schema/hook 指纹不匹配拒绝旧结果 |
| `fingerprint.py` | 索引指纹：custom 目录全部 .py 内容 + 配置/profile 哈希 | custom 词表改动即失效旧库，防用旧语义回答新问题 |
| `diagnostics.py` | custom 项目诊断汇总 | 核心只聚合统一诊断事实，不识别项目框架 |
| `mcp_server.py` | stdio JSON-RPC MCP server（18 工具） | 自举 UTF-8；日志一律 stderr；全查询 auto refresh，进程内 1 秒节流；ui-refs 工具描述由 custom profile 注入 |
| `custom/` | 项目定制层（config/facts/seeds/ui_profile/tbui_index 五件，仓库仅随附模板说明） | 见 CUSTOM_GUIDE |

## 3. 表结构（store.DDL）

| 表 | 内容 | 规模 |
| --- | --- | --- |
| `files` | id / path / mtime / profile / parse_ok，增量与级联删除的锚点 | 8994 |
| `names` | 标识符倒排（name, file_id, line, col）；semantic-only 不写入 | ~353 万 |
| `defs` / `classes` | 函数/类定义（file, line, class, name；类含 bases 逗号串） | 数万 |
| `imports` | (file, module, name, alias, line, scope)，相对导入归一；结构图消费全部作用域 | ~30 万 |
| `attr` | 属性类型事实：Property 声明（含第二参数类型）+ self.X=构造 | 数万 |
| `global_assign` | 运行时全局注入：genv.X = self → (base, attr, class) | 数百 |
| `ret` | 返回类型事实：return 构造()（module/class.method 两个命名空间） | 数千 |
| `comp_raw` / `comp` | 带稳定 position 的组件注入原始行 / 精确有序宿主边 | 依项目增长 |
| `method_alias` | 源码物理方法名与框架运行时方法名映射，保留 class/file/line 身份 | 依项目增长 |
| `rpc` | 字符串分发 RPC 调用点：(file, line, chan, method, stub)，stub 可 NULL | 数千（见附录 F） |
| `pubsub` | 事件分发两侧事实：(file, line, side, event, func, cls)，event 是 import 归一的常量键 | 数千（见附录 G） |
| `ui_binding` | custom 声明的资源绑定事实：(file,line,class,kind,key,receiver,wrapper,...)，kind 对 core 不透明 | 5.7 万（SEEK 族为主） |
| `binding` | custom 模块级声明归一后的不透明关系：(domain,owner,relation,target,variant,confidence,reason) | 按项目声明增长 |
| `callback_raw` | custom 声明的通用约定回调：(file,line,class,kind,source,target) | 数千 |
| `receiver_fact` | custom 提供的调用点 receiver 类型证据 | 按项目规则增长 |
| `rpc_handler` | custom 提供的 RPC handler、方向、端点与证据等级 | 按项目规则增长 |
| `endpoint_alias` | custom 归一的实现 endpoint 与跨端/运行时等价名 | 按项目规则增长 |
| `edges` | 查询结果缓存（name+def+kind 主键，payload 含 mtimes 与版本） | 按查询增长 |
| `meta` | schema 版本 / 构建统计 | - |

## 4. 查询热路径（读 resolve.py 前必读）

单函数 callers 可能有数百到上万候选行。三个性能层，改动前先理解：

1. **文件级行索引**（`_file_index`）：每文件预分析一次，建
   `行→Call 节点`、`行→外层函数/类区间`、`变量→赋值节点` 三个 dict。
   绝不允许在候选循环里 `ast.walk` 全树（历史教训：1000 万次 walk，43s）。
2. **Resolver 级 SQL 结果缓存**：`_bases_cache` / `_cfiles_cache`（键含
   from_file 消歧维度）/ `_method_cache` / `_imports_cache`。mro_has_method
   递归对同键的重复查询曾达百万次级（120s SQL 时间）。
3. **超高频名防线**（blast.py `MAX_NAME_CANDS=1500`）：`__init__`/`add_timer`
   等万级候选冷验证是分钟级且无产品价值，blast 闭包对此只吃 edges 缓存。

历史优化记录：单高频函数 214s → 文件索引 43s → SQL 缓存 36s →（阈值+缓存）
亚秒。叠加效果，语义结果完全一致（回归验证）。

## 5. 关键设计决策

### 5.1 桩数据化取代 .pyi
`.pyi` 受 Python 模块解析规则约束必须与源码同目录（镜像目录桩实测 0 生效），
无法与源码分离。数据化进 SQLite 后约束消失，同目录规则不再适用。

### 5.2 有序组件、精确基类与解析分区
`comp_raw/comp` 保留装饰器展开顺序。运行时查找按「后注入组件优先，再查
宿主直接定义和基类」执行，匹配逐次 `setattr` 的覆盖语义；组件方法里的
`self.X()` 还会经反向共同宿主解析。基类不再遍历同名类全集，而按同文件、
显式 import、唯一近邻消歧。custom 可用 `file_partition` 声明 client/server
等解析域；不同非空域默认隔离，显式 import、组件和继承边仍可合法跨域。

### 5.3 零误报优先于高召回
解析不了且仍可能命中目标的边降级 CANDIDATE。已经解析到另一定义、分区
不兼容或共同运行时宿主不相交的同名调用直接排除，不再制造限定查询噪声。

### 5.4 RESOLVER_VERSION 缓存失效
edges 缓存键含解析器行为版本（resolve.py 内 RESOLVER_VERSION 常量）。任何影响边判定结果的
改动（消歧规则、递归防线、新的推导路径）必须 +1，否则旧结论污染新查询。
历史教训：修了镜像类消歧后旧缓存仍命中，VERIFIED 停在错误值。

### 5.5 表格目录专用索引策略
路径可通过 `INDEX_PROFILE_RULES` 配置 `full` 或 `semantic-only`。后者保留
结构和语义事实但不写海量 identifier names；对超大纯数据模块只保留顶层
import 与行数。profile 属于配置指纹，变化后必须完整重建。普通增量不
VACUUM，仅 `--rebuild` 或显式 `--vacuum` 执行。

### 5.6 import 解析（build.py `_import_target_files`）
`from M import N` 的 N 可能是子模块（落 `M/N.py`）也可能是 M 内的名字
（落 `M.py`），按序尝试。历史教训：可行性版一律拼 `M.py`，导致全部
attr 形态组件边丢失。模块映射基于全部已索引文件（__init__.py 可能无类）。
imports 表记录模块、类、函数内全部导入供结构图使用；组件装饰器只读取
scope 为空的模块级导入。调用解析与 custom 钩子按调用点恢复模块域和外到内
函数域，同一作用域别名冲突时降级未解析，不产生错误 VERIFIED。

### 5.7 覆盖率契约（P4：失败不静默）
scanner 把 ast 解析失败记为 files.parse_ok=0（事实降级仅保 names）。查询侧
三处透出：resolve 的结果带 `coverage{status, parse_failed, issues}`（全库状态，
issues 只列查询相关坏文件）；
structure 按 scope（deps/importers 的目标集）给 partial 并附 issues 文件
清单；callers 对候选落在坏文件的边注明「所在文件 ast 解析失败」，agent
可区分「索引残缺」与「语义歧义」。对齐 codemap 的 complete/partial 契约。
查询结果的 `coverage` 只表达 AST 覆盖；`index.scope.status` 表达索引是
complete 还是 targeted。旧 `index.coverage` 暂作兼容别名，不再混作查询覆盖率。

### 5.8 统一新鲜度
所有 CLI/MCP 查询按 `auto|check|off` 执行全仓文件集与 mtime 检查；auto
只增量刷新新增、修改、删除文件，check 只报告漂移，off 明确接受旧库。
MCP 的 auto 检查进程内节流 1 秒。schema、配置、profile 或 custom hook
指纹不匹配时拒绝返回旧结果，不在查询路径静默触发完整重建。JSON 统一返回
`index` 元数据。本项目约 9000 文件的新鲜度扫描实测 0.61 秒。
查询 Store 全程只读，不执行 DDL、meta 写入或 edges 缓存写入；增量 build
由单 writer 锁串行，WAL reader 可继续读取构建前的完整快照。

### 5.9 RPC 双端配对（P3-2：字符串分发的约定边）
孵化案例的 RPC 全部是字符串分发（`CallServer('X', ...)` / `CallClient` /
stub 族 / Des 族），调用点无调用表达式，语义验证链路天然断在 RPC 边界。
解法与 §5.7 同哲学——**事实数据化**：custom 分发表
`RPC_DISPATCHERS = {attr: (chan, 方法名参数位, stub参数位)}` 描述「哪些
方法名是分发器、方法名在第几个参数」，scanner 经第五钩子 `rpc_facts` 把
调用点落 rpc 表（chan/method/stub 对 core 不透明）。查询侧 `rpc_refs`
按 method 配 defs 表：stub 已知时 class==stub 的定义精确配对，其余同名
定义列 name-only 候选；输出 RPC-INFERRED（非语义验证，约定推断）分级。
blast 闭包对每个变更函数补查 rpc 表，远端调用点作为入边进闭包（via_rpc
标注）并继续向上传递。已知边界：StubCaller 小写族 stub 名来自运行时
self.stubname（rpc 行 stub=NULL，按方法名配对）；全库 1 处变量方法名
调用跳过；响应回调链（caller/callback 变量形态）不做完整分析。

### 5.10 pubsub 事件配对（P3-1：事件分发的约定边）
事件机制与 RPC 同病：订阅靠装饰器（客户端 `@ListenTo(events.X)`、服务端
`@Subscribe(sconst.X)`）、发布靠 `genv.messenger.Broadcast(events.X)` /
`self.Publish(Cls.X)`，注册表在运行时，静态链路断在两侧。解法同 §5.9：
custom 分发表（PUBSUB_SUBSCRIBERS/PUBSUB_PUBLISHERS，Broadcast 限
receiver 末段 messenger 防误报）+ 第六钩子 `pubsub_facts` 落 pubsub 表。
**join 键是经 import 归一的「模块路径.常量名」而非常量值**——孵化案例
客户端常量是 range(1,1246) 解包整数、服务端 enum 小整数，两端撞号且
无数值语义；scanner 侧新增 import 预扫（FactContext.imports）供钩子把
`events.X` 根名解析成模块路径。归一只要求自洽（同端两侧 import 风格
一致即同键），根名解析失败落 `?`+原文按原文 join（unresolved 降级）。
订阅装饰器 Call 的 receiver 有裸名/模块前缀两形态都认；发布必须带
receiver（裸名 Publish(x.Y) 防误报）。嵌套 def 双访问按
(file,line,side,event) 去重保留内层归属。查询侧 `pubsub_refs` 裸事件名
按后缀匹配分组（客户端与服务端同名常量分开，防跨端撞名误配）；blast
闭包双向穿透（改订阅 handler → 发布点入影响面，反之亦然，via_pubsub
标注）。已知边界：变量首参（全库约 3 处）不做属性回溯；模块级语句里的
pubsub 调用不采集（与 rpc 口径一致）。

### 5.11 通用约定回调、运行时别名与 receiver 展开

core 提供 `callback_facts(stmt, ctx)` 与 `call_callback_facts(call, ctx)`
协议及 callback_raw 存储，分别覆盖声明式回调和函数体内注册回调；timer、
partial 等具体 API 仍由 custom 白名单解释。回调边进入 callers、callees、
path 与 blast-radius，并按 kind 输出 `*-INFERRED`。core 不认识 Property
或 `_on_set_`。孵化案例只在
custom/facts.py 把 `Property("x", ...)` 映射为
`('PROPERTY', 'x', '_on_set_x')`。resolver 对声明类和目标方法类分别求
同类、反向继承、精确 @Components 注入的运行时宿主闭包；交集非空才返回
`PROPERTY-INFERRED`，并附 host class/file 证据。blast 对变更声明把目标回调
列为第一层 `via_callback` 影响。

`method_alias_facts` 把源码物理方法映射到框架改写后的运行时名称；resolver
以物理 file/line 为节点，并保留 source/runtime name，避免装饰器改名后丢失
定义身份。`receiver_type_facts` 可先落伪类型，再由查询期
`expand_receiver_type` 结合 binding 事实展开到具体宿主；这类边输出
`FRAMEWORK-INFERRED`，可被 path 遍历但不冒充纯 AST 直证。

### 5.12 blast 输出投影

闭包始终计算到 depth/MAX_EDGES，`limit` 只约束序列化，不改变计数与后续层。
CLI 默认 full 保持旧行为；MCP 默认 summary。page 模式以 callers/importers
为 section，callers 再按 layer（1=直接、2+=传递）和 offset/limit 分页。

## 6. 已知边界（接手者从这里继续）

- `Space.GetEntity` 等通用方法返回类型是语境近似（seeds 人工标注），
  跨语境会错——REQUIREMENTS §3-1 的「调用方语境投票」是规划解法
- attr 版引用查询（哪个 self.X 读写流经哪个 Property 声明）尚未成命令；
  `_on_set_x` 约定回调已覆盖，但普通属性访问仍需 usages/源码链确认
- MCP server 的 qcodemap_build 全量 rebuild 在 server 进程内执行会阻塞
  该连接约 3.5 分钟（工具级可接受，未做进度上报）
- 函数参数无类型标注时，`store.count(...)` 这类「参数名.方法()」调用
  无法推导参数类型（设计内边界：不做通用类型系统；模块级 `mod.func()`
  与 `self.X`、局部变量构造推导均覆盖）
- 裸项目（无 custom 档案）实测：索引/结构四命令/`mod.func()` 语义链路
  全部可用；modmap 曾仅取含类文件致纯模块项目静默降级（v5 修复，
  2026-08-18 发现于 QCodeMap 自索引实验）

## 7. 回归套件（tests/）

| 脚本 | 断言 | 耗时 |
| --- | --- | --- |
| `test_feasibility.py` | 5/5 语义边零假边（10 文件小样独立建库） | ~1s |
| `test_scale.py` | 全库规模/查询/缓存指标，文件数 ±1% 容差 | 增量秒级（--rebuild 全量） |
| `test_structure.py` | 四命令输出正确性 + hubs 锚点方向 | ~1s |
| `test_blast.py` | 闭包命中已知边（--files 模式，不依赖 svn） | 秒级（缓存热后） |
| `test_mcp.py` | 进程内协议全流程（18 工具）+ 真子进程冒烟 | ~10s |
| `test_p4.py` | 覆盖率契约 + 三新命令 + 懒刷新（临时小库自建） | ~2s |
| `test_rpc.py` | RPC 分发提取 + rpc-refs 配对 + blast 穿透（临时小库） | ~2s |
| `test_pubsub.py` | pubsub 双端提取 + 事件归一 join + blast 穿透（临时小库） | ~2s |
| `test_callback.py` | 通用约定回调 + custom 规则 + 严格宿主验证 + blast | ~2s |
| `test_local_imports.py` | 局部 import 结构边 + 词法隔离 + custom 事件归一 | ~1s |
| `test_ui_refs.py` | ui_facts 提取 + tbui 展开 + 三视图配对 + audit（真实 custom 词表小样） | ~10s |
| `test_p5.py` | partial targets、新鲜度、指纹、profile、receiver、RPC、诊断、UTF-8、blast hunk | ~6s |
| `test_p6.py` | 只读 Store/WAL 快照、单 writer 锁、通用 binding、ui-refs 分页、core/custom 边界 | ~1s |
| `test_p7.py` | 符号消歧、endpoint 宿主/别名、严格 RPC、混合路径、相关覆盖问题 | ~2s |
| `test_p8.py` | 有序组件、方法别名、注册回调、伪类型展开、分区、统一符号接口 | ~2s |

动任何 qcodemap/ 代码后全部回归都跑；只动 custom/ 至少跑 feasibility、scale、p5。
