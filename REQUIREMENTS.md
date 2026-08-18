# QCodeMap 需求开发文档

> 定位：**通用 Python 代码库的语义级导航工具**（框架习语经 custom/ 插拔，
> 核心项目无关）。孵化案例为 Messiah 游戏项目（内部代码库），本文实测数据
> 与路径示例均出自该案例。

- 版本：v0.9（P3-1 落地：pubsub 事件配对 pubsub-refs + blast 穿透）
- 日期：2026-08-18
- 状态：**P0~P2 已完成**（实测见附录 C/D）；**P4 第一轮（§2.2 第 1/2/3/5 项）
  已完成**（附录 E），剩余 diff（第 4 项）与 HTTP serve（第 6 项，有真实
  消费场景再做）；**P3-2 RPC 双端跳转已完成**（附录 F）；**P3-1 pubsub
  事件配对已完成**（附录 G），P3 其余两项（种子自动化/attr-refs）按需并行
- 文档：[README.md](README.md)（快速上手）·
  [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)（模块/数据流/决策记录）·
  [docs/CUSTOM_GUIDE.md](docs/CUSTOM_GUIDE.md)（二次开发指南）

---

## 1. 项目创建的意图

### 1.1 要解决的问题

孵化案例 Messiah 游戏项目（内部代码库，核心代码约 6000 文件 / 20000+ 含产物）
的代码调查长期依赖两种手段，各有硬伤：

| 手段 | 硬伤（实测数据） |
| --- | --- |
| grep 关键词检索 | 只认字符串不认语义；同名函数/镜像目录（release_data）造成大量噪音；无法回答「改这个函数影响谁」 |
| codemap CLI（v4.4.0） | 依赖分析是 **import 级**，无调用链能力；全仓 --deps 触发 ast-grep 30 秒超时 |
| jedi + 类型桩（前期 PoC） | get_references 有 30 文件解析安全阀，2 万文件库上跨文件边全部静默丢失；单函数查询 12~17 秒 |

而游戏框架的动态语义（`@Components` setattr 拷贝、`genv` 运行时注入、
`Property` 声明、entitylist 反射）使得 **PyCharm 级的调用链分析**在通用工具上
天然残缺——前期实验证明：无桩 jedi 解析 0 条跨文件边，配桩后 18 条（+18），
但桩机制又被 `.pyi` 必须与源码同目录的规则锁死，无法与源码分离。
这类习语并非孤例，凡重动态语义的 Python 项目（Web 框架的服务定位器、
插件的注册表注入等）都有同构问题，故核心引擎按项目无关设计。

### 1.2 项目定位

**QCodeMap = 桩数据化 + 预建倒排索引 + 两阶段查询 + 传统 codemap 结构能力**，一句话：

> 把「框架语义」从类型桩文件降格为索引库中的数据行，把「找引用」从现扫变成查表，
> 为大型 Python 代码库提供秒级、可依赖、与源码物理分离的调用链与结构查询工具。
> 框架习语经 custom/ 钩子插拔，换项目零改核心。

### 1.3 设计原则（从可行性验证中固化）

1. **纯 stdlib**：只用 ast / sqlite3 / re / os / pathlib。不引入 jedi、codemap 二进制依赖
   （jedi 桩版结论已被本方案取代，但保留其基准测试地位）。
2. **索引与源码分离**：全部索引产物落在 QCodeMap 目录的 `cache/`，被分析项目
   目录零写入、版本管理零感知。索引可再生，删除即重建。
3. **零误报优先于高召回**：解析不了的边宁可降级输出「同名候选（未验证）」，不给假边。
   可行性验证中 5/5 边全部命中的前提是没有一条错误边。
4. **两阶段架构**：阶段 1 倒排查表（毫秒）拿候选，阶段 2 纯 ast 语义解析验证。
   该架构已被验证为万文件级代码库下的正确形态（PyCharm 同构）。

---

## 2. 开发方向

### 2.1 架构总览

