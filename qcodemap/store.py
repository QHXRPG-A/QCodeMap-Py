# -*- coding: utf-8 -*-
"""SQLite 存储层：DDL、文件登记（mtime）、按文件级联清理、edges 缓存。

表设计要点：
- names 是唯一的大表（全量约 558 万行），file 列用 files.id 整型引用；
  其余事实表行数小，file 列直接存相对路径，查询直观。
- 除 names 外所有事实表都带 file 列，删除/重扫一个文件时按 file 级联清理。
- edges 是查询结果缓存（P1），按源文件 mtime 在查询期核对失效，不在建库期清理。
"""

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 8

DDL = '''
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files(
    id INTEGER PRIMARY KEY, path TEXT UNIQUE, mtime REAL,
    parse_ok INTEGER NOT NULL DEFAULT 1,
    profile TEXT NOT NULL DEFAULT 'full');
CREATE TABLE IF NOT EXISTS names(name TEXT, file INT, line INT, col INT);
CREATE INDEX IF NOT EXISTS idx_names_name ON names(name);
CREATE INDEX IF NOT EXISTS idx_names_file ON names(file);
CREATE TABLE IF NOT EXISTS defs(file TEXT, line INT, class TEXT, name TEXT);
CREATE INDEX IF NOT EXISTS idx_defs_name ON defs(name);
CREATE TABLE IF NOT EXISTS classes(file TEXT, name TEXT, bases TEXT, line INT);
CREATE INDEX IF NOT EXISTS idx_classes_name ON classes(name);
CREATE TABLE IF NOT EXISTS imports(
    file TEXT, module TEXT, name TEXT, alias TEXT, line INT, scope TEXT);
CREATE INDEX IF NOT EXISTS idx_imports_file ON imports(file);
CREATE TABLE IF NOT EXISTS attr(file TEXT, class TEXT, attr TEXT, type TEXT);
CREATE INDEX IF NOT EXISTS idx_attr_class ON attr(class);
CREATE TABLE IF NOT EXISTS global_assign(base TEXT, attr TEXT, class TEXT, file TEXT);
CREATE INDEX IF NOT EXISTS idx_global ON global_assign(base, attr);
CREATE TABLE IF NOT EXISTS ret(module TEXT, func TEXT, type TEXT, file TEXT);
CREATE TABLE IF NOT EXISTS comp_raw(file TEXT, host TEXT, kind TEXT, value TEXT);
CREATE TABLE IF NOT EXISTS comp(host TEXT, host_file TEXT, comp TEXT, comp_file TEXT);
CREATE INDEX IF NOT EXISTS idx_comp_host ON comp(host);
CREATE TABLE IF NOT EXISTS rpc(file TEXT, line INT, chan TEXT, method TEXT, stub TEXT);
CREATE INDEX IF NOT EXISTS idx_rpc_method ON rpc(method);
CREATE INDEX IF NOT EXISTS idx_rpc_file ON rpc(file);
CREATE TABLE IF NOT EXISTS receiver_fact(
    file TEXT, line INT, expr TEXT, type TEXT, confidence TEXT, reason TEXT);
CREATE INDEX IF NOT EXISTS idx_receiver_file_line ON receiver_fact(file, line);
CREATE INDEX IF NOT EXISTS idx_receiver_type ON receiver_fact(type);
CREATE TABLE IF NOT EXISTS rpc_handler(
    file TEXT, line INT, chan TEXT, method TEXT, endpoint TEXT,
    confidence TEXT, reason TEXT);
CREATE INDEX IF NOT EXISTS idx_rpc_handler_method ON rpc_handler(method);
CREATE TABLE IF NOT EXISTS pubsub(file TEXT, line INT, side TEXT, event TEXT, func TEXT, cls TEXT);
CREATE INDEX IF NOT EXISTS idx_pubsub_event ON pubsub(event);
CREATE INDEX IF NOT EXISTS idx_pubsub_file ON pubsub(file);
CREATE TABLE IF NOT EXISTS callback_raw(
    file TEXT, line INT, class TEXT, kind TEXT, source TEXT, target TEXT);
CREATE INDEX IF NOT EXISTS idx_callback_target ON callback_raw(target);
CREATE TABLE IF NOT EXISTS ui_binding(
    file TEXT, line INT, kind TEXT, key TEXT, receiver TEXT, cls TEXT, func TEXT);
CREATE INDEX IF NOT EXISTS idx_ui_binding_key ON ui_binding(key);
CREATE INDEX IF NOT EXISTS idx_ui_binding_file ON ui_binding(file);
CREATE INDEX IF NOT EXISTS idx_ui_binding_kind_key ON ui_binding(kind, key);
CREATE TABLE IF NOT EXISTS binding(
    file TEXT, line INT, domain TEXT, owner TEXT, relation TEXT, target TEXT,
    variant TEXT, confidence TEXT, reason TEXT);
CREATE INDEX IF NOT EXISTS idx_binding_owner ON binding(domain, owner, variant);
CREATE INDEX IF NOT EXISTS idx_binding_target ON binding(domain, relation, target);
CREATE INDEX IF NOT EXISTS idx_binding_file ON binding(file);
CREATE TABLE IF NOT EXISTS edges(
    name TEXT, def_file TEXT, def_line INT, kind TEXT, payload TEXT,
    PRIMARY KEY(name, def_file, def_line, kind));
'''

# file 列直接存路径、可直接级联删除的表（names 走 file_id 单独处理）
CASCADE_TABLES = ('defs', 'classes', 'imports', 'attr', 'global_assign',
                  'comp_raw', 'ret', 'rpc', 'receiver_fact', 'rpc_handler',
                  'pubsub', 'callback_raw', 'ui_binding', 'binding')


