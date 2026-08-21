<p align="right">
  <a href="README.md">English</a> | <strong>简体中文</strong>
</p>

# 🗺️ QCodeMap

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE) [![Python](https://img.shields.io/badge/python-3.x-blue.svg)](https://www.python.org/downloads/) [![Dependencies](https://img.shields.io/badge/dependencies-0-pure%20stdlib-success.svg)]() [![MCP](https://img.shields.io/badge/MCP-17_tools-blueviolet.svg)]() [![Version](https://img.shields.io/badge/version-v0.9-orange.svg)]()

面向 Python 代码库（尤其是大型、重框架的项目）的**语义级导航索引**：把代码里的
定义、调用，连同框架特有的动态写法（组件注入、声明式属性、运行时注入的全局
对象），统统扫进一个 SQLite 索引库，配上倒排索引和两阶段查询——「谁调用它 /
它调了谁 / 改它影响谁」这类问题秒级出答案，结果可信，而且完全不碰源码。

QCodeMap 不试图理解你的代码、推断架构，也不替你判断哪些相关——那是
LLM 和工程师的事。它只做一件事：

> 让每一次「谁调用它 / 它调了谁 / 改它影响谁」，快到可以随手查，准到可以直接信。

[快速上手](#-快速上手) • [工作原理](#工作原理) • [命令](#命令) • [MCP Server](#-mcp-server) • [实测数据](#-实测数据) • [对比](#与替代方案对比)

![QCodeMap 源码索引与 AI Agent 工作流](docs/qcodemap-framework.zh-CN.svg)

## 解决什么问题

大代码库里查代码是绕圈子的：提出一个问题 → 检索 → 读代码 → 冒出新的问题 →
再检索。费时间的不是思考，是每一步检索。

手头没有趁手的工具时，选择只有这么几个：

- **grep**：只认字符串不认语义，同名函数、镜像目录带来大量噪音，答不了
  「改这个函数影响谁」
- **codemap CLI**：依赖分析只到 import 级，没有调用链；万文件库上全仓
  `--deps` 会触发 ast-grep 30 秒超时
- **jedi + 类型桩**：大库上跨文件的调用边会被 30 文件解析上限悄悄丢弃，
  单次查询 12~17 秒；`.pyi` 桩还必须和源码放在同一目录，没法分开管理

更深一层的原因：重度使用框架的 Python 项目里有大量**动态写法**——组件注入
靠 setattr 拷贝（不进 MRO）、全局对象在运行时注入、属性靠声明式注册。
通用工具天生看不懂这些；jedi 配桩能解决一部分，但桩被「与源码同目录」
这条规则锁死。

## 核心思路

查代码这件事，缺的不是更聪明的工具，而是**开销更低、结果更可靠的查询**。

把「找引用」从每次都现扫全仓，变成查一张事先建好的索引库；把原本绑在源码
上的类型桩知识，变成索引库里随时可以重建的数据行。调查流程不变，
每一步都快一个数量级。

瓶颈不在分析能力，在检索开销和结果的可信度。

## 定位：是什么，不是什么

### ✅ 是

- 一个静态索引工具：把代码库（连同框架的动态写法）扫进 SQLite
- 秒级给出调用链、RPC / 事件双端配对、变更影响面
- 给 LLM 和工程师用的导航层：直接定位到文件和行号区间
- 结果分级标注（`VERIFIED` / `CANDIDATE`），宁可漏报不误报

### ❌ 不是

- 不是语义分析器，也不是架构推断引擎
- 不是 IDE（没有补全、重构）
- 不在运行时插桩（纯静态，不运行你的代码）
- 不替你判断哪些代码相关——这是使用者自己的事

## 效果对比

### 用 grep

```
grep "GetTeammateInfo"
→ 大量命中：同名函数、镜像目录、注释全在列
→ 只能人工逐个开文件确认
→ 组件注入、运行时注入的对象看不出来，漏判风险自负
```

### 用 QCodeMap

```
qcodemap callers src/logic/avatar.py GetTeammateInfo
→ 亚秒返回：每条边标注 VERIFIED/CANDIDATE，带目标定义行号
→ 组件 / RPC / 事件这类框架写法由 custom/ 钩子解释
→ 拿不准可以逐级放大：边 → 片段 → 整个文件
```

同样的推理过程，同样的结论，检索从分钟级降到亚秒级，而且每条结果都能
回溯到源码。

## 📊 实测数据

试点项目：约 9000 文件的游戏客户端 / 服务端代码库（2026-08-20，v0.9）。

| 指标 | 数值 |
| --- | --- |
| 全量建库 | 8950 文件 / 97.5s / 220MB（对照优化前 449MB） |
| 语义查询（含验证） | 亚秒级；高频名首查 3s 量级 |
| edges 缓存命中 | 0.001s |
| 5 文件增量 | 1s 内 |
| 语义回归 | 5/5 标准答案边，零假边 |
| 结构四命令 | 全库各 0.22~0.24s |
| blast-radius | 2 文件 121 函数冷 43.2s / 热 0.7s（解析器升级后首次冷缓存） |
| agent 查询 | find 0.004s / file-context 0.16s / context 0.34s |
| 全仓新鲜度检查 | 0.61s；CLI/MCP 自动增量刷新新增、修改、删除文件 |

## ⚡ 快速上手

纯标准库（ast / sqlite3 / re），clone 下来就能用，零第三方依赖。索引产物
放在 `cache/`，随时可以重建，被分析的项目零写入（svn/git 完全无感知）。

AI Agent 可先加载内置的
[QCodeMap Agent skill](skill/qcodemap-agent/SKILL.md)，快速理解仓库、选择查询，
并在不把项目规则混入核心层的前提下适配 `custom/`。

```bash
cd QCodeMap

# 建索引：首次在 custom/ 写项目档案（见 custom/README.md），
# 或直接用参数对任意 Python 目录开扫
# --targets：只索引项目根下这些顶层目录（逗号分隔，如 src,lib = src/ + lib/），
# 用于跳过 tests/docs/产物等无关目录；不传则 ROOT 全量扫描
python -m qcodemap build --root /path/to/your/project --targets src,lib
# --targets 只刷新和清理选中范围；主动建立子集库使用 --rebuild --targets

# 谁调用这个函数（VERIFIED=语义验证边 / CANDIDATE=同名候选）
python -m qcodemap callers src/logic/avatar.py GetTeammateInfo
```

## 命令

### 调用链（语义验证）

```bash
python -m qcodemap callers src/logic/avatar.py GetTeammateInfo  # 谁调用它
python -m qcodemap callers src/logic/avatar.py Activate --receiver-class FestivalTargetEntity
python -m qcodemap callees src/logic/avatar.py RefreshToplogo   # 它调了谁
python -m qcodemap usages HasSkywing                            # 标识符全仓出现点
python -m qcodemap defs HasSkywing                              # 定义点
python -m qcodemap diagnose                                     # custom 项目诊断
```

### RPC 与事件双端配对

```bash
# RPC：按字符串分发的方法名，把调用点和 handler 配对（通道与 stub 一并列出）
python -m qcodemap rpc-refs SetPlayerAimState
python -m qcodemap rpc-refs ObtainClan --stub ClanStub

# 事件：订阅 handler ↔ 发布点配对（事件常量按 import 归一）
python -m qcodemap pubsub-refs ON_MONEY_DMZ_COIN_CHANGE
```

### 结构四件套（对标 codemap，无超时）

```bash
python -m qcodemap deps <文件或目录>
python -m qcodemap importers <文件>
python -m qcodemap hubs --top 25
python -m qcodemap tree --depth 2
```

### 变更影响面（调用链闭包 + import 级两个维度）

```bash
python -m qcodemap blast-radius                        # 默认 svn st 采集变更
python -m qcodemap blast-radius --rev 100:200          # 版本区间
python -m qcodemap blast-radius --files a.py,b.py      # 显式清单
python -m qcodemap blast-radius --mode summary         # 仅计数与层摘要
python -m qcodemap blast-radius --mode page --section callers --layer 2 --offset 0 --limit 50
```

### 面向 AI agent

```bash
python -m qcodemap find avatar_scene                 # 模糊搜路径
python -m qcodemap file-context src/logic/avatar.py  # 单文件信息一次打包
python -m qcodemap context --compact                 # 新会话开场的项目摘要
```

所有查询命令统一支持 `--root`、`--db`、`--json` 和
`--refresh auto|check|off`。JSON 输出带 `schema_version`、`coverage` 和
`index` 元数据（构建时间、刷新情况、漂移数量、配置指纹与覆盖状态）；
ast 解析失败的文件会在 coverage 里标成 partial，不会悄悄缺数据。
`blast-radius` 的 CLI 默认 `full` 保持兼容；MCP 默认 `summary`，
避免一次把海量结果塞满上下文，需要明细时用
`mode=page, section, layer, offset, limit` 分页取。

## 工作原理

- 扫描整个仓库，把每个符号的信息（定义、调用、组件边、RPC / 事件注册）
  写入 SQLite，配上倒排索引
- 框架特有的写法（setattr 组件注入、运行时全局注入、声明式属性）通过
  `custom/facts.py` 钩子解释成核心引擎直接能用的数据行——
  **桩里的知识变成库里的数据**
- 查询分两步：先按名字倒排召回候选，再做语义验证（MRO / 组件边 /
  数据流 / 返回值来源）
- 索引产物在 `cache/`，和源码完全分离、随时可重建；CLI/MCP 查询默认扫描
  全仓文件集和 mtime，发现新增、修改或删除后自动增量刷新
- 核心引擎不认识任何具体项目；项目相关的写法和索引范围都放在 `custom/`
  定制层（仓库里只带模板说明，见 custom/README.md），换项目不用动核心

没有运行时、没有注入、不运行你的代码。查询结果条条可回溯，不是猜的。

## 输出分级（宁可漏报，不误报）

- `VERIFIED`：语义验证链路（MRO / 组件边 / 数据流 / 返回值）落到了目标定义
- `FRAMEWORK-INFERRED`：custom 提供了可靠 receiver 类型证据，已收敛同名候选
- `CANDIDATE`：调用形态成立但解析不可达，或同名有歧义，会注明解析到了哪个同名定义
- `RPC-INFERRED` / `EVENT-INFERRED` / `PROPERTY-INFERRED`：按 custom 声明的
  框架约定推断，保留通道或共同运行时宿主证据，不冒充语义验证
- 解析不了就降级，不给假边

## 🔌 MCP Server

```bash
python -m qcodemap mcp   # stdio，17 个工具，可注册进任意 MCP 客户端
```

注册后，AI agent 查代码直接查库：callers / callees 定位调用链、
blast-radius 评估影响面、file-context 打包单文件、context 生成开场摘要，
省得整文件整文件地读、反反复复地 grep。

## 适用场景

- 万文件级的 Python 库，上下文和耐心都不够用
- 项目重度依赖框架：组件注入、服务定位器、注册式属性满天飞
- AI agent（MCP）需要可信、可分页的代码事实
- 重构 / 合码前评估改动的影响范围

## 不适用的场景

- 小项目：直接读完就行
- 非 Python 代码库
- 需要运行时的真实行为（这是纯静态索引）
- 需要补全、重构这类完整 IDE 能力

## 与替代方案对比

| 能力 | QCodeMap | codemap CLI | jedi + .pyi 桩 | grep |
| --- | --- | --- | --- | --- |
| 调用链（callers/callees） | ✅ 语义验证 | ❌ 仅 import 级 | ⚠️ 大库跨文件边悄悄丢失 | ❌ 字符串匹配 |
| 框架动态写法 | ✅ custom 钩子插拔 | ❌ | ⚠️ 桩须与源码同目录 | ❌ |
| 与源码物理分离 | ✅ | ✅ | ❌ | — |
| 万文件全仓结构查询 | ✅ 0.22s | ⚠️ 30s 超时 | — | ✅ |
| 结果分级标注 | ✅ VERIFIED/CANDIDATE | — | ❌ 悄悄缺数据 | ❌ 噪音 |
| 依赖 | 纯 stdlib | ast-grep 二进制 | jedi | 无 |

## 设计原则

> 只陈述事实，不冒充理解。解析不了就降级，不给假边。

确定性、可回溯、可重建。它是查询工具，不是框架。

## 目录结构

```
qcodemap/     核心引擎（与项目无关）：cli / build / scanner / store / resolve /
              structure / blast / context / rpc_refs / pubsub_refs /
              mcp_server / hooks / config / defaults
custom/       项目定制层：config.py（范围）/ facts.py（框架写法钩子）/
              seeds.py（人工种子）。仓库只随附 README.md 模板
skill/        内置 qcodemap-agent 上手 skill：仓库导航与脱敏 custom 适配指南
tests/        自包含回归套件（test_p4/test_p5 自建临时库）；锚定试点代码库的回归与
              custom/ 项目档案留在本地工作区，不入公开仓库
cache/        索引产物（约 220MB，可重建，不入库）
docs/         深入文档（见下）
```

## 文档索引

| 文档 | 内容 |
| --- | --- |
| [skill/qcodemap-agent/SKILL.md](skill/qcodemap-agent/SKILL.md) | Agent 快速上手、查询路由和隐私安全的 custom 适配 |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | 模块职责、数据流、表结构、关键设计决策与原因 |
| [docs/CUSTOM_GUIDE.md](docs/CUSTOM_GUIDE.md) | 二次开发指南：换项目/扩语义/加命令的手把手路径 |

## 维护约定

- 改动后必跑公开自包含回归：`python tests/test_p4.py` 与
  `python tests/test_p5.py`；本地工作区有试点代码库与项目档案时，再跑完整项目回归
- 解析器行为变更必须 `RESOLVER_VERSION + 1`（resolve.py 顶部），旧缓存自动失效；
  存储结构变更必须 `SCHEMA_VERSION + 1`（store.py 顶部）并 `--rebuild`
- 新框架语义一律写进 `custom/facts.py` 钩子，核心包不认识任何框架名

## License

MIT License — 见 [LICENSE](LICENSE)。

## 致谢

- 结构四件套对标 codemap CLI v4.4.0，并解决了它在万文件库上超时的问题
- jedi 桩版 PoC 提供了前期基准（已被本方案取代）

> QCodeMap：把查代码这件事，变得又快又可靠。