```
┌─ 建索引（一次性 43s / 增量秒级）───────────────────────────┐
│ scan: 遍历核心目录 .py（复用排除清单）                      │
│  ├ names    标识符倒排：name → file:line:col   （阶段1）   │
│  ├ defs/classes/imports   定义与导入图                     │
│  └ facts    桩数据化四类语义事实：                          │
│     ├ Property('x', default) 声明 → 类属性类型             │
│     ├ @Components(...) 注册 → 宿主↔组件 MRO 补边           │
│     │   （含 *pkg.importall() 星调用：读包 __init__ 清单）  │
│     ├ genv.X = self / = 构造 → 运行时全局类型              │
│     └ self.X = 构造 / return 构造 → 属性/返回类型          │
│ store: SQLite（cache/qcodemap.db，实测 6052 文件 235MB）   │
└────────────────────────────────────────────────────────────┘
┌─ 两阶段查询 ───────────────────────────────────────────────┐
│ 阶段1  SQL 查表拿候选（实测 1~25ms）                        │
│ 阶段2  候选位置 → ast 定位调用表达式 → 局部变量数据流追踪    │
│        → 类型查 facts → 沿 bases+组件 MRO 找方法定义        │
│        → 定义比对，边成立/降级                              │
└────────────────────────────────────────────────────────────┘
```

### 2.2 分期计划

**P0 —— 正式工程骨架（先行，~1 天）**

- `qcodemap/` 包：`store.py`（SQLite 封装）、`scanner.py`（单文件 ast 扫描）、
  `build.py`（目录遍历 + 增量更新 + @Components pass2 跨文件解析）。
- 已有可行性脚本中的实现可直接提炼（注意修正 §3 已知问题）。
- CLI：`python -m qcodemap build [--root X] [--targets ...] [--rebuild]`。

**P1 —— 查询能力（核心价值，~3 天）**

- `resolve.py`：两阶段查询解析器（可行性脚本已验证的完整链路产品化）。
- CLI：
  - `qcodemap callers <file> <func>` —— 谁调用这个函数（核心场景）。
  - `qcodemap callees <file> <func>` —— 这个函数调了谁。
  - `qcodemap usages <symbol>` —— 标识符全仓出现点（带验证标记）。
- 输出分级：`VERIFIED`（语义验证边）/ `CANDIDATE`（同名未验证）/ 排除镜像目录。
- `edges` 缓存表：已验证边落库，二次查询直接命中，mtime 失效。

**P2 —— 传统 codemap 能力（平价替代，~2 天）**

- `qcodemap tree [--depth N]`：目录结构+体积统计（排除清单同源）。
- `qcodemap deps <目录>`：import 级依赖图（ast 提取，无 ast-grep 超时问题）。
- `qcodemap importers <file>`：谁 import 这个文件、枢纽判定。
- `qcodemap hubs`：import 入度排行（对标 codemap 的枢纽识别）。
- 输出风格对齐 codemap 的 JSON schema（`codemap.analysis/v1` 风格），
  降低 AI 侧消费习惯的迁移成本。

**P3 —— 语义扩展（按需，与 P4 并行可做）**

- pubsub 事件配对表：`ListenTo(ON_X)` ↔ `Broadcast/Pub(ON_X)` 常量 join，
  补静态不可达的事件分发边（jedi 版同样做不到的天花板）。
  ——**已完成**（2026-08-18，P3-1，见附录 G）：pubsub 事实表 + 第六钩子
  `pubsub_facts`（订阅/发布分发表在 custom/facts.py）+ `pubsub-refs` 命令/
  MCP 工具（EVENT-INFERRED/LISTENER 分级）+ blast-radius 双向事件穿透。
  join 键是经 import 归一的「模块路径.常量名」而非常量值（客户端常量是
  range 解包整数序号、服务端 enum 小整数，两端撞号且无数值语义）。
- RPC 桩↔handler 约定映射（客户端 stub 名 ↔ 服务端注册）。
  ——**已完成**（2026-08-18，P3-2，见附录 F）：rpc 事实表 + 第五钩子
  `rpc_facts`（分发表在 custom/facts.py，7 通道族）+ `rpc-refs` 命令/
  MCP 工具（RPC-INFERRED/HANDLER 分级配对）+ blast-radius 闭包穿透
  RPC 边界。注意：本项目 RPC 是字符串分发（`CallServer('X')` 等）而非
  显式桩类，配对按方法名（stub 类名已知时精确配对）。
