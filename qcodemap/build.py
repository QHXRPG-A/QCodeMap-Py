# -*- coding: utf-8 -*-
"""建库：目录遍历 + mtime 增量 + pass2 跨文件组件解析。

增量语义：path 集合与库内一致、mtime 未变 -> 跳过；新增/变更 -> 重扫；
消失 -> 级联删除。pass2（comp_raw -> comp）在任何变更后整体重跑：
组件行数量级小（万级以下），全量重算远比精确增量可靠。
"""

import ast
import concurrent.futures
import contextlib
import fnmatch
import json
import os
import sqlite3
import sys
import time

from qcodemap.fingerprint import analysis_fingerprint
from qcodemap import scanner
from qcodemap.store import Store

_WORKER_HOOKS = None


@contextlib.contextmanager
def build_lock(db_path, timeout=60.0):
    """跨进程串行化同一索引库的 writer；reader 仍可读取 WAL 快照。"""
    lock_path = str(db_path) + '.build.lock'
    os.makedirs(os.path.dirname(os.path.abspath(lock_path)), exist_ok=True)
    lock_file = open(lock_path, 'a+b')
    acquired = False
    started = time.monotonic()
    try:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b'\0')
            lock_file.flush()
        while not acquired:
            lock_file.seek(0)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock_file.fileno(),
                                fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except (OSError, IOError):
                if time.monotonic() - started >= timeout:
                    raise RuntimeError('等待索引构建锁超时，请稍后重试: %s' % lock_path)
                time.sleep(0.05)
        yield
    finally:
        if acquired:
            lock_file.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def _init_scan_worker(custom_dir):
    global _WORKER_HOOKS
    from qcodemap.config import load_config
    _WORKER_HOOKS = load_config(custom_dir=custom_dir).hooks


def _scan_worker(job):
    rel, path, mtime, downsample, profile = job
    rows = scanner.scan_file(rel, path, _WORKER_HOOKS,
                             downsample=downsample, profile=profile)
    return rel, mtime, profile, rows


def _excluded_dirs(dirnames, exclude_dirs):
    return [d for d in dirnames if d in exclude_dirs]


def _excluded_file(fn, patterns):
    return any(fnmatch.fnmatch(fn, pat) for pat in patterns)


def _included(rel, include_paths):
    """路径级放行：rel 本身或其祖先命中 include_paths。"""
    if not include_paths:
        return False
    for p in include_paths:
        if rel == p or rel.startswith(p.rstrip('/') + '/'):
            return True
    return False


def collect_files(cfg):
    """按配置遍历磁盘，返回 {rel_path: mtime}。

    排除规则：INCLUDE_PATHS 命中的路径（含子树）优先放行，跳过目录名/文件名
    排除；其余按 EXCLUDE_DIRS（任意层级目录名）与 EXCLUDE_FILES 过滤。
    include 路径可能不在 TARGETS 覆盖范围内，单独补扫。
    """
    root = cfg.root
    out = {}
    bases = [os.path.join(root, t) for t in cfg.targets] if cfg.targets else [root]
    # include 目标不在任何 target 前缀下时，作为独立遍历根补上
    for p in cfg.include_paths:
        if not any(p == t or p.startswith(t.rstrip('/') + '/')
                   for t in (cfg.targets or [])):
            bases.append(os.path.join(root, p))
    for base in bases:
        if os.path.isfile(base):  # 单文件 include（TARGETS 外的散置文件）
            rel = os.path.relpath(base, root).replace('\\', '/')
            try:
                out[rel] = os.path.getmtime(base)
            except OSError:
                pass
            continue
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            rel_dir = os.path.relpath(dirpath, root).replace('\\', '/')
            # 剪枝前对每个子目录做 include 判断：命中放行的子树整体保留
            #（「target 内被目录名排除但被路径放行」的场景，如 src/data）
            dirnames[:] = [d for d in dirnames
                           if _included(rel_dir + '/' + d, cfg.include_paths)
                           or d not in cfg.exclude_dirs]
            for fn in filenames:
                if not fn.endswith('.py'):
                    continue
                p = os.path.join(dirpath, fn)
                rel = os.path.relpath(p, root).replace('\\', '/')
                # include 命中的路径豁免文件级排除（data 目录里的 *_origin.py
                # 明文版要放行），其余按 EXCLUDE_FILES 过滤
                if not _included(rel, cfg.include_paths) \
                        and _excluded_file(fn, cfg.exclude_files):
                    continue
                if not _included(rel, cfg.include_paths) \
                        and _excluded_dirs(os.path.dirname(rel).split('/'),
                                           cfg.exclude_dirs):
                    continue
                try:
                    out[rel] = os.path.getmtime(p)
                except OSError:
                    continue
    return out


