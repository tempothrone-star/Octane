"""SARIF export retains finding provenance without claiming independent verification."""
from __future__ import annotations
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

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
            with patch.object(bridge.intelligence, 'list_evidence_ids', wraps=bridge.intelligence.list_evidence_ids) as listing:
                limited=export_sarif(bridge.intelligence,project,limit=1)
            listing.assert_called_once_with(project,'structured_report',1)
            self.assertTrue(limited['runs'][0]['properties']['truncated'])
            for ids in (['missing'], 'not-list'):
                with self.assertRaises(ValueError):
                    export_sarif(bridge.intelligence,project,ids)
            with self.assertRaises(ValueError):
                export_sarif(bridge.intelligence,project,limit=0)
            with self.assertRaises(KeyError):
                export_sarif(bridge.intelligence,'missing')

    def test_public_evidence_listing_is_bounded_filtered_and_project_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            from octane.db import ProjectIntelligence
            pil=ProjectIntelligence(Path(directory)/'pil.sqlite3')
            first=pil.create_project('first',directory)['id']
            second=pil.create_project('second',directory)['id']
            ids=[pil.submit_evidence(first, 'structured_report', {}, 'First')['id'],
                 pil.submit_evidence(first, 'other', {}, 'Other')['id'],
                 pil.submit_evidence(second, 'structured_report', {}, 'Foreign')['id']]
            self.assertEqual(pil.list_evidence_ids(first, 'structured_report'),[ids[0]])
            self.assertEqual(pil.list_evidence_ids(first,limit=1),[ids[0]])
            for invalid in (0,1001,True):
                with self.assertRaises(ValueError):
                    pil.list_evidence_ids(first,limit=invalid)
            with self.assertRaises(ValueError):
                pil.list_evidence_ids(first,evidence_type='')
            with self.assertRaises(KeyError):
                pil.list_evidence_ids('missing')
            denied=pil.register_agent(first,'denied','reader',[],[])['id']
            with self.assertRaises(PermissionError):
                pil.list_evidence_ids(first,requesting_agent_id=denied)
            reader=pil.register_agent(first,'reader','reader',[],['read_evidence'])['id']
            self.assertEqual(pil.list_evidence_ids(first,'structured_report',requesting_agent_id=reader),[ids[0]])