- 种子事实的采集自动化：`Space.GetEntity` 等返回类型目前靠人工种子，
  扩展为「docstring 类型标注 + 调用方语境投票」半自动采集。
- attr-refs 命令（属性版 callers）：cur_titles 实测暴露的缺口——attr 原始
  出现大头是字典键，按 receiver（self/genv.avatar/owner/...）分类并沿
  comp 反查宿主 Property 声明，同名声明消歧。

**P4 —— 产品化与 agent 生态对齐（2026-08-18 定；第一轮同日落地，见附录 E）**

对标 codemap（https://github.com/JordanCoin/codemap）只看 Python 维度的
差距复盘：分析深度已反超（codemap 对 py 仅 ast-grep import 级，无调用链；
QCodeMap 有语义调用图/框架事实/VERIFIED 分级/函数级影响面），缺口集中在
产品化与 AI 消费体验。按价值排序：

1. **覆盖率契约硬化**（小）：scanner 记 `parse_ok` 标记，callers/callees
   结果透出「验证时有 N 个文件 ast 失败」——ast 失败文件目前静默降级为
   仅 names，结果不告知索引残缺（对齐 codemap complete/partial 契约）。
   ——**已完成**（附录 E）：files.parse_ok 落库（SCHEMA_VERSION 2），
   resolve 全库口径 coverage、structure 按 scope 给 partial+issues、
   callers 坏文件候选专门注明（ARCHITECTURE §5.7）。
2. **MCP 工具面补齐**（小）：9→12 工具。补 `find_file`（模糊路径搜索）、
   `get_file_context`（单文件的 defs+importers+deps 打包一次给 AI，省多
   次往返）、`context`（见 3）。
   ——**已完成**（附录 E）：三工具 + CLI find / file-context / context
   子命令（qcodemap/context.py）。
3. **context 聚合命令**（小）：结构+枢纽+统计的一次性机器可读项目档案
   （对标 `codemap context --for X --compact`），AI 会话冷启动注入用。
   ——**已完成**（附录 E）：qcodemap.context/v1——stats+top_dirs+hubs+
   external_top+coverage，compact 截断；intent/skills 等字段按「明确
   不追齐」不实现。
4. **依赖漂移对比**（中）：`diff` 命令——两版本/两快照间 deps/hubs 漂移
   报告（重构前后枢纽变化、新增耦合），与 blast-radius 的「变更→影响」
   互补成双向。（P4 第二轮）
5. **懒刷新**（小，替代 codemap watch 守护）：MCP 查询入口发现目标文件
   mtime 漂移时自动增量 build（0.5s 级），比后台守护进程更贴合现有架构。
   ——**已完成**（附录 E）：目标文件型工具（callers/callees/deps/
   importers/get_file_context/blast）查询前 drift_check（上限 1000 文件），
   漂移即增量重建，结果附 refresh 摘要（ARCHITECTURE §5.8）。
6. **HTTP serve**（中，有真实消费场景再做）：127.0.0.1 API，供脚本/网页
   消费（目前仅 stdio MCP）。

明确不追齐的（场景不符，防 scope 膨胀）：远程仓库浅克隆分析（本库永远在
本地 svn）、budgets/guidance/routing 项目配置与会话 hooks（宿主 ZCode +
skills 已覆盖）、handoff/working-set/activity 跨 agent 交接、skyline 可视化。

### 2.3 明确不做（防 scope 膨胀）

- 不做通用 Python 类型系统（闭包、泛型容器、多态推导）——框架模式语义已够用。
- 不做编辑器集成 / LSP server——查询 CLI + AI 消费是目标形态。
- 不 fork jedi / codemap——codemap 保持独立 CLI 共存，QCodeMap 是自研补充。
- 不处理 `.pyi`——桩已数据化，项目内不再生成任何文件。

---

## 3. 已知问题与风险（来自可行性验证）