_ALL_TABLES = ('meta', 'files', 'names', 'defs', 'classes', 'imports', 'attr',
               'global_assign', 'ret', 'comp_raw', 'comp', 'rpc', 'pubsub',
               'receiver_fact', 'rpc_handler', 'endpoint_alias',
               'callback_raw', 'ui_binding', 'binding', 'edges')


def _prepare_rebuild(db_path):
    """rebuild 前置：独立连接 DROP 全部表（含旧 schema 结构），再由 Store
    的 DDL 按当前 schema 重建——绕开初始化期版本校验的同时保证表结构更新。
    """
    con = sqlite3.connect(db_path)
    corrupt = False
    try:
        for table in _ALL_TABLES:
            con.execute('DROP TABLE IF EXISTS %s' % table)
        con.commit()
    except sqlite3.OperationalError as exc:
        if 'locked' in str(exc).lower():
            raise RuntimeError('索引库正在被查询进程占用，请稍后重试 rebuild') from exc
        corrupt = True
    except sqlite3.DatabaseError:
        corrupt = True
    finally:
        try:
            con.close()
        except sqlite3.DatabaseError:
            corrupt = True
    if corrupt:
        # 索引库是可再生缓存；rebuild 已明确授权丢弃旧库。
        for path in (db_path, db_path + '-journal', db_path + '-wal', db_path + '-shm'):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass


def _profile_for(cfg, rel):
    for pattern, profile in getattr(cfg, 'index_profile_rules', ()):
        if fnmatch.fnmatch(rel, pattern):
            if profile not in ('full', 'semantic-only'):
                raise ValueError('未知索引 profile: %s (%s)' % (profile, pattern))
            return profile
    return 'full'


def _in_selected_scope(cfg, rel):
    return any(rel == t or rel.startswith(t.rstrip('/') + '/')
               for t in cfg.targets) or _included(rel, cfg.include_paths)


def build(cfg, rebuild=False, verbose=True, scope_rels=None, vacuum=False,
          _locked=False):
    """串行化 writer 后执行建库；_locked 仅供已持锁的内部调用。"""
    if _locked:
        return _build(cfg, rebuild=rebuild, verbose=verbose,
                      scope_rels=scope_rels, vacuum=vacuum)
    with build_lock(cfg.db_path):
        return _build(cfg, rebuild=rebuild, verbose=verbose,
                      scope_rels=scope_rels, vacuum=vacuum)


