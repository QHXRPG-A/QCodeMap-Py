# -*- coding: utf-8 -*-
"""内建默认配置：custom/ 缺失或未覆盖时兜底，保证裸核心对任意 Python 项目可用。"""

import os

# 被分析项目根（默认当前目录；正式使用由 custom/config.py 或 --root 指定）
ROOT = os.getcwd()

# 纳入索引的目标（相对 ROOT 的目录名/文件名，空 = ROOT 全量）
TARGETS = []

# 目录名排除（任意层级命中即跳过整棵子树）
EXCLUDE_DIRS = {'__pycache__', '.git', '.svn', '.idea', '.vscode',
                'venv', 'node_modules', '__MACOS'}

# 文件名排除（fnmatch 语法，如 '*_origin.py'）
EXCLUDE_FILES = []

# 路径级放行（相对 ROOT，posix 目录或文件）：优先于 EXCLUDE_DIRS 目录名排除，
# 用于精确捞回被目录名规则误杀的路径（如某个 data 子目录）
INCLUDE_PATHS = []

# names 倒排降采样前缀：命中路径的文件只索引 def/class/import/赋值/装饰器行
# 的 token（表格产物目录的纯数据行没有调用语义，全量索引只有高频键名噪音）
NAMES_DOWNSAMPLE_PREFIXES = []

# SQLite 产物路径；None -> <项目>/cache/qcodemap.db
DB_PATH = None
