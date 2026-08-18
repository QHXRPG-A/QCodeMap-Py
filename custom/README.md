# custom/ —— 项目定制层

QCodeMap 核心引擎（`qcodemap/`）项目无关；本目录是它与具体代码库之间的
唯一接缝。仓库不随附任何项目档案——三个文件全部可选，缺失即用默认值，
按下表自建即可对任意 Python 项目开扫。

| 文件 | 作用 | 何时需要 |
| --- | --- | --- |
| `config.py` | 项目档案：索引范围（ROOT / TARGETS / EXCLUDE_* / INCLUDE_PATHS 等） | 首次使用建议建，也可用 CLI `--root/--targets` 代替 |
| `facts.py` | 框架习语钩子：声明式属性、组件注册、运行时全局注入等语义事实的提取规则 | 项目有动态语义习语才需要（纯 plain Python 可省） |
| `seeds.py` | 人工类型种子：ast 静态扫不出的返回类型/属性类型 | 按需补充 |

最小 `config.py` 模板：

```python
ROOT = r'D:\your\project'        # 被分析项目根
TARGETS = ['src', 'lib']         # 纳入索引的顶层目录（空 = ROOT 全量）
EXCLUDE_DIRS = {'__pycache__'}   # 目录名排除（任意层级命中即剪枝）
EXCLUDE_FILES = []               # 文件名排除（fnmatch）
```

- 装载优先级：defaults 兜底 ← 本目录覆盖 ← CLI 参数最高
- 多项目共存：`--custom <目录>` 或环境变量 `QCODEMAP_CUSTOM` 指向不同档案，`--db` 分库
- 框架习语钩子的写法与四类习语参考表：见 [docs/CUSTOM_GUIDE.md](../docs/CUSTOM_GUIDE.md)
- 事实如何被两阶段查询消费：见 [docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md)