| # | 问题 | 缓解方向 |
| --- | --- | --- |
| 1 | `GetEntity` 等通用方法返回类型依赖「语境种子」（如按 toplogo 语境标 AvatarSceneNode），跨语境会错 | 返回类型种子标注置信度；或按调用方属性访问集做局部形状推断；P1 先维护显式种子文件 `seeds.py` |
| 2 | 同名类多文件（如 `CombatAvatarMember` 在多玩法模块重复定义）当前取首定义 | defs/classes 查询一律带 file 维度；同名时降级 CANDIDATE 并列出全部定义点 |
| 3 | pubsub `ListenTo` 事件分发边静态不可达（真值只能运行时拿） | 已由 P3-1 配对表补约定边（附录 G），输出标注 `EVENT-INFERRED` |
| 4 | 2 万全量文件下 names 表 558 万行 / 235MB，但未测含 facts 的完整库体量 | P0 完成后立即跑全量 build 实测，预期 <400MB |
| 5 | 数据目录排除（data/data_lang）按目录名硬编码，特殊路径可能漏 | 排除清单提为常量 + CLI 参数覆盖；对 `*_origin.py` 单文件级排除 |
| 6 | 增量更新的正确性（删除/改名文件的级联清理）尚未实现 | store 层按 file_id 级联删除（DDL 已预留） |

---

## 4. 最终应实现的功能（验收标准）

### 4.1 功能验收

1. **建索引**：`qcodemap build` 对核心七目录（gclient/gserver/gshare/HelenFramework/
   SunshineSDK/Montage/UGC）全量建库 ≤90 秒（实测 43s）；增量更新（单文件改动）
   ≤5 秒；缓存固定在 QCodeMap 目录的 `cache/`，被分析项目目录零写入。
2. **调用链查询**：`qcodemap callers <file> <func>`——
   - 可行性基准五条边（RefreshAiTakeoverToplogo ×2、RemoveDummyEntity ×2、
     GetTeammateInfo ×1）全部命中且零假边；
   - 单函数查询（含语义验证）≤5 秒，edges 缓存命中时 ≤0.5 秒；
   - 每条边带 `VERIFIED/CANDIDATE` 分级与调用方函数名（经 ast 映射外层函数）。
3. **结构查询**：`tree/deps/importers/hubs` 四命令输出与 codemap 对应能力一致
   （抽样对比 import 入度 Top25 名单相符），且无 30 秒超时限制。
4. **可维护性**：种子事实集中在 `seeds.py` 可注释可覆盖；索引可 `--rebuild` 重建；
   排除清单单点定义。

### 4.2 质量验收

- 回归测试：`test_feasibility.py`（语义链路 5/5）与 `test_scale.py`（规模基准）
  纳入 `tests/`，每次改动后跑通。
- 零依赖：`import qcodemap` 全链路不出现第三方库。
- 零污染：`svn st` 在被分析项目中无任何新增/修改。

### 4.3 最终形态示例

```
$ python -m qcodemap callers gclient/gameplay/logic_base/entities/combatavatarmembers/cimp_combat_unit.py GetTeammateInfo
[VERIFIED] AvatarSceneNode.RefreshAiTakeoverToplogo
           gclient/gameplay/logic_base/comps/avatar_scene_node.py:366
[CANDIDATE] replay_util.GetTeammateInfo（同名不同函数，已排除语义验证）
           gclient/gameplay/logic_base/comps/comp_mark.py:306
...（17 处验证边 + 分级列表，总耗时 3.2s，缓存命中 0.2s）
```

---

## 附录 A：可行性验证数据（2026-08-17）

| 指标 | 数值 | 脚本 |
| --- | --- | --- |
| 语义链路命中 | 5/5 标准答案边，零假边 | test_feasibility.py（10 文件小样） |
| 全量建索引 | 6052 文件 / 558 万 names / 43s / 235MB | test_scale.py |
| 阶段1查询 | 1ms（普通名）~ 25ms（万级高频名） | test_scale.py |
| 对照：jedi+桩版 | 单函数 12~17s，安全阀丢边 | jedi_goto_edges.py（外部） |
| 对照：codemap --deps | import 级 only，全仓 30s 超时 | codemap v4.4.0（外部） |

## 附录 B：关键设计决策记录

- 桩数据化取代 .pyi：`.pyi` 必须与源码同目录（Python 模块解析规则），无法与源码
  分离；数据化后进 SQLite，同目录约束消失。（实验：镜像目录桩 0 生效）
