# -*- coding: utf-8 -*-
"""QCodeMap 规模探测：全量核心目录建倒排索引，测耗时/体量/查询速度。

不跑语义解析（已由 test_feasibility 验证），只验证阶段1的规模可行性。
"""
import os
import re
import sqlite3
import time
from pathlib import Path

# 孵化案例代码库根与产物库路径（运行时按环境替换）
ROOT = Path(os.environ.get('QCODEMAP_DEMO_ROOT', '.'))
DB = Path(os.environ.get('QCODEMAP_DEMO_DB', 'cache/scale_test.db'))
TOKEN_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')

# codemap 实测排除清单（目录名命中即跳过）
EXCLUDE = {'__pycache__', 'Lib', 'Lib3', 'venv', '_ide_stubs', 'release_data',
           '.git', '.idea', '.vscode', '.codemaker', '.codex', '.codex_tmp',
           '.cursor', '.zcode', 'openspec', 'docs', 'outputs', 'LocalTemp',
           'data', 'data_lang'}
TARGETS = ['gclient', 'gserver', 'gshare', 'HelenFramework', 'SunshineSDK', 'Montage', 'UGC']

if DB.exists():
    DB.unlink()
con = sqlite3.connect(str(DB))
con.executescript('''
CREATE TABLE names(name TEXT, file INT, line INT, col INT);
CREATE INDEX idx_n ON names(name);
CREATE TABLE files(id INTEGER PRIMARY KEY, path TEXT);
''')

t0 = time.time()
nf = 0
batch = []
for t in TARGETS:
    for dirpath, dirnames, filenames in os.walk(ROOT / t):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE]
        for fn in filenames:
            if not fn.endswith('.py'):
                continue
            p = Path(dirpath) / fn
            rel = p.relative_to(ROOT).as_posix()
            nf += 1
            try:
                data = p.read_bytes()
            except OSError:
                continue
            cur = con.execute('INSERT INTO files(path) VALUES(?)', (rel,))
            fid = cur.lastrowid
            for enc in ('utf-8', 'gbk'):
                try:
                    text = data.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            else:
                text = data.decode('utf-8', errors='replace')
            for i, line in enumerate(text.splitlines(), 1):
                for m in TOKEN_RE.finditer(line):
                    batch.append((m.group(0), fid, i, m.start() + 1))
            if len(batch) > 500000:
                con.executemany('INSERT INTO names VALUES(?,?,?,?)', batch)
                batch = []
if batch:
    con.executemany('INSERT INTO names VALUES(?,?,?,?)', batch)
con.commit()
t1 = time.time()

total = con.execute('SELECT COUNT(*) FROM names').fetchone()[0]
print('files=%d names=%d build=%.0fs db=%.0fMB' % (nf, total, t1 - t0, DB.stat().st_size / 1048576))

# 查询基准：三个典型名字 + 冷热
for name in ('GetTeammateInfo', 'OnTeammateAiTakeoverChange', 'Property'):
    tq = time.time()
    rows = con.execute('SELECT f.path, n.line FROM names n JOIN files f ON n.file=f.id WHERE n.name=?',
                       (name,)).fetchall()
    print('query %-30s rows=%d %.3fs' % (name, len(rows), time.time() - tq))
