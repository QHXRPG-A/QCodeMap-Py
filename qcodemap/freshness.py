# -*- coding: utf-8 -*-
"""查询前索引新鲜度检查与增量刷新。"""

import time

from qcodemap import build as build_mod
from qcodemap.fingerprint import analysis_fingerprint
from qcodemap.store import Store

_THROTTLE = {}


def ensure_fresh(cfg, mode='auto', throttle_seconds=0.0):
    if mode not in ('auto', 'check', 'off'):
        raise ValueError('refresh 必须是 auto/check/off')
    key = cfg.db_path
    now = time.monotonic()
    cached = _THROTTLE.get(key)
    if throttle_seconds and cached and now - cached[0] < throttle_seconds:
        return dict(cached[1])

    store = Store.open_reader(cfg.db_path)
    try:
        stored_fp = store.get_meta('analysis_fingerprint')
        current_fp = analysis_fingerprint(cfg)
        if stored_fp != current_fp:
            raise RuntimeError(
                '索引配置/schema/custom hook 指纹不匹配；拒绝返回旧结果。'
                '请执行 qcodemap build --rebuild')
        built_at = store.get_meta('built_at')
        coverage_status = store.get_meta('coverage_status') or 'complete'
        known = store.all_files()
        profiles = dict(store.con.execute(
            'SELECT profile, COUNT(*) FROM files GROUP BY profile'))
    finally:
        store.close()

    refreshed = False
    drift = []
    scan_elapsed = 0.0
    if mode != 'off':
        t0 = time.monotonic()
        disk = build_mod.collect_files(cfg)
        scan_elapsed = time.monotonic() - t0
        drift = sorted(
            set(disk) ^ set(known)
            | {rel for rel in set(disk) & set(known)
               if disk[rel] != known[rel][1]})
        if mode == 'auto' and drift:
            build_mod.build(cfg, verbose=False, scope_rels=drift)
            refreshed = True
            store = Store.open_reader(cfg.db_path)
            try:
                built_at = store.get_meta('built_at')
                coverage_status = store.get_meta('coverage_status') or 'complete'
                profiles = dict(store.con.execute(
                    'SELECT profile, COUNT(*) FROM files GROUP BY profile'))
            finally:
                store.close()

    meta = {
        'built_at': int(built_at) if built_at else None,
        'refreshed': refreshed,
        'drift_count': len(drift),
        'refresh_mode': mode,
        'scan_elapsed': round(scan_elapsed, 3),
        'config_fingerprint': current_fp,
        'scope': {'status': coverage_status, 'profiles': profiles},
        # 兼容 v0.9 消费方；它表示 build scope，不是查询 AST coverage。
        'coverage': coverage_status,
        'profiles': profiles,
    }
    if mode == 'check' and drift:
        meta['stale'] = True
        meta['drift_files'] = drift[:50]
    _THROTTLE[key] = (now, dict(meta))
    return meta


def attach_index(result, index_meta):
    if isinstance(result, dict):
        result['index'] = index_meta
    return result