- 纯 stdlib 取代 jedi：get_references 的 `_PARSED_FILE_LIMIT=30` 安全阀在大库上
  静默丢边（抬高至 2000 仍无效），且进程内冷扫描单次 198s。
- @Components 星调用解析法：`*pkg.importall()` → 读包 `__init__.py` 的
  `from . import X` 清单 → 在 X 模块找 `{Host}Member` 类。（实测打通
  CombatAvatar → CombatAvatarMember → GetTeammateInfo 链）
- 局部变量二级推导：`avatar = space.GetEntity(x)` 中 space 本身是
  `self.space` 属性时，先解属性类型再查返回事实。（实测打通 avatar 链）

## 附录 C：P0+P1 实测数据（2026-08-17）

| 指标 | 验收线 | 实测 | 结论 |
| --- | --- | --- | --- |
| 全量建库 | ≤90s | 6052 文件 78.1s（含完整 ast+facts，旧基线 43s 仅倒排） | 过 |
| 库体量 | <400MB | 333MB（含 facts + 索引） | 过（§3-4 关闭） |
| 单文件增量 | ≤5s | 0.5s（mtime 命中即跳过） | 过 |
| 语义查询（含验证） | ≤5s | 0.66s（41 候选全验证） | 过 |
| edges 缓存命中 | ≤0.5s | 0.001s | 过 |
| 语义链路回归 | 5/5 边 | 5/5 零假边（tests/test_feasibility.py） | 过 |
| 零依赖 | stdlib only | ast/sqlite3/re/importlib/json/argparse/fnmatch/warnings | 过 |
| 项目零写入 | svn st 无新增 | replay_util.py 仅 touch mtime，svn 无感知 | 过 |

落地中的关键增强（超出可行性版）：
- **同名类并集语义**：gclient/gserver 镜像类（CombatAvatar）与多文件同名组件
  （CombatAvatarMember ×2）在方法查找时按「同文件>同目录>同顶层 target」排序
  查全部定义（§3-2 的正式解法，消歧失败不再直接断链）；
- **import 解析重写**：`from M import N` 的 N 为子模块时落 `M/N.py`（可行性版
  拼成 `M.py`，组件边全丢）；模块映射基于全部文件而非仅有类的文件；
- **模块级调用解析**：`mod.Func()` 调用形态走 import 归一（GetPlayer 等）；
- **edges 缓存带 resolver_version**：解析器行为变更（RESOLVER_VERSION +1）即
  整体失效，避免旧结论污染；
- **Property 第二参数捕获**：`Property("ai_memory", CAIMemory)` 直接登记真实
  类型，减少对人工种子的依赖。

## 附录 D：第二轮实测数据（2026-08-17）

| 能力 | 实测 | 备注 |
| --- | --- | --- |
| deps/importers/hubs/tree | 四命令全库各 ≤0.15s | 纯 SQL 查表，无 ast-grep 30s 超时坑；hubs Top10 锚点与 codemap 实测方向一致（consts/events/cconst 领跑） |
| importers 枢纽判定 | P95 入度阈值 | avatar_scene_node.py 18 引用判枢纽 |
| MCP server | 9 工具，进程内+子进程冒烟通过 | stdio JSON-RPC，纯 stdlib；注册于项目 .codex/mcp.json（stdio 型，cwd=本目录） |
| blast-radius | 冷/热均 0.4s（121 函数×2 文件，闭包深度 1） | 调用链闭包（codemap 无此能力）+ import 级双维度 |

第二轮落地的关键修复：
- **查询热路径三轮优化**（叠加效果：单高频函数 214s → 亚秒）：
  1. 文件级预索引（行→调用节点/外层函数/赋值），替代逐候选全树 ast.walk；
  2. mro/类定义/方法定义查询的 Resolver 级 dict 缓存（283 万次 execute → 千次级；
     缓存键含 from_file 消歧维度）；
  3. blast 闭包对超高频名（>1500 候选，如 __init__/add_timer）只吃 edges 缓存，
     不做冷验证（此类名字的 VERIFIED 边极少且 CANDIDATE 洪流无信息量）。
