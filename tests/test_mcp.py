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

    def test_long_default_ttl_and_renew(self):
        import time
        out = self.serve(tool(1, 'claim', topic='long', paths=['x.py']), tool(2, 'list_claims'),
                         tool(3, 'renew', topic='long', ttl_minutes=600), tool(4, 'list_claims'),
                         tool(5, 'renew', topic='nobody-holds-this'))
        c1, c2 = json.loads(self.text(out[2]))['long'], json.loads(self.text(out[4]))['long']
        self.assertGreater(c1['until'] - time.time(), 470 * 60)          # a 2 h session keeps its claim
        self.assertGreater(c2['until'] - time.time(), 590 * 60)
        self.assertEqual(c2['paths'], c1['paths'])                        # renew keeps the paths
        self.assertIn('not held by you', self.text(out[5]))

    def test_path_with_comma_stays_one_path(self):
        d = os.path.join(self.tmp, 'a,b')
        os.makedirs(d)
        out = self.serve(tool(1, 'claim', topic='t', paths=[d]), tool(2, 'list_claims'))
        self.assertEqual(json.loads(self.text(out[2]))['t']['paths'], [d])

    def test_malformed_requests_never_crash(self):
        # [11-25] a list as arguments used to raise TypeError and kill the server (and with it the agent's tools)
        import random
        rnd = random.Random(1125)
        junk = [None, 0, 1.5, True, 'x', '', [], ['a'], [1, 2], {}, {'a': 1}, 'a' * 5000, 'a\x00b']
        names = ['claim', 'release', 'who', 'jobs', 'lane', 'events', 'blame', 'status', 'nope', None, 5]
        msgs = []
        for i in range(1, 121):
            args = {k: rnd.choice(junk) for k in rnd.sample(['topic', 'paths', 'action', 'n', 'ttl_minutes', 'share',
                                                              'name', 'path', 'zzz'], rnd.randint(0, 3))}
            params = rnd.choice([{'name': rnd.choice(names), 'arguments': rnd.choice([args, args, ['x'], 'bad', None])},
                                 'bad', [1], None, {'arguments': args}])
            msgs.append(req(i, 'tools/call', params))
        msgs.append(req(999, 'ping'))
        out = self.serve(*msgs)
        self.assertEqual(sorted(out), list(range(1, 121)) + [999])   # every request answered, server alive at the end
        for i in range(1, 121):
            self.assertTrue('error' in out[i] or 'content' in out[i]['result'], out[i])

if __name__ == '__main__':
    unittest.main()