def _build(cfg, rebuild=False, verbose=True, scope_rels=None, vacuum=False):
    """执行（增量）建库，返回统计 dict。rebuild 先清 meta 再建库，
    保证 schema 版本校验（Store 初始化期）不会拦下重建请求。"""
    t0 = time.time()
    stages = {}
    last_progress = [t0]

    def progress(phase, current=None, total=None, force=False):
        now = time.time()
        if not verbose or (not force and now - last_progress[0] < 5):
            return
        suffix = '' if current is None else ' %d/%d' % (current, total)
        sys.stderr.write('[qcodemap] %s%s  %.1fs\n' % (phase, suffix, now - t0))
        sys.stderr.flush()
        last_progress[0] = now

    if rebuild:
        _prepare_rebuild(cfg.db_path)
    store = Store.open_writer(cfg.db_path)
    try:
        previous_fingerprint = store.get_meta('analysis_fingerprint')
        previous_coverage = store.get_meta('coverage_status')
        if (getattr(cfg, 'targets_overridden', False) and not rebuild
                and previous_fingerprint is None):
            raise RuntimeError(
                '无完整基库时 build --targets 不会默认建子集库；'
                '请先完整 build，或显式使用 --rebuild --targets')
        if rebuild:
            _drop_all(store)
        phase_t0 = time.time()
        disk = collect_files(cfg)
        stages['collect'] = round(time.time() - phase_t0, 3)
        progress('collect', len(disk), len(disk), force=True)
        known = store.all_files()
        if scope_rels is not None:
            active = {r.replace('\\', '/') for r in scope_rels}
            disk = {rel: mt for rel, mt in disk.items() if rel in active}
            delete_scope = active
        elif getattr(cfg, 'targets_overridden', False) and not rebuild:
            delete_scope = {rel for rel in known if _in_selected_scope(cfg, rel)}
        else:
            delete_scope = set(known)
        n_new = n_upd = n_del = 0
        for rel in sorted(delete_scope):
            if rel not in disk:
                store.remove_file(rel)
                n_del += 1
        phase_t0 = time.time()
        scan_rels = sorted(disk)
        scan_jobs = []
        for rel in scan_rels:
            mtime = disk[rel]
            old = known.get(rel)
            profile = _profile_for(cfg, rel)
            old_profile_row = store.con.execute(
                'SELECT profile FROM files WHERE path=?', (rel,)).fetchone()
            old_profile = old_profile_row[0] if old_profile_row else None
            if old is not None and old[1] == mtime and old_profile == profile:
                continue
            if old is not None:
                store.remove_file(rel)
                n_upd += 1
            else:
                n_new += 1
            scan_jobs.append((
                rel, os.path.join(cfg.root, rel.replace('/', os.sep)), mtime,
                any(rel.startswith(p) for p in cfg.names_downsample), profile))

        def insert_result(index, result):
            rel, mtime, profile, rows = result
            fid = store.insert_file(rel, mtime,
                                    parse_ok=bool(rows.get('parse_ok', True)),
                                    profile=profile)
            store.insert_rows(fid, rows)
            progress('scan', index, len(scan_jobs))

        if len(scan_jobs) > 100 and getattr(cfg, 'custom_dir', None):
            workers = min(4, max(2, os.cpu_count() or 2))
            with concurrent.futures.ProcessPoolExecutor(
                    max_workers=workers, initializer=_init_scan_worker,
                    initargs=(cfg.custom_dir,)) as executor:
                results = executor.map(_scan_worker, scan_jobs, chunksize=4)
                for index, result in enumerate(results, 1):
                    insert_result(index, result)
        else:
            for index, job in enumerate(scan_jobs, 1):
                rel, path, mtime, downsample, profile = job
                rows = scanner.scan_file(
                    rel, path, cfg.hooks, downsample=downsample, profile=profile)
                insert_result(index, (rel, mtime, profile, rows))
        stages['scan'] = round(time.time() - phase_t0, 3)
        n_raw = store.count('comp_raw')
        phase_t0 = time.time()
        if n_new or n_upd or n_del:
            store.con.execute('DELETE FROM comp')
            for row in _resolve_comps(store, cfg):
                store.con.execute('INSERT INTO comp VALUES(?,?,?,?)', row)
        stages['pass2'] = round(time.time() - phase_t0, 3)
        progress('pass2', force=True)
        store.set_meta('built_at', str(int(time.time())))
        store.set_meta('root', cfg.root)
        if getattr(cfg, 'targets_overridden', False) and not rebuild:
            store.set_meta('analysis_fingerprint', previous_fingerprint)
            store.set_meta('coverage_status', previous_coverage or 'complete')
        else:
            store.set_meta('analysis_fingerprint', analysis_fingerprint(cfg))
            coverage_status = ('targeted' if getattr(cfg, 'targets_overridden', False)
                               else 'complete')
            store.set_meta('coverage_status', coverage_status)
            store.set_meta('targets_json', json.dumps(cfg.targets, ensure_ascii=False))
        # build 收尾钩子：custom 顺带刷新引擎管线外的资源索引（默认无操作）
        phase_t0 = time.time()
        if cfg.hooks is None:
            stages['build_done'] = 'skipped: no custom hooks'
        else:
            try:
                cfg.hooks.build_done(store, cfg, stages)
            except Exception as exc:  # 资源索引失败不阻断主库产出
                sys.stderr.write('[qcodemap] build_done hook failed: %r\n' % (exc,))
                sys.stderr.flush()
                stages['build_done'] = 'failed: %r' % (exc,)
            else:
                stages['build_done'] = round(time.time() - phase_t0, 3)
        phase_t0 = time.time()
        store.commit()
        stages['commit'] = round(time.time() - phase_t0, 3)
        progress('commit', force=True)
        do_vacuum = rebuild or vacuum
        if do_vacuum:
            phase_t0 = time.time()
            store.con.execute('VACUUM')
            stages['vacuum'] = round(time.time() - phase_t0, 3)
            progress('vacuum', force=True)
        else:
            stages['vacuum'] = 0.0
        stats = {
            'files': store.count('files'), 'new': n_new, 'updated': n_upd, 'deleted': n_del,
            'comp_raw': n_raw, 'comp': store.count('comp'),
            'callback_raw': store.count('callback_raw'),
            'names': store.count('names'), 'parse_failed': store.parse_failed_count(),
            'stages': stages, 'vacuumed': do_vacuum,
            'elapsed': round(time.time() - t0, 1),
        }
        if verbose:
            sys.stderr.write(
                'build 完成: %d 文件 (+%d ~%d -%d) names=%d 组件边=%d/%d '
                'ast失败=%d  %.1fs\n'
                % (stats['files'], n_new, n_upd, n_del,
                   stats['names'], stats['comp'], stats['comp_raw'],
                   stats['parse_failed'], stats['elapsed']))
            sys.stderr.flush()
        return stats
    finally:
        store.close()