- **局部变量二级推导递归防线**（depth>6 返回 None）：`x = x.f()` 自引用形态
  曾致无限递归；RESOLVER_VERSION 随行为变化升级（当前 v4），旧 edges 缓存整体失效；
- **diff hunk 解析**：new 侧行区间直接由 @@ 头解析，变更函数定位为启发式
  enclosing（def 到下一 def 前），输出带 heuristic 标注；
- **test_scale 基线容差**：文件数 ±1%（目标库自然演进），names ±10% 监控线。

## 附录 E：P4 第一轮实测数据（2026-08-18）

| 能力 | 实测 | 备注 |
| --- | --- | --- |
| 覆盖率契约 | 全库 ast 失败 1 文件被捕获并透出 | 此前静默残缺；callers/结构命令均带 partial+计数，deps 附 issues 清单 |
| find | 0.004s（avatar_scene_node 唯一命中） | LIKE 转义后真子串语义，短路径优先 |
| file-context | 0.155s（92 defs/19 imports/18 importers/枢纽=True） | 单文件消费面一次打包 |
| context | 0.344s（stats+top_dirs+hubs+external_top） | hubs Top3 consts/events/cconst 与附录 D 锚点一致；compact 截断生效 |
| 懒刷新 | touch 1 文件 → drift_check 命中 → 0.8s 增量重建 → refresh 摘要回传 | 二次调用无 refresh（索引已同步）；MCP 全链路验证 |
| MCP | 12 工具，进程内+子进程冒烟通过 | 新增 find_file / get_file_context / context |
| 语义回归 | 5/5 边零假边；GetTeammateInfo VERIFIED=21 与基线一致 | 六件回归全过（含新增 test_p4） |
| SCHEMA_VERSION 1→2 | rebuild 自动 DROP 旧表按新 schema 重建 | rebuild 前置 `_prepare_rebuild` 绕开版本校验死锁（首跑踩坑：只清 meta 时旧表结构残留导致 INSERT 失败） |

本轮全量重建 488s（基线 207s；names/组件边/文件数与基线一致，判断为机器
当日负载，非代码回归——增量路径 0.5s 不变）。

P4 第一轮落地中的关键实现点：
- **rebuild 语义修正**：`_prepare_rebuild` 在 Store 打开前独立连接 DROP
  全部表。原版 `--rebuild` 会先撞上 schema 版本校验（版本不符拒绝打开），
  形成死锁；且只清 meta 不 DROP 时 `CREATE TABLE IF NOT EXISTS` 跳过建表，
  新列缺失导致 INSERT 失败——schema 变更必须物理重建表结构。
- **find_file 的 LIKE 转义**：文件名常见 `_` 是 LIKE 通配符，不转义会
  伪命中（test_p4 用 demo_g00d 反例锚定）。
- **blast 的 svn st 模式与懒刷新合并**：MCP 入口先采集变更清单做 drift_check，
  再显式传入 blast，省一次 svn st 子进程调用；rev 模式对历史版本不刷新。
- **importers 的 coverage scope 语义**：scope=被引用目标集而非引用方全集
  （与 deps 的「查询目标集」口径一致），目标文件正常即 complete。

## 附录 F：P3-2 RPC 双端跳转实测（2026-08-18）

通道捕获量级（rpc 表，全量重建 268s）：

| 通道 | rpc 行数 | 对照探索预估 | 说明 |
| --- | --- | --- | --- |
| C2S（CallServer 族） | 2004 | ~1520+ | 探索只数 `.CallServer(`；同形态 Act/AnyTime/GameLogic 一并捕获，合理偏高 |
| S2C（CallClient） | 710 | ~757 | 排除 debug.py GM 通道与变量回调首参，合理偏低 |
| STUB（stub 族） | 287 | ~340 | 小写族无字面量方法名（变量）的自然跳过 |
| MAILBOX（call 族） | 195 | ~140+ | 常量回调名 |
| DES | 10 | 12 | 全部命中 |
| 合计 | 3206 | - | - |