class Store(object):

    def __init__(self, db_path, read_only=False):
        self.path = str(db_path)
        self.read_only = bool(read_only)
        if self.read_only:
            path = Path(self.path).resolve()
            if not path.exists():
                raise RuntimeError('索引库不存在，请先执行 qcodemap build: %s' % path)
            self.con = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
            self.con.execute('PRAGMA query_only=ON')
            self.con.execute('PRAGMA busy_timeout=5000')
            self._validate_schema(require_version=True)
            return

        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(self.path)
        # WAL 允许查询读取构建前的完整快照；NORMAL 对可再生缓存兼顾吞吐与完整性。
        self.con.execute('PRAGMA busy_timeout=5000')
        self.con.execute('PRAGMA journal_mode=WAL')
        self.con.execute('PRAGMA synchronous=NORMAL')
        self.con.executescript(DDL)
        self._validate_schema(require_version=False)
        self.set_meta('schema_version', str(SCHEMA_VERSION))

    @classmethod
    def open_reader(cls, db_path):
        """打开不执行 DDL/meta 写入的只读查询连接。"""
        return cls(db_path, read_only=True)

    @classmethod
    def open_writer(cls, db_path):
        """打开建库/测试写连接；保留构造器默认行为的显式入口。"""
        return cls(db_path, read_only=False)

    def _validate_schema(self, require_version):
        try:
            ver = self.get_meta('schema_version')
        except sqlite3.OperationalError as exc:
            raise RuntimeError('索引库缺少 QCodeMap schema，请先执行 qcodemap build') from exc
        if require_version and ver is None:
            raise RuntimeError('索引库缺少 schema_version，请执行 qcodemap build --rebuild')
        if ver is not None and int(ver) != SCHEMA_VERSION:
            raise RuntimeError('缓存库 schema 版本 %s 与当前 %d 不符，'
                               'rebuild=True 或 CLI --rebuild 重建（若已带该参数，'
                               '说明旧库连接残留，删除 cache 库文件后重试）'
                               % (ver, SCHEMA_VERSION))

    # ---- 基础 ----

    def get_meta(self, key):
        row = self.con.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key, value):
        self.con.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, str(value)))

    def commit(self):
        if self.read_only:
            raise RuntimeError('只读 Store 不允许 commit')
        self.con.commit()

    def close(self):
        if not self.read_only:
            self.con.commit()
        self.con.close()

    # ---- 文件级增量 ----

    def all_files(self):
        """{path: (file_id, mtime)}"""
        return {p: (fid, mt) for fid, p, mt in
                self.con.execute('SELECT id, path, mtime FROM files')}

    def remove_file(self, rel):
        """级联删除一个文件的全部索引行（不存在时为无害空操作）。"""
        row = self.con.execute('SELECT id FROM files WHERE path=?', (rel,)).fetchone()
        if row:
            self.con.execute('DELETE FROM names WHERE file=?', (row[0],))
            self.con.execute('DELETE FROM files WHERE id=?', (row[0],))
        for table in CASCADE_TABLES:
            self.con.execute('DELETE FROM %s WHERE file=?' % table, (rel,))

    def insert_file(self, rel, mtime, parse_ok=True, profile='full'):
        cur = self.con.execute(
            'INSERT INTO files(path, mtime, parse_ok, profile) VALUES(?,?,?,?)',
            (rel, mtime, 1 if parse_ok else 0, profile))
        return cur.lastrowid

    def insert_rows(self, fid, rows):
        """写入 scanner.scan_file 的结果。

        约定：除 names 外，scan 结果里的行都是对应表的完整行（file 列即 rel，
        与 hooks 协议一致）；names 行是 (name, line, col)，这里补 file_id。
        """
        if rows.get('names'):
            self.con.executemany('INSERT INTO names VALUES(?,?,?,?)',
                                 [(n, fid, ln, col) for (n, ln, col) in rows['names']])
        # 其余事实表行 = 完整表行；列数以 DDL 为准，按首行宽度生成占位符
        for table in ('defs', 'classes', 'imports', 'attr',
                      'global_assign', 'ret', 'comp_raw', 'rpc',
                      'receiver_fact', 'rpc_handler', 'pubsub', 'callback_raw',
                      'ui_binding', 'binding'):
            data = rows.get(table)
            if data:
                marks = ','.join('?' * len(data[0]))
                self.con.executemany('INSERT INTO %s VALUES(%s)' % (table, marks), data)

    # ---- 统计 ----

    def count(self, table):
        return self.con.execute('SELECT COUNT(*) FROM %s' % table).fetchone()[0]

    def parse_failed_count(self):
        return self.con.execute(
            'SELECT COUNT(*) FROM files WHERE parse_ok=0').fetchone()[0]

    def parse_failed_files(self, rels=None):
        """parse_ok=0 的文件清单（有序）；rels 给出时限定在该集合内。

        覆盖率契约的数据源：查询侧据此给出 partial 状态与 issues 出处。
        """
        if rels is None:
            return [p for (p,) in self.con.execute(
                'SELECT path FROM files WHERE parse_ok=0 ORDER BY path')]
        rels = sorted(set(rels))
        out = []
        for i in range(0, len(rels), 500):
            chunk = rels[i:i + 500]
            q = ('SELECT path FROM files WHERE parse_ok=0 AND path IN (%s)'
                 % ','.join('?' * len(chunk)))
            out.extend(p for (p,) in self.con.execute(q, chunk))
        return sorted(out)
