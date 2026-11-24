"""MCP server: protocol handshake, tool list, and claim/held/release through real separate server processes."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from test_agentlanes import Base  # noqa: E402


def rpc(*msgs):
    return ''.join(json.dumps(m) + '\n' for m in msgs)


def req(i, method, params=None):
    m = {'jsonrpc': '2.0', 'id': i, 'method': method}
    if params is not None:
        m['params'] = params
    return m


def tool(i, name, **args):
    return req(i, 'tools/call', {'name': name, 'arguments': args})


class MCP(Base):
    def serve(self, *msgs, agent='tester'):
        r = self.ak('mcp', env=self.env_for(AGENTLANES_AGENT=agent), inp=rpc(*msgs))
        self.assertEqual(r.returncode, 0, r.stderr)
        return {m['id']: m for m in map(json.loads, r.stdout.splitlines())}

    def text(self, rep):
        return rep['result']['content'][0]['text']

    def test_handshake_and_tools(self):
        out = self.serve(req(1, 'initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                                               'clientInfo': {'name': 't', 'version': '0'}}),
                         {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                         req(2, 'tools/list'), req(3, 'ping'), req(4, 'nope/method'))
        self.assertEqual(sorted(out), [1, 2, 3, 4])            # the notification gets no reply
        self.assertEqual(out[1]['result']['serverInfo']['name'], 'agentlanes')
        self.assertIn('tools', out[1]['result']['capabilities'])
        names = {t['name'] for t in out[2]['result']['tools']}
        self.assertTrue({'claim', 'release', 'who', 'status', 'jobs'} <= names)
        for t in out[2]['result']['tools']:
            self.assertEqual(t['inputSchema']['type'], 'object')
        self.assertEqual(out[3]['result'], {})
        self.assertEqual(out[4]['error']['code'], -32601)

    def test_claim_conflict_release_across_agents(self):
        f = os.path.join(self.tmp, 'a.py')
        a = self.serve(tool(1, 'claim', topic='t1', paths=[f], desc='refactor'), agent='alice')
        self.assertFalse(a[1]['result']['isError'], a)
        self.assertIn('claimed', self.text(a[1]))
        b = self.serve(tool(1, 'claim', topic='t1'), tool(2, 'who', paths=[f]), agent='bob')
        self.assertIn('HELD by alice', self.text(b[1]))
        self.assertFalse(b[1]['result']['isError'])           # held is an answer, not a tool failure
        self.assertIn('alice', self.text(b[2]))
        a = self.serve(tool(1, 'release', topic='t1'), tool(2, 'list_claims'), agent='alice')
        self.assertIn('released', self.text(a[1]))
        self.assertEqual(json.loads(self.text(a[2])), {})

    def test_jobs_and_bad_input(self):
        out = self.serve(tool(1, 'jobs', action='add', title='write docs'), tool(2, 'jobs', action='list'),
                         tool(3, 'claim'), req(4, 'tools/call', {'name': 'rm_rf'}))
        self.assertIn('write docs', self.text(out[2]))
        self.assertTrue(out[3]['result']['isError'])
        self.assertEqual(out[4]['error']['code'], -32602)

    def test_parse_error_keeps_serving(self):
        r = self.ak('mcp', inp='not json\n' + rpc(req(1, 'ping')))
        lines = [json.loads(x) for x in r.stdout.splitlines()]
        self.assertEqual(lines[0]['error']['code'], -32700)
        self.assertEqual(lines[1]['id'], 1)


if __name__ == '__main__':
    unittest.main()
