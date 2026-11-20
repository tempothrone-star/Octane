"""Exercise LSP framing, callbacks, budgets, and semantic paths without real servers."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from octane.language_server import LanguageServer, _node, semantic_calls

PEER=Path(__file__).parent/'fixtures/fake_language_server.py'


class LanguageServerProtocolTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        (self.root/'main.py').write_text('def entry():\n    return helper()\n')
        (self.root/'helper.py').write_text('def helper(): return 1\n')

    def peer(self,mode='normal',timeout=5):
        server=LanguageServer([sys.executable,str(PEER.resolve()),mode],self.root,timeout=timeout)
        self.addCleanup(server.close)
        return server

    def test_protocol_roundtrip_callbacks_environment_and_stderr_tail(self):
        with patch.dict(os.environ,{'SECRET_TOKEN':'must-not-reach-peer'}):
            server=self.peer()
            self.assertIn('capabilities',server.request('initialize',{}))
            result=server.request('probe',{})
        self.assertTrue(result['ok'])
        self.assertNotIn('SECRET_TOKEN',result['environment'])
        # Synchronize with the peer's stderr writer before checking its bounded tail.
        server.stderr_reader.join(timeout=0.02)
        self.assertLessEqual(len(server.stderr_tail),8192)
        self.assertNotIn('synthetic-secret',server.diagnostics)
        server.send('initialized',{})
        self.assertIsNone(server.request('unknown',{}))

    def test_failure_modes_report_errors_and_close_the_process(self):
        for mode,error in [('exit',ValueError),('bad-header',ValueError),('oversize',ValueError),
                           ('short-body',ValueError),('error',RuntimeError)]:
            with self.subTest(mode=mode):
                server=self.peer(mode)
                with self.assertRaises(error):
                    server.request('initialize',{})
                server.close()
                self.assertIsNotNone(server.process.returncode)
                self.assertTrue(server.process.stdout.closed)

    def test_response_wait_and_expired_deadline_are_bounded(self):
        server=self.peer('stall',timeout=0.15)
        with self.assertRaisesRegex(TimeoutError,'analysis budget'):
            server.request('initialize',{})
        with self.assertRaises(TimeoutError):
            server.request('initialize',{})

    def test_semantic_edges_are_produced_without_an_external_provider(self):
        files=[{'path':'main.py','language':'python'}]
        contents={'main.py':(self.root/'main.py').read_bytes()}
        known=[{'node_key':'symbol:main.py:entry','path':'main.py','line':1,'name':'entry','language':'python'}]
        servers={'python':{'command':[sys.executable,str(PEER.resolve())],'initializationOptions':{},'environmentAllowlist':[]}}
        nodes,edges,coverage=semantic_calls(self.root,files,contents,'revision',known,servers,limit=1)
        self.assertTrue(edges)
        self.assertTrue(all(edge['metadata']['basis']=='LSP call hierarchy' for edge in edges))
        self.assertEqual(edges[0]['source'],'symbol:main.py:entry')
        self.assertEqual(coverage['python']['calls'],1)
        self.assertFalse(any(node['name']=='foreign' for node in nodes))
        no_files=semantic_calls(self.root,[],{},'revision',[],servers)
        self.assertEqual(no_files,([],[],{}))

    def test_node_mapping_rejects_external_paths_and_distinguishes_ambiguous_symbols(self):
        region={'start':{'line':0,'character':0},'end':{'line':0,'character':5}}
        value={'uri':(self.root/'main.py').as_uri(),'name':'entry','range':region}
        self.assertIsNone(_node({**value,'uri':'https://example.invalid'},'python',self.root,'r',[]))
        self.assertIsNone(_node({**value,'uri':self.root.parent.as_uri()},'python',self.root,'r',[]))
        self.assertTrue(_node(value,'python',self.root,'r',[])['node_key'].startswith('lsp:'))
