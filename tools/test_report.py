"""Isolated unittest reporting; never imports the user's live profile."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import unittest
from parity_evidence import digest, mapping_payload, source_manifest

ROOT = Path(__file__).resolve().parents[1]


class Result(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.records.append({'id': test.id(), 'outcome': 'passed'})

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.records.append({'id': test.id(), 'outcome': 'skipped', 'reason': reason})

    def addError(self, test, err):
        super().addError(test, err)
        self.records.append({'id': test.id(), 'outcome': 'error'})

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.records.append({'id': test.id(), 'outcome': 'failed'})

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self.records.append({'id': test.id(), 'outcome': 'failed', 'subtest': str(subtest)})

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self.records.append({'id': test.id(), 'outcome': 'expected-failure'})

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self.records.append({'id': test.id(), 'outcome': 'unexpected-success'})


if __name__ == '__main__':
    output = Path(sys.argv[1])
    ledger = json.loads((ROOT/'docs/parity.json').read_text())
    before = source_manifest(ROOT, ledger)
    loader = unittest.TestLoader()
    suite = (loader.loadTestsFromNames(sys.argv[2:]) if len(sys.argv) > 2
             else loader.discover(str(ROOT/'tests')))
    result = unittest.TextTestRunner(verbosity=2, resultclass=Result).run(suite)
    hashes = source_manifest(ROOT)
    report = {'schema_version': 2, 'created_utc': datetime.now(timezone.utc).isoformat(),
              'platform': sys.platform, 'python': sys.version.split()[0],
              'live_windows_desktop': False, 'live_telegram': False,
              'successful': result.wasSuccessful() and before == hashes, 'tests_run': result.testsRun,
              'source_changed_during_run': before != hashes,
              'mapping_sha256': digest(mapping_payload(ledger)),
              'source_digest': digest(hashes), 'records': result.records, 'source_sha256': hashes}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    raise SystemExit(not report['successful'])
