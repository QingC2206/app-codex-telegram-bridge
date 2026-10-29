"""R02 evidence gates: fixtures only, no production profile or services."""
import copy
import ast
import hashlib
import importlib.util
import json
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1] / 'tools'
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
import audit_parity as audit
from parity_evidence import canonical_ast, digest, mapping_payload, source_manifest, test_definitions


class EvidenceGateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for folder in ('src', 'tests', 'tools', 'docs'):
            (self.root / folder).mkdir()
        self.source = 'def example():\n    return 1\n'
        (self.root/'src/example.py').write_text(self.source)
        (self.root/'tools/example.py').write_text('VALUE = 1\n')
        (self.root/'tests/test_example.py').write_text(
            'import unittest\nclass Case(unittest.TestCase):\n'
            '    def test_specific(self):\n        self.assertEqual(1, 1)\n'
            '    def test_unrelated(self):\n        self.assertTrue(True)\n')
        (self.root/'docs/test-migration.json').write_text('{"scenarios":[]}')
        self.selector = 'test_example.Case.test_specific'
        self.ledger = {'schema_version':2, 'baseline_commit':'fixture', 'inventory_status':'reviewed',
            'test_migration':'docs/test-migration.json', 'verification_scope':{'windows_native':'deferred-by-user'},
            'capabilities':[{'id':'A01', 'name':'Example', 'status':'pending-evidence',
                'logic':[{'id':'A01.R01','text':'Specific behavior','review_state':'mapped',
                          'review_note':'Read the exact behavior assertion','scenarios':['S1','UI1']}],
                'baseline':[{'file':'src/example.py'}], 'implementation':['src/example.py'],
                'sharing':{'decision':'shared','reason':'OS independent'}}],
            'scenarios':{
                'S1':{'kind':'regression','rule_ids':['A01.R01'],'assertion':'Specific behavior assertion',
                      'reviewer':'fixture','tests':{self.selector:test_definitions(self.root)[self.selector]}},
                'UI1':{'kind':'manual','rule_ids':['A01.R01'],'assertion':'Observe actual Telegram UI',
                       'reviewer':'fixture','platform':'darwin','method':'telegram-ui'}},
            'native_evidence':{'current':[]}, 'historical_evidence':{'macos_ui':'passed'}}
        self.report = self.fresh()
        baseline = patch.object(audit,'baseline_sources',return_value={'src/example.py':self.source})
        baseline.start();self.addCleanup(baseline.stop)

    def fresh(self):
        (self.root/'docs/parity.json').write_text(json.dumps(self.ledger))
        manifest = source_manifest(self.root,self.ledger)
        mapping = digest(mapping_payload(self.ledger))
        self.ledger['final_review']={'id':'fixture-review','reviewer':'fixture','reviewed_at':'2026-10-28T00:00:00Z',
                                   'source_digest':digest(manifest),'mapping_sha256':mapping}
        return {'schema_version':2,'successful':True,'source_changed_during_run':False,
                'source_sha256':manifest,'source_digest':digest(manifest),'mapping_sha256':mapping,
                'records':[{'id':self.selector,'outcome':'passed'}]}

    def check(self, complete=False):
        return audit.audit(self.ledger,reviewed=True,complete=complete,root=self.root,report=self.report)

    def native(self, **changes):
        artifact={'schema_version':2,'source_digest':self.report['source_digest'],
                  'mapping_sha256':self.report['mapping_sha256'],'platform':'darwin',
                  'records':[{'scenario_id':'UI1','rule_ids':['A01.R01'],'outcome':'passed',
                              'method':'telegram-ui','observed_at':'2026-10-28T00:00:00Z',
                              'observations':['Quoted task message continued the same task.']}]}
        artifact.update(changes)
        path=self.root/'docs/parity-native-fixture.json';raw=json.dumps(artifact).encode();path.write_bytes(raw)
        self.ledger['native_evidence']['current']=[{'path':str(path),'sha256':hashlib.sha256(raw).hexdigest()}]
        return path

    def test_current_report_passes_reviewed_but_native_is_separate(self):
        result=self.check();self.assertEqual(result['errors'],[])
        self.assertEqual(result['offline_rules_passed_current_report'],1)
        self.assertEqual(result['native_pending_rule_ids'],['A01.R01'])
        self.assertTrue(self.check(complete=True)['errors'])
        self.native();self.assertEqual(self.check(complete=True)['errors'],[])

    def test_historical_passed_string_and_missing_native_files_cannot_complete(self):
        self.ledger['verification_scope']['macos_ui']='passed'
        self.report=self.fresh()
        self.assertTrue(any('current native' in e for e in self.check(True)['errors']))
        path=self.native();path.unlink()
        self.assertTrue(any('Invalid current native' in e for e in self.check(True)['errors']))

    def test_empty_fingerprint_is_rejected(self):
        self.report['source_sha256']={}
        self.assertTrue(any('cannot be empty' in e for e in self.check()['errors']))
        self.assertEqual(self.check()['offline_rules_passed_current_report'],0)

    def test_new_removed_and_changed_source_files_are_detected(self):
        for mode in ('new','removed','changed'):
            with self.subTest(mode=mode):
                path=self.root/'tools/example.py';original=path.read_text()
                if mode=='new':(self.root/'src/new.py').write_text('pass\n')
                elif mode=='removed':path.unlink()
                else:path.write_text('VALUE = 2\n')
                self.assertTrue(any('source file set differs' in e or 'Changed since regression' in e for e in self.check()['errors']))
                path.write_text(original);(self.root/'src/new.py').unlink(missing_ok=True)

    def test_new_rule_without_review_cannot_hide_in_reviewed_capability(self):
        self.ledger['capabilities'][0]['logic'].append({'id':'A01.R02','text':'New requirement',
            'review_state':'pending','review_note':'No matching evidence','scenarios':[]})
        self.report=self.fresh()
        self.assertTrue(any('A01.R02: rule evidence mapping remains pending' in e for e in self.check()['errors']))

    def test_repointing_to_unrelated_scenario_requires_reciprocal_review(self):
        self.ledger['scenarios']['UNRELATED']={'kind':'regression','rule_ids':['A02.R01'],
            'assertion':'Unrelated assertion','reviewer':'fixture','tests':{
                'test_example.Case.test_unrelated':test_definitions(self.root)['test_example.Case.test_unrelated']}}
        self.ledger['capabilities'][0]['logic'][0]['scenarios']=['UNRELATED']
        self.report=self.fresh()
        self.assertTrue(any('not explicitly mapped' in e for e in self.check()['errors']))

    def test_editing_mapping_or_test_after_review_invalidates_attestation(self):
        self.ledger['scenarios']['S1']['assertion']='A different claim'
        self.assertTrue(any('stale' in e for e in self.check()['errors']))
        self.report=self.fresh()
        path=self.root/'tests/test_example.py';path.write_text(path.read_text().replace('self.assertEqual(1, 1)','self.assertEqual(2, 2)'))
        self.assertTrue(any('test definition missing or changed' in e for e in self.check()['errors']))

    def test_skipped_or_failed_duplicate_record_is_never_passed(self):
        self.report['records'].append({'id':self.selector,'outcome':'failed'})
        self.assertTrue(any('test has not passed' in e for e in self.check()['errors']))
        self.report['records']=[{'id':self.selector,'outcome':'skipped'}]
        self.assertTrue(any('test has not passed' in e for e in self.check()['errors']))

    def test_native_content_and_source_version_are_verified(self):
        for changes in ({'source_digest':'old-version'},{'mapping_sha256':'old-rules'},
                        {'platform':'windows'},{'records':[]}):
            with self.subTest(changes=changes):
                self.native(**changes);self.assertTrue(self.check(True)['errors'])
        path=self.native();path.write_text('{}')
        self.assertTrue(any('file digest mismatch' in e for e in self.check(True)['errors']))

    def test_native_record_needs_observations_and_exact_rule_ids(self):
        self.native(records=[{'scenario_id':'UI1','rule_ids':['A01.R01'],'outcome':'passed'}])
        self.assertTrue(self.check(True)['errors'])
        self.native(records=[{'scenario_id':'UI1','rule_ids':['A02.R01'],'outcome':'passed',
            'method':'telegram-ui','observed_at':'2026-10-28T00:00:00Z','observations':['Observed']}])
        self.assertTrue(self.check(True)['errors'])

    def test_malformed_native_json_shapes_are_rejected_without_crashing(self):
        for records in ([None],['not-a-record'],{'not':'a-list'}):
            with self.subTest(records=records):
                self.native(records=records)
                self.assertTrue(any('Invalid current native' in e for e in self.check(True)['errors']))
        path=self.native();raw=b'[]';path.write_bytes(raw)
        self.ledger['native_evidence']['current'][0]['sha256']=hashlib.sha256(raw).hexdigest()
        self.assertTrue(any('Invalid current native' in e for e in self.check(True)['errors']))
        self.ledger['native_evidence']['current']=[None]
        self.assertTrue(any('Invalid current native' in e for e in self.check(True)['errors']))

    def test_report_and_native_results_do_not_create_hash_self_reference(self):
        before=source_manifest(self.root,self.ledger)
        (self.root/'docs/parity-tests.json').write_text('{"arbitrary":"output"}')
        self.native();self.ledger['final_review']['reviewed_at']='later'
        self.assertEqual(source_manifest(self.root,self.ledger),before)
        self.ledger['capabilities'][0]['logic'][0]['text']='Changed requirement'
        self.assertNotEqual(source_manifest(self.root,self.ledger),before)

    def test_specific_plugin_exception_cannot_expand_to_common_feature(self):
        rule=self.ledger['capabilities'][0]['logic'][0]
        rule.update(review_state='approved-exception',exception='specific-plugin-extension')
        self.report=self.fresh()
        self.assertTrue(any('exception not authorized' in e for e in self.check()['errors']))

    def test_installation_metadata_and_workflow_are_part_of_source_set(self):
        files={'install.py':'print(1)', 'VERSION':'1.0', 'MANIFEST.in':'include VERSION',
               'README.md':'setup', '.github/workflows/ci.yml':'name: CI'}
        for name,content in files.items():
            path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(content)
        manifest=source_manifest(self.root,self.ledger)
        self.assertTrue(set(files).issubset(manifest))
        self.assertTrue(any('source file set differs' in e for e in self.check()['errors']))

    def test_reviewed_test_fingerprint_covers_fixture_but_not_unrelated_new_test(self):
        path=self.root/'tests/test_example.py';original=path.read_text()
        expected=test_definitions(self.root)[self.selector]
        path.write_text(original+'    def test_new(self):\n        self.assertTrue(True)\n')
        self.assertEqual(test_definitions(self.root)[self.selector],expected)
        path.write_text(original+'    def setUp(self):\n        self.value=2\n')
        self.assertNotEqual(test_definitions(self.root)[self.selector],expected)

    def test_ast_hash_ignores_new_empty_python_fields_but_keeps_assertion_changes(self):
        tree=ast.parse('def example():\n    assert result == 1\n')
        original=digest(canonical_ast(tree))
        function=tree.body[0]
        function.type_params=[]
        with_empty=digest(canonical_ast(tree))
        del function.type_params
        self.assertEqual(digest(canonical_ast(tree)),with_empty)
        self.assertEqual(original,with_empty)
        changed=ast.parse('def example():\n    assert result == 2\n')
        self.assertNotEqual(digest(canonical_ast(changed)),original)

    def test_native_duplicate_results_or_failed_records_cannot_be_laundered(self):
        path=self.native();artifact=json.loads(path.read_text())
        records=artifact['records']*2
        self.native(records=records)
        self.assertTrue(any('duplicate scenario' in e for e in self.check(True)['errors']))
        records=[dict(records[0],outcome='failed')];self.native(records=records)
        self.assertTrue(any('incomplete scenario' in e for e in self.check(True)['errors']))

    def test_user_deferred_windows_native_does_not_satisfy_complete_gate(self):
        self.ledger['scenarios']['WIN1']={'kind':'manual','rule_ids':['A01.R01'],
            'assertion':'Actual Windows process behavior','reviewer':'fixture',
            'platform':'windows','method':'windows-desktop'}
        self.ledger['capabilities'][0]['logic'][0]['scenarios'].append('WIN1')
        self.report=self.fresh();self.native()
        self.assertEqual(self.check()['errors'],[])
        result=self.check(True)
        self.assertTrue(any('WIN1' in e for e in result['errors']))
        self.assertTrue(result['native_pending_scenarios'][0]['deferred_by_user'])

    def test_reporter_records_failed_subtests_expected_failures_and_skips(self):
        from test_report import Result
        class Cases(unittest.TestCase):
            def test_subcase(self):
                with self.subTest(case='bad'):self.fail('expected fixture failure')
            @unittest.expectedFailure
            def test_expected(self):self.fail('expected fixture failure')
            @unittest.skip('fixture skip')
            def test_skipped(self):pass
        result=unittest.TextTestRunner(stream=io.StringIO(),resultclass=Result).run(unittest.defaultTestLoader.loadTestsFromTestCase(Cases))
        outcomes={r['id'].rsplit('.',1)[-1]:r['outcome'] for r in result.records}
        self.assertEqual(outcomes,{'test_subcase':'failed','test_expected':'expected-failure','test_skipped':'skipped'})
        self.assertFalse(result.wasSuccessful())


if __name__ == '__main__':
    unittest.main()
