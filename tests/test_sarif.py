"""SARIF export retains finding provenance without claiming independent verification."""
from __future__ import annotations
import tempfile
from pathlib import Path
import unittest

from octane.openclaw_bridge import OctaneBridge
from octane.sarif import export_sarif


class SarifTests(unittest.TestCase):
    def test_export_round_trip_locations_attribution_and_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory); root=base/'project'; root.mkdir()
            bridge=OctaneBridge(str(base/'pil.sqlite3'),[str(root)],str(base/'workspaces'))
            def call(op, **params):
                return bridge.dispatch({'operation':op,'params':params})
            project=call('project_create',root=str(root),name='sarif')['id']
            evidence=call('evidence_add',projectId=project,evidenceType='structured_report',description='Imported scan',
                payload={'findings':[{'rule':'F401','message':'unused import','path':'src/my file.py','line':4},
                                     {'rule':'x','message':'No file','path':'/outside/file','level':'unexpected'}]})['id']
            report=call('evidence_sarif',projectId=project,evidenceIds=[evidence])
            self.assertEqual(report['version'],'2.1.0')
            findings=report['runs'][0]['results']
            self.assertEqual(findings[0]['locations'][0]['physicalLocation']['artifactLocation']['uri'],'src/my%20file.py')
            self.assertEqual(findings[0]['properties']['octaneEvidenceId'],evidence)
            self.assertNotIn('locations',findings[1])
            self.assertEqual(findings[1]['level'],'warning')
            limited=export_sarif(bridge.intelligence,project,limit=1)
            self.assertTrue(limited['runs'][0]['properties']['truncated'])
            for ids in (['missing'], 'not-list'):
                with self.assertRaises(ValueError):
                    export_sarif(bridge.intelligence,project,ids)
            with self.assertRaises(ValueError):
                export_sarif(bridge.intelligence,project,limit=0)
            with self.assertRaises(KeyError):
                export_sarif(bridge.intelligence,'missing')