def _drop_all(store):
    for table in ('files', 'names', 'defs', 'classes', 'imports', 'attr',
                  'global_assign', 'ret', 'comp_raw', 'comp', 'rpc', 'pubsub',
                  'receiver_fact', 'rpc_handler', 'endpoint_alias', 'callback_raw',
                  'ui_binding', 'binding', 'edges'):
        store.con.execute('DELETE FROM %s' % table)


# ---- 懒刷新（P4-5） ----

DRIFT_CHECK_CAP = 1000


def drift_check(store, cfg, rels, cap=DRIFT_CHECK_CAP):
    """懒刷新检测：rels 中磁盘 mtime 与库内漂移（或库外新文件）的清单。

    只检测不重建，触发 build 由调用方决定（须先关掉本连接再 build，
    避免两连接并发写锁库）。rels 为空返回 []；超过 cap（大目录 scope，
    stat 本身成风暴且漂移概率趋近 1）返回 None 表示放弃检测。
    """
    rels = [r.replace('\\', '/') for r in (rels or []) if r]
    if not rels:
        return []
    rels = list(dict.fromkeys(rels))
    if len(rels) > cap:
        return None
    known = store.all_files()
    out = []
    for rel in rels:
        try:
            disk = os.path.getmtime(os.path.join(cfg.root, rel.replace('/', os.sep)))
        except OSError:
            if rel in known:
                out.append(rel)
            continue
        old = known.get(rel)
        if old is None or disk != old[1]:
            out.append(rel)
    return out


# ---- pass2: comp_raw -> comp ----

