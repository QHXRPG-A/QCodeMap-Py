# -*- coding: utf-8 -*-
"""P3-1 pubsub 事件配对回归：双端提取 + 事件归一 + pubsub-refs 配对 + blast 穿透。

自建临时小库（不碰主库），用 custom/facts.py 的真实 MessiahFacts 钩子
（连真实分发表一起测），覆盖客户端 ListenTo/Broadcast、服务端
Subscribe/Publish 两种 import 写法的归一 join、unresolved 降级、
排除项（FireEvent/变量首参/字面量首参/pubsub_stub 文件）、嵌套 def 去重。

用法：python tests/test_pubsub.py
"""

import os
import shutil
import sys
import tempfile

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

from qcodemap import blast as blast_mod              # noqa: E402
from qcodemap import build as build_mod              # noqa: E402
from qcodemap import pubsub_refs as pr_mod           # noqa: E402
from qcodemap.config import Config                   # noqa: E402
from qcodemap.store import Store                     # noqa: E402

C_SHOP = 'gclient/ui/shop.py'
C_LOOSE_LISTEN = 'gclient/ui/loose_listen.py'
C_LOOSE_PUB = 'gclient/ui/loose_pub.py'
S_LOGIC = 'gserver/entities/game_logic_x.py'
S_STUB = 'gserver/entities/pubsub_stub.py'

SHOP_SRC = '''# -*- coding: utf-8 -*-
from gclient.framework.util import events
from gclient.framework.util.events import ListenTo


class Shop(object):

    @ListenTo(events.ON_COIN_CHANGE)
    def on_coin(self):
        pass

    @events.ListenTo(events.ON_ADS_STATE)
    def on_ads(self):
        pass

    def refresh(self):
        genv.messenger.Broadcast(events.ON_COIN_CHANGE)

    def pay(self):
        genv.messenger.Broadcast(events.ON_PAY_CLOSED)

    def bad_variable(self, ev):
        genv.messenger.Broadcast(ev)

    def outer(self):
        @ListenTo(events.ON_NESTED)
        def inner_handler():
            pass
        return inner_handler

    def fire_anim(self):
        self.skeleton.FireEvent('@Run')
'''

LOOSE_LISTEN_SRC = '''# -*- coding: utf-8 -*-
class LooseListen(object):

    @ListenTo(ev.ON_WEIRD)
    def on_weird(self):
        pass
'''

LOOSE_PUB_SRC = '''# -*- coding: utf-8 -*-
class LoosePub(object):

    def emit(self):
        genv.messenger.Broadcast(ev.ON_WEIRD)
'''

LOGIC_SRC = '''# -*- coding: utf-8 -*-
from gserver import sconst
from gserver.sconst import CombatAvatarEvent


class GameLogicX(object):

    @sconst.pubsub.Subscribe(sconst.CombatAvatarEvent.DIE)
    def on_avatar_die(self):
        pass

    def kill(self, avatar):
        avatar.Publish(CombatAvatarEvent.DIE)

    def kill_all(self, avatars):
        for a in avatars:
            a.Publish(sconst.CombatAvatarEvent.DIE)
'''

STUB_SRC = '''# -*- coding: utf-8 -*-
from gserver import sconst


class StubProxy(object):

    def forward(self, proxy):
        proxy.Publish(sconst.AvatarEvent.ONLINE)
'''


