# -*- coding: utf-8 -*-
"""配置加载：defaults 兜底 <- custom/ 覆盖 <- CLI 再覆盖。

custom 目录默认取包平级的 custom/，可用环境变量 QCODEMAP_CUSTOM 或
参数 custom_dir 改指他处。core 不 import custom 的任何具体名字，
一律按文件路径动态加载（importlib），缺失哪个文件就用哪个默认。
"""

import importlib.util
import os
import sys
from pathlib import Path

from qcodemap import defaults
from qcodemap.hooks import FactsHooks

PKG_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PKG_DIR.parent

# custom/config.py 里可出现的配置键（大小写敏感）
KNOWN_KEYS = ('ROOT', 'TARGETS', 'EXCLUDE_DIRS', 'EXCLUDE_FILES',
              'INCLUDE_PATHS', 'NAMES_DOWNSAMPLE_PREFIXES', 'DB_PATH',
              'RPC_CHANNELS', 'INDEX_PROFILE_RULES',
              # 资源绑定查询（ui-refs）：外部资源根与自管索引库，值对 core
              # 不透明，由 custom 的 build_done / 查询模块消费；缺省不启用
              'UI_TBUI_ROOT', 'UI_CSB_ROOT', 'UI_INDEX_DB')


class Config(object):
    """运行期配置快照；CLI 覆盖具有最高优先级。"""

    def __init__(self):
        self.root = defaults.ROOT
        self.targets = list(defaults.TARGETS)
        self.exclude_dirs = set(defaults.EXCLUDE_DIRS)
        self.exclude_files = list(defaults.EXCLUDE_FILES)
        self.include_paths = list(defaults.INCLUDE_PATHS)
        self.names_downsample = list(defaults.NAMES_DOWNSAMPLE_PREFIXES)
        self.db_path = defaults.DB_PATH or str(PROJECT_DIR / 'cache' / 'qcodemap.db')
        self.ret_seeds = {}
        self.attr_seeds = {}
        self.rpc_channels = {}  # 通道代码 -> 显示名（rpc-refs 输出用）
        self.index_profile_rules = []  # [(glob, full|semantic-only), ...]
        self.ui_tbui_root = ''   # 外部 tbui 资源根（ui-refs；空 = 不启用）
        self.ui_csb_root = ''    # 外部 csb 包内产物根（漂移标注用）
        self.ui_index_db = ''    # 资源自管索引库路径（空 = 用主库同目录 qcodemap_ui.db）
        self.ui_profile = None   # custom/ui_profile.py 的 Profile 实例；None=降级
        self.targets_overridden = False
        self.hooks = None  # FactsHooks 实例；None = 无框架钩子，仅通用事实
        self.custom_dir = None


def _load_py_file(path, modname):
    """按路径加载一个 python 文件为模块；core 与 custom 解耦的关键。"""
    spec = importlib.util.spec_from_file_location(modname, str(path))
    mod = importlib.util.module_from_spec(spec)
    # 注册进 sys.modules：multiprocessing 子进程 pickle 钩子实例时按名可寻
    sys.modules[modname] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(modname, None)
        raise
    return mod


def _apply_custom_config(cfg, mod):
    for key in KNOWN_KEYS:
        if not hasattr(mod, key):
            continue
        val = getattr(mod, key)
        if key == 'ROOT':
            cfg.root = str(val)
        elif key == 'TARGETS':
            cfg.targets = [str(t).replace('\\', '/') for t in (val or [])]
        elif key == 'EXCLUDE_DIRS':
            cfg.exclude_dirs = set(val or ())
        elif key == 'EXCLUDE_FILES':
            cfg.exclude_files = list(val or ())
        elif key == 'INCLUDE_PATHS':
            cfg.include_paths = [str(p).replace('\\', '/') for p in (val or ())]
        elif key == 'NAMES_DOWNSAMPLE_PREFIXES':
            cfg.names_downsample = [str(p).replace('\\', '/') for p in (val or ())]
        elif key == 'RPC_CHANNELS':
            cfg.rpc_channels = dict(val or {})
        elif key == 'INDEX_PROFILE_RULES':
            cfg.index_profile_rules = [tuple(item) for item in (val or ())]
        elif key == 'DB_PATH':
            cfg.db_path = str(val)
        elif key == 'UI_TBUI_ROOT':
            cfg.ui_tbui_root = str(val)
        elif key == 'UI_CSB_ROOT':
            cfg.ui_csb_root = str(val)
        elif key == 'UI_INDEX_DB':
            cfg.ui_index_db = str(val)


def _apply_custom_seeds(cfg, mod):
    # seeds 为 dict 合并语义：custom 键优先（覆盖内置同键种子）
    cfg.ret_seeds.update(getattr(mod, 'RET_SEEDS', None) or {})
    cfg.attr_seeds.update(getattr(mod, 'ATTR_SEEDS', None) or {})


def _apply_custom_facts(cfg, path):
    mod = _load_py_file(path, 'qcodemap_custom_facts')
    hooks_cls = None
    for name in dir(mod):
        obj = getattr(mod, name)
        if isinstance(obj, type) and issubclass(obj, FactsHooks) and obj is not FactsHooks:
            hooks_cls = obj
            break
    if hooks_cls is not None:
        cfg.hooks = hooks_cls()


def _apply_custom_ui_profile(cfg, custom_dir):
    """加载 custom/ui_profile.py（可选）：资源绑定查询的项目词汇层。

    缺失时 cfg.ui_profile 保持 None，ui-refs 以无 profile 模式降级
    （仅输出绑定事实，不做资源树配对）。
    """
    path = os.path.join(custom_dir, 'ui_profile.py')
    if not os.path.exists(path):
        return
    mod = _load_py_file(path, 'qcodemap_custom_ui_profile')
    profile_cls = None
    for name in dir(mod):
        obj = getattr(mod, name)
        if isinstance(obj, type) and name == 'Profile':
            profile_cls = obj
            break
    if profile_cls is not None:
        cfg.ui_profile = profile_cls()


def load_config(root=None, targets=None, db_path=None, custom_dir=None):
    """加载配置；三个位置参数是 CLI 级覆盖，优先级最高。"""
    cfg = Config()
    cdir = Path(custom_dir or os.environ.get('QCODEMAP_CUSTOM') or (PROJECT_DIR / 'custom'))
    cfg.custom_dir = str(cdir)
    if (cdir / 'config.py').is_file():
        _apply_custom_config(cfg, _load_py_file(cdir / 'config.py', 'qcodemap_custom_config'))
    if (cdir / 'seeds.py').is_file():
        _apply_custom_seeds(cfg, _load_py_file(cdir / 'seeds.py', 'qcodemap_custom_seeds'))
    if (cdir / 'facts.py').is_file():
        _apply_custom_facts(cfg, cdir / 'facts.py')
    _apply_custom_ui_profile(cfg, str(cdir))
    # CLI 覆盖
    if root:
        cfg.root = str(root)
    if targets:
        cfg.targets = [t.replace('\\', '/') for t in targets]
        cfg.targets_overridden = True
    if db_path:
        cfg.db_path = str(db_path)
    return cfg
