# -*- coding: utf-8 -*-
"""项目诊断通用入口；具体规则完全由 custom hook 提供。"""

import time


def diagnose(store, cfg):
    t0 = time.time()
    hooks = getattr(cfg, 'hooks', None)
    issues = list(hooks.project_diagnostics(store, cfg)) if hooks else []
    issues.sort(key=lambda item: (
        item.get('severity', ''), item.get('file', ''), item.get('line', 0),
        item.get('code', '')))
    return {
        'schema_version': 'qcodemap.diagnostics/v1',
        'issues': issues,
        'count': len(issues),
        'elapsed': round(time.time() - t0, 3),
    }