def _resolve_comps(store, cfg):
    """组件注入原始行 -> (host, host_file, comp_class, comp_file) 集合。

    ref:  同文件类或 import 名字指向的模块；
    attr: mod.Cls 形态，按 import 前缀定位文件；
    importall: 钩子给出成员类名，在包目录下全部模块里找定义。
    """
    con = store.con
    # 模块映射基于全部已索引文件（__init__.py 可能没有类定义）
    mods = {scanner.module_of(rel): rel
            for (rel,) in con.execute('SELECT path FROM files')}
    hooks = cfg.hooks
    out = set()
    for (file, host, kind, value) in con.execute(
            'SELECT file, host, kind, value FROM comp_raw').fetchall():
        if kind == 'ref':
            hit = con.execute('SELECT 1 FROM classes WHERE file=? AND name=?',
                              (file, value)).fetchone()
            if hit:
                out.add((host, file, value, file))
                continue
            for f in _import_target_files(con, mods, file, value):
                if con.execute('SELECT 1 FROM classes WHERE file=? AND name=?',
                               (f, value)).fetchone():
                    out.add((host, file, value, f))
                    break
        elif kind == 'attr':
            base, cls = value.rsplit('.', 1)
            for f in _import_target_files(con, mods, file, base):
                if con.execute('SELECT 1 FROM classes WHERE file=? AND name=?',
                               (f, cls)).fetchone():
                    out.add((host, file, cls, f))
                    break
        elif kind == 'importall':
            pkg_mod = _resolve_module(con, mods, file, value)
            if not pkg_mod or not hooks:
                continue
            member_names = hooks.importall_members(host) or [host]
            # 精确档：包 __init__.py 已索引时只查其 from . import X 导出清单
            sub_rels = None
            init_rel = mods.get(pkg_mod)
            if init_rel and init_rel.endswith('__init__.py'):
                try:
                    init_tree = ast.parse(scanner.read_source(
                        os.path.join(cfg.root, init_rel.replace('/', os.sep))))
                except SyntaxError:
                    init_tree = None
                if init_tree is not None:
                    sub_rels = []
                    for st in ast.walk(init_tree):
                        if isinstance(st, ast.ImportFrom) and st.module is None and st.level == 1:
                            for a in st.names:
                                sub = mods.get('%s.%s' % (pkg_mod, a.name))
                                if sub:
                                    sub_rels.append(sub)
            if sub_rels is None:
                # 退路：包目录下全部模块（__init__ 未索引/未导出时）
                prefix = pkg_mod.replace('.', '/') + '/'
                sub_rels = [rel2 for rel2 in mods.values()
                            if rel2.startswith(prefix) and rel2.endswith('.py')
                            and '__init__' not in rel2]
            for rel2 in sub_rels:
                for mname in member_names:
                    if con.execute('SELECT 1 FROM classes WHERE file=? AND name=?',
                                   (rel2, mname)).fetchone():
                        out.add((host, file, mname, rel2))
    return sorted(out)


def _import_target_files(con, mods, from_file, name):
    """from_file 的 import 语境下，名字 name 可能的定义文件候选（有序）。

    覆盖三种形态：from M import name（name 或为子模块 -> M/name.py，或为
    M.py 内的名字）、import M.name as name（-> M/name.py）、import name（-> name.py）。
    """
    cands = []
    for (m, n, a) in con.execute(
            'SELECT module,name,alias FROM imports WHERE file=? AND scope=?',
            (from_file, '')).fetchall():
        if not (n == name or a == name):
            continue
        if n:
            for dotted_mod in ('%s.%s' % (m, n), m):
                rel = mods.get(dotted_mod)
                if rel and rel not in cands:
                    cands.append(rel)
        elif m:
            rel = mods.get(m)
            if rel and rel not in cands:
                cands.append(rel)
    if not cands:
        rel = mods.get(name)
        if rel:
            cands.append(rel)
    return cands


def _resolve_module(con, mods, from_file, ref):
    """from_file 的 import 语境下把 ref（模块名/别名）解析为绝对 dotted 模块。"""
    if ref in mods:
        return ref
    fid_mod = scanner.module_of(from_file)
    for (m, n, a) in con.execute(
            'SELECT module,name,alias FROM imports WHERE file=? AND scope=?',
            (from_file, '')).fetchall():
        if n == ref or a == ref:
            full = '%s.%s' % (m, n) if n else m
            return full if full in mods else (m if m in mods else None)
        if m == ref:
            return m
    guess = '%s.%s' % (fid_mod.rsplit('.', 1)[0] if '.' in fid_mod else '', ref)
    return guess if guess in mods else None
