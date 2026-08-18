# -*- coding: utf-8 -*-
"""MCP server 端到端回归：subprocess 起 server，走完 initialize -> tools/list
-> tools/call(callers/hubs/blast) 全流程。

用法：python tests/test_mcp.py
"""

import io
import json
import os
import subprocess
import sys

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)


class _FakeStdin(object):
    def __init__(self, lines):
        self._lines = list(lines)

    def __iter__(self):
        return iter(self._lines)


def _rpc(msgs):
    """进程内驱动 serve()（不起子进程，测协议逻辑）。"""
    from qcodemap import mcp_server
    out = io.StringIO()
    mcp_server.serve(stdin=_FakeStdin([json.dumps(m) for m in msgs]), stdout=out)
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


def main():
    msgs = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
         'params': {'protocolVersion': '2024-11-05'}},
        {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
        {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
         'params': {'name': 'qcodemap_callers',
                    'arguments': {'file': 'gclient/gameplay/logic_base/entities/'
                                          'combatavatarmembers/cimp_combat_unit.py',
                                  'func': 'GetTeammateInfo'}}},
        {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call',
         'params': {'name': 'qcodemap_hubs', 'arguments': {'top': 5}}},
        {'jsonrpc': '2.0', 'id': 5, 'method': 'ping'},
        {'jsonrpc': '2.0', 'id': 6, 'method': 'tools/call',
         'params': {'name': 'qcodemap_no_such_tool', 'arguments': {}}},
    ]
    resps = _rpc(msgs)
    by_id = {r.get('id'): r for r in resps}

    failed = []
    init = by_id.get(1, {}).get('result', {})
    if init.get('serverInfo', {}).get('name') != 'qcodemap':
        failed.append('initialize 应答异常: %s' % init)
    if init.get('protocolVersion') != '2024-11-05':
        failed.append('protocolVersion 未回显')
    tools = by_id.get(2, {}).get('result', {}).get('tools', [])
    if len(tools) != 14:
        failed.append('tools/list 应为 14 个工具，实际 %d' % len(tools))
    tool_names = {t['name'] for t in tools}
    for expect in ('qcodemap_find_file', 'qcodemap_get_file_context',
                   'qcodemap_context', 'qcodemap_rpc_refs',
                   'qcodemap_pubsub_refs'):
        if expect not in tool_names:
            failed.append('缺少工具 %s' % expect)
    callers = json.loads(by_id[3]['result']['content'][0]['text'])
    if callers['n_verified'] < 5:
        failed.append('callers VERIFIED=%d 过少' % callers['n_verified'])
    hubs = json.loads(by_id[4]['result']['content'][0]['text'])
    if not hubs.get('hubs'):
        failed.append('hubs 空结果')
    if by_id.get(5, {}).get('result') != {}:
        failed.append('ping 应答异常')
    if by_id.get(6, {}).get('error', {}).get('code') != -32602:
        failed.append('未知工具应回 -32602')

    # 真子进程冒烟：python -m qcodemap mcp 应起来并应答 initialize
    p = subprocess.Popen([sys.executable, '-m', 'qcodemap', 'mcp'],
                         cwd=PROJECT, stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         text=True, encoding='utf-8')
    try:
        p.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                                  'params': {}}) + '\n')
        p.stdin.flush()
        line = p.stdout.readline()
        sub_ok = json.loads(line).get('result', {}).get('serverInfo', {}).get(
            'name') == 'qcodemap'
    finally:
        p.kill()
    if not sub_ok:
        failed.append('子进程模式 initialize 失败: %.80s' % line)

    if failed:
        print('FAIL:')
        for f in failed:
            print('  -', f)
        return 1
    print('PASS（进程内 %d 响应 + 子进程冒烟）' % len(resps))
    return 0


if __name__ == '__main__':
    sys.exit(main())
