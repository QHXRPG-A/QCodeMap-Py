# -*- coding: utf-8 -*-
"""P3-2 RPC 双端跳转回归：分发表提取 + rpc-refs 配对 + blast 穿透。

自建临时小库（不碰主库），用 custom/facts.py 的真实 MessiahFacts 钩子
（连真实分发表一起测），覆盖五通道提取、变量名跳过、stub 归一、配对分级。

用法：python tests/test_rpc.py
"""

import os
import shutil
import sys
import tempfile

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

from qcodemap import blast as blast_mod              # noqa: E402
from qcodemap import build as build_mod              # noqa: E402
from qcodemap import rpc_refs as rr_mod              # noqa: E402
from qcodemap.config import Config                   # noqa: E402
from qcodemap.hooks import FactsHooks                # noqa: E402
from qcodemap.store import Store                     # noqa: E402

C_FSM = 'gclient/framework/camera/fsm.py'
C_AVATAR = 'gclient/framework/entities/avatar.py'
S_AVATAR = 'gserver/entities/avatar.py'
S_STUB = 'gserver/entities/clan_stub.py'
S_CALLER = 'gserver/entities/clan_caller.py'

CLIENT_SRC = '''# -*- coding: utf-8 -*-
class Fsm(object):
    def update_aim(self, is_aim):
        genv.player.CallServer('SetPlayerAimState', is_aim)

    def query(self):
        genv.avatar.CallServer('QueryRandom', 'OnQueryBack')

    def packed(self, speed):
        self.CallServerPacked('OnSpeedLevelChange', speed)

    def bad_variable_name(self, name):
        genv.player.CallServer(name)
'''

SERVER_SRC = '''# -*- coding: utf-8 -*-
class Avatar(object):
    def SetPlayerAimState(self, is_aim):
        self.aim = is_aim

    def push(self, client):
        client.CallClient('ServerShowMessage', 'hello')

    def OnSpeedLevelChange(self, speed):
        self.speed = speed
'''

STUB_SRC = '''# -*- coding: utf-8 -*-
class ClanStub(object):
    def ObtainClan(self, caller, callback, guid):
        return guid
'''

CALLER_SRC = '''# -*- coding: utf-8 -*-
from gserver.entities.stub_extention import StubHelper


def migrate():
    StubHelper().CallShardStubHostnum(1, 'key', 'ClanStub@2', 'ObtainClan',
                                      None, 'cb', 7)


class Agent(object):
    def ping(self):
        StubHelper().CallOneShardStub('ClanStub', 'ObtainClan', None, 'cb', 8)
'''


def main():
    tmp = tempfile.mkdtemp(prefix='qcodemap_rpc_')
    failed = []
    try:
        root = os.path.join(tmp, 'src')
        for d in ('gclient/framework/camera', 'gclient/framework/entities',
                  'gserver/entities'):
            os.makedirs(os.path.join(root, d))
        for rel, src in ((C_FSM, CLIENT_SRC), (C_AVATAR, ''),
                         (S_AVATAR, SERVER_SRC), (S_STUB, STUB_SRC),
                         (S_CALLER, CALLER_SRC)):
            with open(os.path.join(root, rel.replace('/', os.sep)), 'w') as f:
                f.write(src)
        db = os.path.join(tmp, 'rpc.db')
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
            n_rpc = store.count('rpc')
            # 7 处分发调用：6 个常量方法名入表 + 1 个变量名（CallServer(name)）跳过
            if n_rpc != 6:
                rows = store.con.execute(
                    'SELECT * FROM rpc').fetchall()
                failed.append('rpc 行应为 6（变量名跳过），实际 %d: %s'
                              % (n_rpc, rows))

            # ---- rpc_refs：C2S 配对 ----
            out = rr_mod.rpc_refs(store, cfg, 'SetPlayerAimState', json_out=True)
            rpcs = [i for i in out['items'] if i['level'] == 'RPC-INFERRED']
            hs = [i for i in out['items'] if i['level'] == 'HANDLER']
            if len(rpcs) != 1 or rpcs[0]['file'] != C_FSM or rpcs[0]['chan'] != 'C2S':
                failed.append('C2S 调用点配对失败: %s' % rpcs)
            if not any(i['file'] == S_AVATAR for i in hs):
                failed.append('handler 定义未命中: %s' % hs)

            # ---- CallServerPacked：真实签名首参为方法名 ----
            out = rr_mod.rpc_refs(store, cfg, 'OnSpeedLevelChange', json_out=True)
            packed = [i for i in out['items'] if i['level'] == 'RPC-INFERRED']
            if len(packed) != 1 or packed[0]['chan'] != 'C2S':
                failed.append('CallServerPacked C2S 提取失败: %s' % packed)

            # ---- S2C ----
            out = rr_mod.rpc_refs(store, cfg, 'ServerShowMessage', json_out=True)
            rpcs = [i for i in out['items'] if i['level'] == 'RPC-INFERRED']
            if len(rpcs) != 1 or rpcs[0]['chan'] != 'S2C':
                failed.append('S2C 配对失败: %s' % rpcs)

            # ---- STUB + stub 归一（'ClanStub@2' -> ClanStub）----
            out = rr_mod.rpc_refs(store, cfg, 'ObtainClan', stub='ClanStub',
                                  json_out=True)
            rpcs = [i for i in out['items'] if i['level'] == 'RPC-INFERRED']
            if len(rpcs) != 2:
                failed.append('ObtainClan 调用点应 2 处，实际 %d' % len(rpcs))
            if any(i.get('stub') != 'ClanStub' for i in rpcs):
                failed.append('stub 归一失败: %s' % [i.get('stub') for i in rpcs])
            hs = [i for i in out['items'] if i['level'] == 'HANDLER'
                  and i.get('match') == 'stub']
            if len(hs) != 1 or hs[0]['file'] != S_STUB:
                failed.append('stub handler 精确配对失败: %s' % hs)
        finally:
            store.close()

        # ---- blast 穿透：改 handler（ObtainClan）-> 调用方进影响面 ----
        store = Store(db)
        try:
            out = blast_mod.blast(store, cfg, files=[S_STUB], depth=1,
                                  json_out=True)
            files_hit = {e['caller_file'] for e in out['direct_callers']}
            if S_CALLER not in files_hit:
                failed.append('blast 未穿透 RPC 边界，direct_callers: %s'
                              % out['direct_callers'])
            via_rpc = [e for e in out['direct_callers'] if e.get('via_rpc')]
            if not via_rpc:
                failed.append('RPC 入边应带 via_rpc 标注: %s'
                              % out['direct_callers'])
        finally:
            store.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if failed:
        print('FAIL:')
        for f in failed:
            print('  -', f)
        return 1
    print('PASS（五通道提取 + rpc-refs 配对 + stub 归一 + blast 穿透）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