四案例真库验证全部命中：
1. `rpc-refs SetPlayerAimState`：客户端 fps_state_fsm.py:336（StateAim.OnEnter）
   ↔ 服务端 combat_avatar.py:719（CombatAvatar.SetPlayerAimState），C2S；
2. `rpc-refs ObtainClan --stub ClanStub`：clan_caller.py:34 ↔ clan_stub.py:135，
   stub 精确配对（CallShardStubHostnum 双字面量提取）；
3. `rpc-refs ServerShowMessage`：服务端 3 处调用 ↔ 客户端 cimp_combat.py:238
   handler，S2C 反向；
4. `rpc-refs NotifyForceRecycle`：imp_clan_base/imp_race_base 两处 DES 调用 ↔
   clan_stub/race_stub 两 handler（同名双定义全部列出）。

blast 穿透实测：`blast-radius --files gserver/entities/clan_stub.py --depth 1`
的直接调用方含 3 条 via_rpc 入边，其中 CreateClan 的调用方在**客户端**
（gclient/gamesystem/entities/avatarmembers/cimp_clan.py:135）——影响面
首次穿越双端。

落地关键点与坑：
- **参数位以分发器定义为准，勿凭调用样例推断**：CallShardStubHostnum
  签名 (hostnum, shardkey, stubname, rpc_method) 的 stub=arg2/rpc=arg3，
  首版凭样例写成 2/1 位，test_rpc 当场抓出（'key' 被当成方法名）；
- **stub 分片后缀归一**：'ClanStub@2' 落库前 split('@')[0]，配对不受
  分片实例名影响；
- **RPC 入边与语义入边同去重键**：(file, line) 维度，blast 闭包里两类
  入边共用 seen_direct/seen_trans；
- **handler 不依赖 @rpc_method 装饰器**：ObtainClan 等 stub 方法无装饰器，
  配对一律走 defs 表按方法名（stub 已知时精确）；
- **ast 失败文件的 RPC 调用自动缺席**（如 cimp_replay.py 的 4 处
  CallServerNew），note 提示走 usages 补查。

## 附录 G：P3-1 pubsub 事件配对实测（2026-08-18）

机制摸底（两轮探索结论）：客户端与服务端是两套干净分端的事件机制——
客户端 `@events.ListenTo(events.X)`（约 4276 处）注册进 genv.messenger
（即 gshare/pubsub 单例），发布主入口 `genv.messenger.Broadcast(events.X)`
（约 3473 处，`self.Publish` 客户端仅 1 处且为注释死代码）；服务端
`@Subscribe(sconst.X)`（41 处）注册进 `_FUNCTION_DICT`，发布
`self.Publish(Cls.X)` 等（132+ 处）。**join 键 = 经 import 归一的
「模块路径.常量名」**：常量值是 range(1,1246) 解包整数（客户端 1245 个
常量全部 tuple-unpack 赋值）与服务端 enum 小整数，两端撞号且无数值语义，
裸属性名跨端也会撞（DIE 等两端语义无关）。

通道捕获量级（pubsub 表，全量重建 348s，SCHEMA 4）：

| 侧 | pubsub 行数 | 对照探索预估 | 说明 |
| --- | --- | --- | --- |
| listen（订阅） | 4769 | ~4276+41 | 客户端 4199（grep 4276 含注释行，ast 侧天然不计）+ 服务端 559 + 其余 11 |
| publish（发布） | 3628 | ~3473+132 | Broadcast（receiver 末段限 messenger）+ 服务端 Publish 族 |

探索预估的量级偏差已归因：服务端 @Subscribe 预估 41 只数了 sconst./consts.
前缀形态，漏掉 500 处 `@pubsub.Subscribe(裸类名)`；客户端差值 77 为注释掉
的装饰器（grep 计、ast 不计）。全表 8397 行，unresolved（`?` 前缀）仅 26 行，
distinct 事件键 1363 个，其中 1212 个双端配对成功（有发布有订阅）。

案例验证（主库实测全命中）：
1. `pubsub-refs ON_MONEY_DMZ_COIN_CHANGE`：发布点 1（cimp_dmz_mall.py:53
   PlayerAvatarMember._on_set_dmz_coin）↔ 订阅 2（dmz_shop_window.py:657
   DmzShopWindow.OnDmzCoinChange + dmz_warehouse_window.py:1472），客户端链；
