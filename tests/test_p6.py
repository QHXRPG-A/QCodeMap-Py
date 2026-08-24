# -*- coding: utf-8 -*-
"""P6: 只读 Store、声明关系、分页和 core/custom 边界回归。"""

import ast
import os
import shutil
import sqlite3
import sys
import tempfile

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

from qcodemap import build, freshness, ui_refs  # noqa: E402
from qcodemap.config import Config  # noqa: E402
from qcodemap.hooks import FactsHooks  # noqa: E402
from qcodemap.store import Store  # noqa: E402


SOURCE = '''DECLARATIONS = {
    "panel": {"resource": "shared_panel.csb", "wrapper": "PanelWrapper"},
}

def Render():
    Bind("node")
    Bind("node")
    Bind("node")
    Bind("node")
    Bind("node")
'''


class FixtureHooks(FactsHooks):

    def module_bindings(self, tree, ctx):
        rows = []
        for stmt in tree.body:
            if not isinstance(stmt, ast.Assign) or not isinstance(stmt.value, ast.Dict):
                continue
            for owner_node, fields_node in zip(stmt.value.keys, stmt.value.values):
                if not isinstance(owner_node, ast.Constant) \
                        or not isinstance(fields_node, ast.Dict):
                    continue
                owner = 'record:%s' % owner_node.value
                fields = {k.value: v for k, v in zip(fields_node.keys, fields_node.values)
                          if isinstance(k, ast.Constant)}
                for relation in ('resource', 'wrapper'):
                    value = fields.get(relation)
                    if isinstance(value, ast.Constant):
                        rows.append((value.lineno, 'fixture-ui', owner, relation,
                                     value.value[:-4] if relation == 'resource'
                                     and value.value.endswith('.csb') else value.value,
                                     'default', 'declared',
                                     'synthetic declaration'))
        return rows

    def ui_facts(self, call, ctx):
        if isinstance(call.func, ast.Name) and call.func.id == 'Bind' \
                and call.args and isinstance(call.args[0], ast.Constant):
            return [('NODE', call.args[0].value, None)]
        return []


class FixtureProfile(object):
    seek_attrs = frozenset()
    anchor_receivers = frozenset()
    class_bind_kinds = ('LOAD',)
    getter_call_kind = ''
    getter_kind = ''
    node_kinds = ('NODE',)
    pattern_kinds = frozenset()
    dynamic_kinds = ()
    load_kinds = ('LOAD',)
    anim_load_kind = ''
    anim_play_kind = ''
    item_kind = ''
    wrapper_bind_kind = ''
    base_stops = frozenset()
    wrapper_node_types = ()
    declaration_domain = 'fixture-ui'
    declaration_resource_relation = 'resource'
    declaration_wrapper_relation = 'wrapper'
    declaration_level = 'DECLARED'
    kind_labels = {}
    ui_tool_description = ''

    @staticmethod
    def norm_res_key(name):
        return name[:-4] if name.endswith('.csb') else name

    @staticmethod
    def looks_like_key(name):
        return name.endswith('.csb')

    @staticmethod
    def split_key_segments(key):
        return tuple(key.split('.'))

    @staticmethod
    def open_resource(cfg):
        return None


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as file_obj:
        file_obj.write(text)


def main():
    failed = []
    tmp = tempfile.mkdtemp(prefix='qcodemap_p6_')
    try:
        root = os.path.join(tmp, 'project')
        _write(os.path.join(root, 'src', 'view.py'), SOURCE)
        cfg = Config()
        cfg.root = root
        cfg.targets = ['src']
        cfg.index_profile_rules = [('src/*.py', 'semantic-only')]
        cfg.db_path = os.path.join(tmp, 'index.db')
        cfg.hooks = FixtureHooks()
        cfg.ui_profile = FixtureProfile()
        build.build(cfg, rebuild=True, verbose=False)

        reader = Store.open_reader(cfg.db_path)
        try:
            rows = reader.con.execute(
                'SELECT owner,relation,target,variant FROM binding '
                'ORDER BY relation').fetchall()
            if rows != [('record:panel', 'resource', 'shared_panel', 'default'),
                        ('record:panel', 'wrapper', 'PanelWrapper', 'default')]:
                failed.append('module_bindings 未写入标准关系: %s' % (rows,))

            declared = ui_refs.ui_refs(
                reader, cfg, name='shared_panel.csb', kind='file')
            declared_items = [item for item in declared['items']
                              if item['level'] == 'DECLARED']
            if len(declared_items) != 1 \
                    or declared_items[0]['caller'] != 'PanelWrapper':
                failed.append('声明资源未配对 wrapper: %s' % declared)

            page = ui_refs.ui_refs(
                reader, cfg, name='node', limit=2, offset=2)
            if page['total'] != 5 or page['n_items'] != 2 \
                    or page['next_offset'] != 4 or not page['truncated']:
                failed.append('ui-refs 分页契约错误: %s' % page)

            try:
                reader.con.execute("INSERT INTO meta VALUES('forbidden','1')")
                failed.append('只读 Store 允许写入')
            except sqlite3.OperationalError:
                pass
            try:
                reader.commit()
                failed.append('只读 Store 允许 commit')
            except RuntimeError:
                pass
        finally:
            reader.close()

        writer = Store.open_writer(cfg.db_path)
        writer.set_meta('snapshot_probe', 'old')
        writer.commit()
        reader = Store.open_reader(cfg.db_path)
        try:
            writer.con.execute('BEGIN IMMEDIATE')
            writer.set_meta('snapshot_probe', 'new')
            if reader.get_meta('snapshot_probe') != 'old':
                failed.append('reader 读到了 writer 未提交内容')
            writer.commit()
            if reader.get_meta('snapshot_probe') != 'new':
                failed.append('reader 未看到 writer 提交后的 WAL 快照')
        finally:
            reader.close()
            writer.close()

        with build.build_lock(cfg.db_path, timeout=0.5):
            try:
                with build.build_lock(cfg.db_path, timeout=0.1):
                    failed.append('build lock 允许双 writer')
            except RuntimeError:
                pass

        meta = freshness.ensure_fresh(cfg, mode='off')
        if meta.get('scope', {}).get('status') != 'complete' \
                or meta.get('coverage') != 'complete':
            failed.append('index scope/兼容 coverage 错误: %s' % meta)

        banned = ('gclient', 'gserver', 'genv', 'map_tag_data', 'Pinata')
        for dirpath, _dirs, files in os.walk(os.path.join(PROJECT, 'qcodemap')):
            for filename in files:
                if not filename.endswith('.py'):
                    continue
                path = os.path.join(dirpath, filename)
                with open(path, encoding='utf-8') as file_obj:
                    text = file_obj.read()
                hits = [word for word in banned if word in text]
                if hits:
                    failed.append('core 泄漏项目词汇 %s: %s' % (hits, path))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failed:
        print('FAIL:')
        for item in failed:
            print('  -', item)
        return 1
    print('PASS（P6 只读/声明关系/分页/边界）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
