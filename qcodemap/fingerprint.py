# -*- coding: utf-8 -*-
"""索引兼容性指纹；只描述会改变事实含义或覆盖范围的输入。"""

import hashlib
import inspect
import json
import os

from qcodemap.store import SCHEMA_VERSION

INDEX_FORMAT_VERSION = 2


def analysis_fingerprint(cfg):
    payload = {
        'schema': SCHEMA_VERSION,
        'index_format': INDEX_FORMAT_VERSION,
        'root': os.path.normcase(os.path.abspath(cfg.root)),
        'targets': list(cfg.targets),
        'exclude_dirs': sorted(cfg.exclude_dirs),
        'exclude_files': list(cfg.exclude_files),
        'include_paths': list(cfg.include_paths),
        'profiles': list(getattr(cfg, 'index_profile_rules', ())),
        'ret_seeds': sorted(cfg.ret_seeds.items()),
        'attr_seeds': sorted((str(k), str(v)) for k, v in cfg.attr_seeds.items()),
    }
    h = hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True).encode('utf-8'))
    custom_dir = getattr(cfg, 'custom_dir', None)
    if custom_dir and os.path.isdir(custom_dir):
        for name in sorted(os.listdir(custom_dir)):
            if not name.endswith('.py'):
                continue
            path = os.path.join(custom_dir, name)
            h.update(name.encode('utf-8'))
            with open(path, 'rb') as f:
                h.update(f.read())
    hooks = getattr(cfg, 'hooks', None)
    hook_file = inspect.getsourcefile(type(hooks)) if hooks else None
    if hook_file and os.path.isfile(hook_file) \
            and (not custom_dir or os.path.dirname(hook_file) != os.path.abspath(custom_dir)):
        h.update(os.path.abspath(hook_file).encode('utf-8'))
        with open(hook_file, 'rb') as f:
            h.update(f.read())
    return h.hexdigest()
