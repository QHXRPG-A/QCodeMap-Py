# -*- coding: utf-8 -*-
"""局部 import 回归：结构边、词法别名隔离与 custom 事件归一。"""

import importlib.util
import os
import shutil
import sys
import tempfile

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

from qcodemap import build as build_mod                 # noqa: E402
from qcodemap import pubsub_refs as pubsub_mod          # noqa: E402
from qcodemap import resolve as resolve_mod             # noqa: E402
from qcodemap import structure as structure_mod         # noqa: E402
from qcodemap.config import Config                      # noqa: E402
from qcodemap.store import Store                        # noqa: E402


SOURCES = {
    'pkg/target.py': 'def run():\n    return 1\n',
    'other/target.py': 'def run():\n    return 2\n',
    'pkg/events.py': 'ON_X = 1\n',
    'pkg/caller.py': '''
def first():
    from pkg import target as local_target
    return local_target.run()


def second():
    from other import target as local_target
    return local_target.run()


def publish():
    from pkg import events
    genv.messenger.Broadcast(events.ON_X)
''',
    'pkg/listener.py': '''
from pkg import events


class Listener(object):
    @ListenTo(events.ON_X)
    def handler(self):
        pass
''',
    'pkg/nested.py': '''
def outer():
    from pkg import events

    def inner():
        from pkg import target as nested_target
        genv.messenger.Broadcast(events.ON_X)
        return nested_target.run()

    return inner()
''',
}


def _messiah_hooks():
    spec = importlib.util.spec_from_file_location(
        'test_local_imports_facts', os.path.join(PROJECT, 'custom', 'facts.py'))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.MessiahFacts()


def main():
    tmp = tempfile.mkdtemp(prefix='qcodemap_local_imports_')
    failed = []
    try:
        root = os.path.join(tmp, 'src')
        for rel, source in SOURCES.items():
            path = os.path.join(root, rel.replace('/', os.sep))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                f.write(source.lstrip())
        cfg = Config()
        cfg.root = root
        cfg.targets = ['pkg', 'other']
        cfg.db_path = os.path.join(tmp, 'index.db')
        cfg.hooks = _messiah_hooks()
        build_mod.build(cfg, verbose=False)

        store = Store(cfg.db_path)
        try:
            out = structure_mod.importers(store, 'pkg/target.py', json_out=True)
            if 'pkg/caller.py' not in out['importers']:
                failed.append('局部 import 未进入结构反向边: %s' % out)

            out = resolve_mod.callers(store, cfg, 'pkg/target.py', 'run')
            verified = {(i['file'], i['line']) for i in out['items']
                        if i['level'] == 'VERIFIED'}
            if ('pkg/caller.py', 3) not in verified:
                failed.append('first 的局部别名未解析到 pkg.target: %s' % out['items'])
            if ('pkg/caller.py', 8) in verified:
                failed.append('second 的同名局部别名串到 pkg.target')
            if ('pkg/nested.py', 7) not in verified:
                failed.append('嵌套函数局部 import 未按 inner 词法域解析: %s'
                              % out['items'])

            scopes = [row[0] for row in store.con.execute(
                'SELECT scope FROM imports WHERE file=? ORDER BY line',
                ('pkg/caller.py',)).fetchall()]
            if not {'first', 'second', 'publish'}.issubset(set(scopes)):
                failed.append('局部 import 作用域未落库: %s' % scopes)

            events = pubsub_mod.pubsub_refs(store, cfg, 'ON_X', json_out=True)
            if events['n_publish'] != 2 or events['n_listener'] != 1:
                failed.append('局部事件 import 未归一 join: %s' % events)
        finally:
            store.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failed:
        print('FAIL:')
        for item in failed:
            print('  -', item)
        return 1
    print('PASS（局部 import 结构边 + 词法隔离 + custom 事件归一）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