def main():
    tmp = tempfile.mkdtemp(prefix='qcodemap_pubsub_')
    failed = []
    try:
        root = os.path.join(tmp, 'src')
        for d in ('gclient/ui', 'gserver/entities'):
            os.makedirs(os.path.join(root, d))
        for rel, src in ((C_SHOP, SHOP_SRC), (C_LOOSE_LISTEN, LOOSE_LISTEN_SRC),
                         (C_LOOSE_PUB, LOOSE_PUB_SRC), (S_LOGIC, LOGIC_SRC),
                         (S_STUB, STUB_SRC)):
            with open(os.path.join(root, rel.replace('/', os.sep)), 'w') as f:
                f.write(src)
        db = os.path.join(tmp, 'pubsub.db')
        cfg = Config()
        cfg.root = root
        cfg.targets = ['gclient', 'gserver']
        cfg.db_path = db
        # 用真实 Messiah 分发表（import custom/facts.py 的钩子实例）
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            't_custom_facts', os.path.join(PROJECT, 'custom', 'facts.py'))
        mod = importlib.util.module_from_spec(spec)
        sys.modules['t_custom_facts'] = mod
        spec.loader.exec_module(mod)
        cfg.hooks = mod.MessiahFacts()
        stats = build_mod.build(cfg, verbose=False)
        store = Store(db)
        try:
            n_pubsub = store.count('pubsub')
            # listen 5（客户端 2 + 服务端 1 + loose 1 + 嵌套内层 1）
            # publish 5（客户端 2 + 服务端 2 + loose 1）；
            # 变量首参 / FireEvent / pubsub_stub 文件排除均不产行
            if n_pubsub != 10:
                rows = store.con.execute('SELECT * FROM pubsub').fetchall()
                failed.append('pubsub 行应为 10，实际 %d: %s'
                              % (n_pubsub, rows))
            n_listen = store.con.execute(
                "SELECT COUNT(*) FROM pubsub WHERE side='listen'").fetchone()[0]
            n_publish = store.con.execute(
                "SELECT COUNT(*) FROM pubsub WHERE side='publish'").fetchone()[0]
            if (n_listen, n_publish) != (5, 5):
                failed.append('listen/publish 应为 5/5，实际 %d/%d'
                              % (n_listen, n_publish))

            # ---- 客户端配对：裸名后缀匹配 ----
            out = pr_mod.pubsub_refs(store, cfg, 'ON_COIN_CHANGE', json_out=True)
            if [g['event'] for g in out['groups']] \
                    != ['gclient.framework.util.events.ON_COIN_CHANGE']:
                failed.append('客户端事件键归一失败: %s'
                              % [g['event'] for g in out['groups']])
            pubs = [i for g in out['groups'] for i in g['items']
                    if i['level'] == 'EVENT-INFERRED']
            lis = [i for g in out['groups'] for i in g['items']
                   if i['level'] == 'LISTENER']
            if len(pubs) != 1 or pubs[0]['file'] != C_SHOP \
                    or pubs[0]['caller'] != 'Shop.refresh':
                failed.append('客户端发布点配对失败: %s' % pubs)
            if len(lis) != 1 or lis[0]['caller'] != 'Shop.on_coin':
                failed.append('客户端订阅 handler 配对失败: %s' % lis)

            # ---- 服务端配对：两种 import 写法归一到同一键 ----
            out = pr_mod.pubsub_refs(store, cfg, 'DIE', json_out=True)
            if [g['event'] for g in out['groups']] \
                    != ['gserver.sconst.CombatAvatarEvent.DIE']:
                failed.append('服务端事件键归一失败: %s'
                              % [g['event'] for g in out['groups']])
            g0 = out['groups'][0]
            pubs = [i for i in g0['items'] if i['level'] == 'EVENT-INFERRED']
            if len(pubs) != 2 or {p['caller'] for p in pubs} \
                    != {'GameLogicX.kill', 'GameLogicX.kill_all'}:
                failed.append('裸类名/sconst 前缀两种写法未归一 join: %s' % pubs)

            # ---- unresolved：两侧同用未导入根名，按原文 join ----
            out = pr_mod.pubsub_refs(store, cfg, 'ON_WEIRD', json_out=True)
            g0 = out['groups'][0] if out['groups'] else {'n_publish': 0,
                                                         'n_listener': 0}
            if g0['event'] != '?ev.ON_WEIRD' \
                    or g0['n_publish'] != 1 or g0['n_listener'] != 1:
                failed.append('unresolved join 失败: %s' % out['groups'])

            # ---- side 过滤 ----
            out = pr_mod.pubsub_refs(store, cfg, 'DIE', side='listen',
                                     json_out=True)
            if out['n_publish'] != 0 or out['n_listener'] != 1:
                failed.append('side 过滤失败: pub=%d lis=%d'
                              % (out['n_publish'], out['n_listener']))
        finally:
            store.close()

        # ---- blast 穿透：改 shop.py -> 发布方与订阅 handler 互入影响面 ----
        store = Store(db)
        try:
            out = blast_mod.blast(store, cfg, files=[C_SHOP], depth=1,
                                  json_out=True)
            via = [e for e in out['direct_callers'] if e.get('via_pubsub')]
            callers = {e['caller'] for e in via}
            if not via:
                failed.append('pubsub 入边应带 via_pubsub 标注: %s'
                              % out['direct_callers'])
            if 'Shop.on_coin' not in callers:
                failed.append('发布方 blast 未穿透到订阅 handler: %s' % via)
        finally:
            store.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failed:
        print('FAIL:')
        for f in failed:
            print('  -', f)
        return 1
    print('PASS（双端提取 + 事件归一 join + unresolved + 排除项 + blast 穿透）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
