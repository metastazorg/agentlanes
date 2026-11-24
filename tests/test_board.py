"""board: the page and the JSON state endpoint, served on localhost."""
import json
import os
import socket
import sys
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(__file__))
from test_agentlanes import CLI, Base  # noqa: E402


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Board(Base):
    def test_page_and_state(self):
        self.ak('claim', 'ui', '--desc', 'redesign')
        self.ak('jobs', 'add', 'write docs')
        port = free_port()
        self.spawn(CLI + ['board', '--port', str(port)])
        url = 'http://127.0.0.1:%d/' % port
        for _ in range(50):
            try:
                page = urllib.request.urlopen(url, timeout=2).read().decode()
                break
            except OSError:
                time.sleep(0.1)
        else:
            self.fail('board did not start')
        self.assertIn('agentlanes board', page)
        st = json.loads(urllib.request.urlopen(url + 'api/state', timeout=5).read())
        self.assertIn('ui', st['claims'])
        self.assertEqual(st['jobs'][0]['title'], 'write docs')
        self.assertTrue(any(e['kind'] == 'claim.add' for e in st['events']))
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(url + '../etc/passwd', timeout=5)
        self.assertEqual(cm.exception.code, 404)


if __name__ == '__main__':
    unittest.main()
