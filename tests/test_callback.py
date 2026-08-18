# -*- coding: utf-8 -*-
"""通用约定回调回归；Messiah Property 仅由 custom 钩子识别。"""

import importlib.util
import os
import shutil
import sys
import tempfile

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

from qcodemap import blast as blast_mod                 # noqa: E402
from qcodemap import build as build_mod                 # noqa: E402
from qcodemap import resolve as resolve_mod             # noqa: E402
from qcodemap.config import Config                      # noqa: E402
from qcodemap.store import Store                        # noqa: E402


SOURCES = {
    'shared/prop.py': '''
from common.classutils import ComponentWithProperty, Property


class SharedState(ComponentWithProperty):
    Property('state', 0, Property.ALL_CLIENTS)
''',
    'client/callback.py': '''
class StateCallback(object):
    def _on_set_state(self, old):
        self.refresh()
''',
    'client/host.py': '''
from common.classutils import Components
from shared.prop import SharedState
from client.callback import StateCallback


@Components(SharedState, StateCallback)
class ClientHost(object):
    pass
''',
    'other/unrelated.py': '''
class Unrelated(object):
    def _on_set_state(self, old):
        pass
''',
    'shared/direct.py': '''
from common.classutils import ComponentWithProperty, Property


class Direct(ComponentWithProperty):
    Property('direct', 0)

    def _on_set_direct(self, old):
        pass
''',
    'shared/inherit.py': '''
from common.classutils import ComponentWithProperty, Property


class Base(ComponentWithProperty):
    Property('inherited', 0)


class Child(Base):
    def _on_set_inherited(self, old):
        pass
''',
}


def _messiah_hooks():
    spec = importlib.util.spec_from_file_location(
        'test_callback_facts', os.path.join(PROJECT, 'custom', 'facts.py'))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.MessiahFacts()


def _inferred(out):
    return [i for i in out['items'] if i['level'] == 'PROPERTY-INFERRED']


def main():
    tmp = tempfile.mkdtemp(prefix='qcodemap_callback_')
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
        cfg.targets = ['shared', 'client', 'other']
        cfg.db_path = os.path.join(tmp, 'index.db')
        cfg.hooks = _messiah_hooks()
        build_mod.build(cfg, verbose=False)

        store = Store(cfg.db_path)
        try:
            out = resolve_mod.callers(
                store, cfg, 'client/callback.py', '_on_set_state')
            inferred = _inferred(out)
            if len(inferred) != 1 or inferred[0]['file'] != 'shared/prop.py':
                failed.append('共享 @Components 宿主未严格连边: %s' % inferred)
            hosts = inferred[0].get('via_callback', {}).get('hosts', []) \
                if inferred else []
            if not any(h['class'] == 'ClientHost' for h in hosts):
                failed.append('约定边缺少共同宿主证据: %s' % hosts)

            out = resolve_mod.callers(
                store, cfg, 'other/unrelated.py', '_on_set_state')
            if _inferred(out):
                failed.append('无共同宿主的同名回调不应连边: %s' % _inferred(out))

            for file, func in (('shared/direct.py', '_on_set_direct'),
                               ('shared/inherit.py', '_on_set_inherited')):
                out = resolve_mod.callers(store, cfg, file, func)
                if len(_inferred(out)) != 1:
                    failed.append('%s 的同类/继承回调未连边: %s'
                                  % (func, out['items']))

            out = blast_mod.blast(store, cfg, files=['shared/prop.py'], depth=1,
                                  json_out=True, mode='full')
            hits = [i for i in out['direct_callers']
                    if i.get('via_callback', {}).get('kind') == 'PROPERTY'
                    and i['caller_file'] == 'client/callback.py']
            if len(hits) != 1 or hits[0]['layer'] != 1:
                failed.append('Property 声明未进入 blast 第一层: %s'
                              % out['direct_callers'])
        finally:
            store.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failed:
        print('FAIL:')
        for item in failed:
            print('  -', item)
        return 1
    print('PASS（通用约定回调 + Property custom 规则 + 严格宿主 + blast）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