2. `pubsub-refs TEAM_SETTLE`：单键 gserver.sconst.CombatAvatarEvent.TEAM_SETTLE
   订阅 43 + 发布 2——裸类名（from gshare.consts import）/sconst 前缀两种
   import 写法经别名归一 join 成同键；
3. blast 双向穿透：改 dmz_shop_window.py → 发布方 cimp_dmz_mall 入影响面
   （via_pubsub）；改 game_logic_sniper_mode.py → CHANGE_HERO 事件一次带出
   43 个订阅 handler（换英雄影响面全貌）。RPC via_rpc 穿透回归无损。

落地关键点与坑：
- **装饰器 Call 的 func 是 Name 不是 Attribute**：`@ListenTo(events.X)`
  的 receiver 形态与 `self.Publish(X)` 不同，订阅侧须同时认裸名与模块
  前缀两种 receiver；发布侧反之必须带 receiver（裸名 `Publish(x.Y)`
  无法与本地同名函数区分，跳过防误报）；
- **事件归一只要求自洽不要求绝对路径**：同端两侧 import 风格一致即
  推出相同键；根名解析失败（局部变量转发等）落 `?`+原文，两侧同用
  同文仍可 join，pubsub-refs 标注 unresolved 降级；
- **嵌套 def 双访问去重**：装饰器/调用会被外层函数 walk 与内层自身
  扫描各产一行，按 (file,line,side,event) 去重保留内层归属（后到覆盖）；
- **排除项**：`.FireEvent(` 518 处是动画骨架事件（字符串 cue 名，无
  Python 监听方）；gserver/entities/pubsub_stub.py 的
  Publish(proxy, topic, data) 是跨进程网络 topic pubsub；@AIListenTo
  是 AI 传感器机制——三者与本机制无关，均在分发表/排除文件层拦下；
- **转发别名归一**：gshare/consts.py:12 在服务端语境 `from gserver.sconst
  import AvatarEvent, CombatAvatarEvent` re-export，1686 个文件两种 import
  写法并存——不归一则同事件裂成两个键、配对率骤降（TEAM_SETTLE 曾裂成
  25+18 两键）。custom 层 PUBSUB_ALIAS_PREFIX 前缀改写解决。

已知边界：
- 变量首参（全库约 3 处 `Broadcast(self.cur_event)`）不做属性回溯，
  静默跳过；
- 模块级语句里的 pubsub 调用不采集（与 rpc 口径一致）；
- 客户端 `self.Publish`/`self.Broadcast` 之外的理论形态（模块级
  pubsub.Publish 3 处转发）不单列，UnBoundPublishFunc 已入分发表。

## 附录 H：作用域 import、约定回调与上下文控制（2026-08-18）

本轮保持“通用能力在 qcodemap/、框架语法只在 custom/”边界：

- imports schema 增加 line/scope。结构图消费所有作用域；组件解析只消费
  模块级 import；调用与事件归一按调用点词法域解析，跨函数同名别名不串线。
- core 新增 callback_facts 通用协议和严格运行时宿主交集；Messiah custom
  才识别 `Property("x") -> _on_set_x`，输出 `PROPERTY-INFERRED`。
- `CallServerPacked(method, *args)` 在 Messiah RPC_DISPATCHERS 登记为 C2S；
  无方法名参数的 CallServerDrive 仍排除。
- blast 输出升级为 qcodemap.blast/v2。MCP 默认 summary、CLI 默认 full；
  page 对 callers 按 layer 分页、对 importers 单独分页，limit 只裁输出。
- MCP server 启动时自举 stdin/stdout/stderr UTF-8，不再要求调用方设置
  PYTHONUTF8；stdout 仍只承载 JSON-RPC。

主库重建：8927 文件、7651033 names、447MB、469.4s、ast 失败 1。
真实锚点：avatar_scene_node importers 18→19（补到 comp_toplogo 局部 import）；
OnSpeedLevelChange 捕获 3 个 CallServerPacked C2S 调用；闪电 capybara_state
连到客户端 `_on_set_capybara_state`，共同宿主 3 个且无同名兜底误连。
