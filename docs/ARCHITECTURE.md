# QCodeMap 架构文档

面向接手开发者：模块职责、数据流、表结构、关键设计决策与其成因。
入门顺序建议：README → 本文 → docs/CUSTOM_GUIDE.md → 直接读代码。

## 1. 总体数据流

```
建库（一次性 207s / 增量亚秒）
  collect_files（build.py，磁盘遍历 + include/exclude 规则）
    → scan_file（scanner.py，单文件 ast 扫描 + custom 钩子事实）
    → store.py 落 SQLite（cache/qcodemap.db）
    → pass2（build.py _resolve_comps：@Components 三形态 → comp 边）
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
| `hooks.py` | 事实提取钩子协议（FactsHooks/FactContext） | 框架习语与核心的唯一接缝，见 CUSTOM_GUIDE |
| `scanner.py` | 单文件扫描：names 倒排 + defs/classes/imports + 事实原始行 | utf-8→gbk 解码链；bytes 字面量剔除；降采样（表格目录每标识符只记首处）；ast 失败仅保 names |
| `store.py` | SQLite 封装：DDL / files 登记（mtime）/ file 级联删除 | names.file 用 file_id 整型（大表省空间），其余事实表 file 列直接存路径 |
| `build.py` | 遍历 + mtime 增量 + pass2 组件解析 | INCLUDE_PATHS 路径级放行优先于目录名排除；include 路径内豁免文件级排除（origin 明文版） |
| `resolve.py` | 两阶段语义验证 + callers/callees/usages + edges 缓存 | 性能敏感区，见 §4 |
| `structure.py` | deps/importers/hubs/tree 结构四命令 | 纯 SQL + 内存 modmap，无 ast-grep；coverage 按 scope 内 parse_ok 给 partial |
| `blast.py` | 变更影响面：变更集采集（svn st/--rev/--files）+ 调用链闭包 | 闭包复用 Resolver；超高频名只吃缓存；RPC 边穿透（via_rpc 标注） |
| `rpc_refs.py` | RPC 双端配对查询：rpc 表调用点 + defs 表 handler | RPC-INFERRED/HANDLER 分级；stub 精确配对优先，方法名兜底 |
| `context.py` | agent 消费面三命令：find_file / get_file_context / context | 全查表无 ast 现扫；context 为一次性项目档案（qcodemap.context/v1） |
| `mcp_server.py` | stdio JSON-RPC MCP server（13 工具） | 日志一律 stderr——stdout 是协议流，print 即损坏；目标文件型工具带懒刷新 |
| `custom/` | 项目定制层（config/facts/seeds 三件，仓库仅随附模板说明） | 见 CUSTOM_GUIDE |

## 3. 表结构（store.DDL）

| 表 | 内容 | 规模 |
| --- | --- | --- |
| `files` | id / path / mtime / parse_ok，增量与级联删除的锚点；parse_ok=0 即 ast 失败（索引仅 names） | 8925 |
| `names` | 标识符倒排（name, file_id, line, col），阶段1 唯一大表 | ~765 万（降采样后） |
| `defs` / `classes` | 函数/类定义（file, line, class, name；类含 bases 逗号串） | 数万 |
| `imports` | (file, module, name, alias)，相对导入在建库时归一为绝对 | ~30 万 |
| `attr` | 属性类型事实：Property 声明（含第二参数类型）+ self.X=构造 | 数万 |
| `global_assign` | 运行时全局注入：genv.X = self → (base, attr, class) | 数百 |
| `ret` | 返回类型事实：return 构造()（module/class.method 两个命名空间） | 数千 |
| `comp_raw` / `comp` | @Components 原始行 / 解析后的 host↔comp 边 | 3831/3658 |
| `rpc` | 字符串分发 RPC 调用点：(file, line, chan, method, stub)，stub 可 NULL | 数千（见附录 F） |
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

### 5.2 同名类并集语义（不是消歧后取一）
`_class_files` 返回**全部**定义文件，排序「同文件 > 同目录 > 同顶层 target
（gclient/gserver 镜像类场景）> 字典序」，方法查找沿排序依次尝试。
依据：@Components 是 setattr 拷贝，宿主方法来自每一个组件类；
gclient/gserver 的 CombatAvatar 是镜像实现，调用方语境决定用哪份。
历史教训：早期版本消歧失败返回 None，GetTeammateInfo 的 VERIFIED 边
从 21 掉到 6。

### 5.3 零误报优先于高召回
解析不了的边降级 CANDIDATE 并注明「解析到同名另一定义 X:Y」——这个注明
本身就是信息（如 HasSkywingForPara 基类版/子类重写版的区分）。

### 5.4 RESOLVER_VERSION 缓存失效
edges 缓存键含解析器行为版本（resolve.py 顶部常量）。任何影响边判定结果的
改动（消歧规则、递归防线、新的推导路径）必须 +1，否则旧结论污染新查询。
历史教训：修了镜像类消歧后旧缓存仍命中，VERIFIED 停在错误值。

### 5.5 表格目录专用索引策略
gclient/data、gserver/data 的 .py 是 bindict 二进制（bytes 字面量），
`*_origin.py` 是其明文版本。处理链：
- INCLUDE_PATHS 路径级放行（优先于 'data' 目录名排除）；
- include 路径内豁免 `*_origin.py` 文件级排除（明文版才有语义可查）；
- token 化前剔除 bytes 字面量（转义序列是伪 token 洪流：曾致 names
  560万→2894万、库 1.5GB）；
- 降采样：每文件每标识符只记首处（表格 key 重复上万行，行级重复纯是体积）；
- rebuild 后 VACUUM（SQLite 删除不还页，实测 1.5GB 虚胖缩回 441MB）。

### 5.6 import 解析（build.py `_import_target_files`）
`from M import N` 的 N 可能是子模块（落 `M/N.py`）也可能是 M 内的名字
（落 `M.py`），按序尝试。历史教训：可行性版一律拼 `M.py`，导致全部
attr 形态组件边丢失。模块映射基于全部已索引文件（__init__.py 可能无类）。

### 5.7 覆盖率契约（P4：失败不静默）
scanner 把 ast 解析失败记为 files.parse_ok=0（事实降级仅保 names）。查询侧
三处透出：resolve 的结果带 `coverage{status, parse_failed}`（全库口径）；
structure 按 scope（deps/importers 的目标集）给 partial 并附 issues 文件
清单；callers 对候选落在坏文件的边注明「所在文件 ast 解析失败」，agent
可区分「索引残缺」与「语义歧义」。对齐 codemap 的 complete/partial 契约。

### 5.8 懒刷新（P4：替代后台守护）
MCP 目标文件型工具（callers/callees/deps/importers/get_file_context/blast）
查询前 drift_check：目标文件 mtime 与库内漂移 → 先增量 build（单文件亚秒）
再查。上限 1000 文件（大目录 scope 放弃检测）。刷新走独立连接且在查询
Store 打开之前（并发写锁库）。usages/hubs/tree/context/rpc_refs 无明确
目标文件，不接入（全库 stat 不成比例）。结果附 `refresh{files, elapsed}` 摘要。

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

## 6. 已知边界（接手者从这里继续）

- `Space.GetEntity` 等通用方法返回类型是语境近似（seeds 人工标注），
  跨语境会错——REQUIREMENTS §3-1 的「调用方语境投票」是规划解法
- pubsub `ListenTo(ON_X)` 事件分发边静态不可达，P3 规划事件常量配对表
- attr 版引用查询（哪个 self.X 访问流经哪个 Property 声明）尚未成命令，
  comp 表只有 host→comp 方向，组件反查宿主声明需走 genv/种子链人工推
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
| `test_mcp.py` | 进程内协议全流程（13 工具）+ 真子进程冒烟 | ~10s |
| `test_p4.py` | 覆盖率契约 + 三新命令 + 懒刷新（临时小库自建） | ~2s |
| `test_rpc.py` | RPC 分发提取 + rpc-refs 配对 + blast 穿透（临时小库） | ~2s |

动任何 qcodemap/ 代码后七个全跑；只动 custom/ 跑 feasibility + scale 即可。
