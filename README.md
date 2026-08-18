# 🗺️ QCodeMap

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE) [![Python](https://img.shields.io/badge/python-3.x-blue.svg)](https://www.python.org/downloads/) [![Dependencies](https://img.shields.io/badge/dependencies-0-pure%20stdlib-success.svg)]() [![MCP](https://img.shields.io/badge/MCP-14_tools-blueviolet.svg)]() [![Version](https://img.shields.io/badge/version-v0.9-orange.svg)]()

面向任意 Python 代码库的**语义级代码导航索引**：把框架动态语义（组件注入、
声明式属性、运行时全局注入等习语）数据化为 SQLite 事实表，配倒排索引与
两阶段查询，提供秒级、可信、与源码物理分离的调用链查询。

QCodeMap 不试图理解你的代码、推断架构，也不替你决定什么相关——那是
LLM 和工程师的工作。

QCodeMap 只为一件事而存在：

> 让每一次「谁调用它 / 它调了谁 / 改它影响谁」，快到可以随手做，准到可以直接信。

[快速上手](#-快速上手) • [工作原理](#工作原理) • [命令](#命令) • [MCP Server](#-mcp-server) • [实测数据](#-实测数据) • [对比](#与替代方案对比)

![QCodeMap 源码索引与 AI Agent 工作流](docs/qcodemap-framework.svg)

## 问题

大型 Python 代码库里的代码调查是迭代式的：想一个问题 → 检索一下 → 读代码 →
再想 → 再检索。贵的不是思考，是每一次检索。

没有趁手工具时，你只有这些选择：

- **grep**：只认字符串不认语义；同名函数、镜像目录带来大量噪音；
  答不了「改这个函数影响谁」
- **codemap CLI**：依赖分析只有 import 级、无调用链；万文件库上全仓
  `--deps` 触发 ast-grep 30 秒超时
- **jedi + 类型桩**：大库上跨文件边被 30 文件解析安全阀静默丢弃，
  单查询 12~17 秒；且 `.pyi` 桩必须与源码同目录，无法物理分离

更深的根因：重框架的 Python 项目存在大量**动态语义**——组件注入是
setattr 拷贝（不进 MRO）、全局对象是运行时注入、属性是声明式注册。
通用工具天生看不懂这些习语；jedi 配桩能部分解决，但桩被「与源码同目录」
的规则锁死。

## 洞察

代码调查不需要更聪明的工具，需要**更便宜、更可信的查表**。

把「找引用」从每次现扫全仓，变成查询一张预先建好的事实表；把框架习语
从与源码绑定的类型桩，降格为索引库里可再生的数据行——同样的调查流程，
每一步都快一个数量级。

瓶颈不是智能，是检索成本与结果可信度。

## QCodeMap 是什么（与不是）

### ✅ 是

- 一个把代码库（含框架动态语义）索引成 SQLite 事实表的静态索引
- 秒级调用链 / RPC / 事件配对 / 变更影响面查询
- 让 LLM 与工程师直接跳到「精确文件 + 行号区间」的导航层
- 输出带 `VERIFIED` / `CANDIDATE` 分级，零假边优先

### ❌ 不是

- 不是语义分析器或架构推断引擎
- 不是 IDE（没有补全、重构执行）
- 不是运行时探针（纯静态，不跑你的代码）
- 不替你决定什么相关——那是使用者的事

## 这如何改变代码调查

### 没有 QCodeMap

```
grep "GetTeammateInfo"
→ 大量命中：同名函数、镜像目录、注释全在列
→ 人工逐个开文件确认
→ 组件注入边、运行时注入对象看不出来，漏判风险自负
```

### 有 QCodeMap

```
qcodemap callers src/logic/avatar.py GetTeammateInfo
→ 亚秒返回：每条边带 VERIFIED/CANDIDATE 分级与目标定义行号
→ 框架习语（组件 / RPC / 事件）由 custom/ 钩子解释
→ 拿不准可以逐级放大：边 → 片段 → 整文件
```

同样的推理，同样的结论，检索从分钟级降到亚秒级，且每条边可溯源。

## 📊 实测数据

孵化案例：约 9000 文件的游戏客户端/服务端代码库（2026-08-18，v0.9）。

| 指标 | 数值 |
| --- | --- |
| 全量建库 | 8927 文件 / 469s / 447MB（对照 PyCharm 同库索引 3.8GB） |
| 语义查询（含验证） | 亚秒级；高频名首查 3s 量级 |
| edges 缓存命中 | 0.001s |
| 单文件增量 | 0.5s |
| 语义回归 | 5/5 标准答案边，零假边 |
| 结构四命令 | 全库各 0.22~0.24s |
| blast-radius | 2 文件 121 函数冷 43.2s / 热 0.7s（解析器升级后首次冷缓存） |
| agent 消费面 | find 0.004s / file-context 0.16s / context 0.34s |
| 懒刷新 | MCP 查询发现 mtime 漂移自动增量 build，单文件级秒内 |

## ⚡ 快速上手

纯标准库（ast / sqlite3 / re），clone 即用，零第三方依赖。索引产物在
`cache/`，可再生，被分析项目零写入（svn/git 无感知）。

```bash
cd QCodeMap

# 建索引：首次在 custom/ 写项目档案（见 custom/README.md），
# 或直接用参数对任意 Python 目录开扫
# --targets：只索引项目根下这些顶层目录（逗号分隔，如 src,lib = src/ + lib/），
# 用于跳过 tests/docs/产物等无关目录；不传则 ROOT 全量扫描
python -m qcodemap build --root /path/to/your/project --targets src,lib

# 谁调用这个函数（VERIFIED=语义验证边 / CANDIDATE=同名候选）
python -m qcodemap callers src/logic/avatar.py GetTeammateInfo
```

## 命令

### 调用链（语义验证）

```bash
python -m qcodemap callers src/logic/avatar.py GetTeammateInfo  # 谁调用它
python -m qcodemap callees src/logic/avatar.py RefreshToplogo   # 它调了谁
python -m qcodemap usages HasSkywing                            # 标识符全仓出现点
```

### 框架习语配对

```bash
# RPC 双端跳转（字符串分发形态，通道与 stub 一并列出）
python -m qcodemap rpc-refs SetPlayerAimState
python -m qcodemap rpc-refs ObtainClan --stub ClanStub

# 事件双端配对（订阅 handler ↔ 发布点，事件键 import 归一）
python -m qcodemap pubsub-refs ON_MONEY_DMZ_COIN_CHANGE
```

### 结构四件套（codemap 平价，无超时）

```bash
python -m qcodemap deps <文件或目录>
python -m qcodemap importers <文件>
python -m qcodemap hubs --top 25
python -m qcodemap tree --depth 2
```

### 变更影响面（调用链闭包 + import 级双维度）

```bash
python -m qcodemap blast-radius                        # 默认 svn st 采集变更
python -m qcodemap blast-radius --rev 100:200          # 版本区间
python -m qcodemap blast-radius --files a.py,b.py      # 显式清单
python -m qcodemap blast-radius --mode summary         # 仅计数与层摘要
python -m qcodemap blast-radius --mode page --section callers --layer 2 --offset 0 --limit 50
```

### agent 消费面

```bash
python -m qcodemap find avatar_scene                 # 模糊路径搜索
python -m qcodemap file-context src/logic/avatar.py  # 单文件完整消费面打包
python -m qcodemap context --compact                 # AI 会话冷启动注入
```

所有命令支持 `--json` 机器可读输出（带 `schema_version` 与 `coverage`；
ast 解析失败文件会在 coverage 里以 partial 状态透出，不静默残缺）。
`blast-radius` 的 CLI 默认 `full` 保持兼容；MCP 默认 `summary` 防止大影响面
撑满上下文，可用 `mode=page, section, layer, offset, limit` 按需取明细。

## 工作原理

- 扫描仓库，把每个符号的事实（定义、调用、组件边、RPC / 事件注册）写入
  SQLite 事实表，配倒排索引
- 框架习语（setattr 组件注入、运行时全局注入、声明式属性等）经
  `custom/facts.py` 钩子解释为核心可消费的数据行——**桩知识降格为数据**
- 查询两阶段：先候选（名字级倒排召回），再语义验证（MRO / 组件边 /
  数据流 / 返回事实）
- 索引产物在 `cache/`，与源码物理分离、可再生；MCP 查询发现 mtime
  漂移自动增量刷新
- 核心引擎项目无关，项目习语与索引范围全部放在 `custom/` 定制层
  （仓库仅含模板说明，见 custom/README.md），换项目零改核心

无运行时、无注入、不跑你的代码。查询结果是可溯源的事实行，不是猜测。

## 输出分级（零误报优先）

- `VERIFIED`：语义验证链路（MRO/组件边/数据流/返回事实）落到目标定义
- `CANDIDATE`：调用形态成立但解析不可达或同名歧义，注明解析到了哪个同名定义
- `RPC-INFERRED` / `EVENT-INFERRED` / `PROPERTY-INFERRED`：由 custom
  声明的框架约定推断，保留通道或共同运行时宿主证据，不冒充语义验证
- 解析不了宁可降级，不给假边

## 🔌 MCP Server

```bash
python -m qcodemap mcp   # stdio，14 个工具，可注册进任意 MCP 客户端
```

注册后，AI agent 的代码调查直接走查表：callers / callees 定位调用链、
blast-radius 评估影响面、file-context 单文件打包、context 冷启动注入，
避免整文件读取与反复 grep。

## 什么时候 QCodeMap 合适

- 万文件级 Python 库，context 与耐心都是稀缺资源
- 重框架习语项目：组件注入、服务定位器、注册表式声明属性
- AI agent（MCP）接入，需要可信、可分页的代码事实
- 重构 / 合码前评估变更影响面

## 什么时候不是它

- 小项目：直接读完就行
- 非 Python 代码库
- 需要运行时真相（这是静态索引）
- 需要补全、重构执行等完整 IDE 能力

## 与替代方案对比

| 能力 | QCodeMap | codemap CLI | jedi + .pyi 桩 | grep |
| --- | --- | --- | --- | --- |
| 调用链（callers/callees） | ✅ 语义验证 | ❌ 仅 import 级 | ⚠️ 大库跨文件边静默丢失 | ❌ 字符串匹配 |
| 框架动态语义 | ✅ custom 钩子插拔 | ❌ | ⚠️ 桩须与源码同目录 | ❌ |
| 与源码物理分离 | ✅ | ✅ | ❌ | — |
| 万文件全仓结构查询 | ✅ 0.22s | ⚠️ 30s 超时 | — | ✅ |
| 输出可信分级 | ✅ VERIFIED/CANDIDATE | — | ❌ 静默残缺 | ❌ 噪音 |
| 依赖 | 纯 stdlib | ast-grep 二进制 | jedi | 无 |

## 设计哲学

> 只陈述事实，不冒充理解。解析不了宁可降级，不给假边。

确定性、可溯源、可再生。它是查询原语，不是框架。

## 目录结构

```
qcodemap/     核心引擎（项目无关）：cli / build / scanner / store / resolve /
              structure / blast / context / rpc_refs / pubsub_refs /
              mcp_server / hooks / config / defaults
custom/       项目定制层：config.py（范围）/ facts.py（框架习语钩子）/
              seeds.py（人工种子）。仓库仅随附 README.md 模板
tests/        回归套件；部分用例锚定孵化案例项目（需该代码库在场才能复跑）
cache/        索引产物（447MB，可再生，不入库）
docs/         深入文档（见下）
```

## 文档索引

| 文档 | 内容 |
| --- | --- |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 模块职责、数据流、表结构、关键设计决策与原因 |
| [docs/CUSTOM_GUIDE.md](docs/CUSTOM_GUIDE.md) | 二次开发指南：换项目/扩语义/加命令的手把手路径 |

## 维护约定

- 改动后必跑回归：`python tests/test_feasibility.py`（5/5）与
  `python tests/test_scale.py`，动解析器则六个全跑（含 test_p4.py）
- 解析器行为变更必须 `RESOLVER_VERSION + 1`（resolve.py 顶部），旧缓存自动失效；
  存储结构变更必须 `SCHEMA_VERSION + 1`（store.py 顶部）并 `--rebuild`
- 新框架语义一律写进 `custom/facts.py` 钩子，核心包不认识任何框架名

## License

MIT License — 见 [LICENSE](LICENSE)。

## 致谢

- 结构能力对齐 codemap CLI（v4.4.0，并以其超时为反面基准）
- jedi 桩版 PoC 提供了前期基准（已被本方案取代）

> QCodeMap：瓶颈不是智能，是可信又便宜的事实查询。
