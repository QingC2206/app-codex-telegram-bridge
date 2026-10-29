#!/usr/bin/env python3
"""Check the Windows inventory and render the single-source parity ledger.

Coverage means inventoried source, not verified behavior. Completion additionally
requires concrete rule evidence and a fresh final source and mapping review.
"""
import argparse
import ast
from collections import Counter
import hashlib
import html
import json
from pathlib import Path
import subprocess
from datetime import datetime, timezone
from parity_evidence import digest, mapping_payload, source_manifest, test_definitions

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / 'docs/parity.json'


def baseline_sources(revision):
    names = subprocess.check_output(
        ['git', 'ls-tree', '-r', '--name-only', revision, 'src'], cwd=ROOT, text=True)
    return {p: subprocess.check_output(['git', 'show', revision + ':' + p], cwd=ROOT).decode()
            for p in names.splitlines() if p.endswith('.py')}


def symbols(source):
    result = []
    def walk(node, parent=''):
        for child in ast.iter_child_nodes(node):
            name = parent
            if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                name = parent + '.' + child.name if parent else child.name
                if not isinstance(child, ast.ClassDef):
                    result.append((name, child.lineno, child.end_lineno))
            walk(child, name)
    walk(ast.parse(source))
    return result


def implementation_hash(paths):
    digest = hashlib.sha256()
    for name in sorted(paths):
        path = ROOT / name
        digest.update(name.encode() + b'\0')
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _current_native(ledger, root, source_hash, mapping_hash, errors):
    """Historical envelopes are never promoted to current acceptance."""
    records = {}
    envelope = ledger.get('native_evidence', {})
    references = envelope.get('current', []) if isinstance(envelope, dict) else None
    if not isinstance(references, list):
        errors.append('Invalid current native evidence reference list')
        return records
    for reference in references:
        label = reference.get('path', '?') if isinstance(reference, dict) else '?'
        try:
            if not isinstance(reference, dict) or not isinstance(reference.get('path'), str):
                raise ValueError('invalid evidence reference')
            path = Path(reference['path'])
            if not path.is_absolute():
                path = root / path
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != reference.get('sha256'):
                raise ValueError('file digest mismatch')
            artifact = json.loads(raw)
            if (not isinstance(artifact, dict) or artifact.get('schema_version') != 2 or artifact.get('source_digest') != source_hash
                    or artifact.get('mapping_sha256') != mapping_hash):
                raise ValueError('evidence version does not match current source/rules')
            if not isinstance(artifact.get('records'), list) or not artifact['records']:
                raise ValueError('empty evidence records')
            for record in artifact['records']:
                if not isinstance(record, dict):
                    raise ValueError('scenario record must be an object')
                scenario = record.get('scenario_id')
                if not isinstance(scenario, str) or not scenario or scenario in records:
                    raise ValueError('missing or duplicate scenario ID')
                if (record.get('outcome') != 'passed' or not record.get('observations')
                        or not isinstance(record['observations'], list)
                        or not all(isinstance(o, str) and o.strip() for o in record['observations'])
                        or not record.get('method') or not record.get('observed_at')
                        or not isinstance(record.get('rule_ids'), list) or not record['rule_ids']
                        or not all(isinstance(rid, str) and rid for rid in record['rule_ids'])
                        or len(set(record['rule_ids'])) != len(record['rule_ids'])):
                    raise ValueError('incomplete scenario result')
                datetime.fromisoformat(record['observed_at'].replace('Z', '+00:00'))
                records[scenario] = {**record, 'platform': artifact.get('platform')}
        except (KeyError, OSError, ValueError, TypeError) as exc:
            errors.append('Invalid current native evidence: ' + str(label) + ': ' + str(exc))
    return records


def audit(ledger, *, complete=False, reviewed=False, root=None, report=None):
    root = Path(root or ROOT)
    strict = complete or reviewed
    sources = baseline_sources(ledger['baseline_commit'])
    covered = set()
    errors = []
    rows = ledger['capabilities']
    report_path = root / 'docs/parity-tests.json'
    if report is None:
        report = json.loads(report_path.read_text()) if report_path.is_file() else {}
    outcomes = {}
    for record in report.get('records', []):
        outcomes.setdefault(record.get('id'), set()).add(record.get('outcome'))
    passed = {key for key, values in outcomes.items() if values == {'passed'}}
    definitions = test_definitions(root)
    manifest = source_manifest(root, ledger)
    mapping_hash = digest(mapping_payload(ledger))
    source_hash = digest(manifest)
    fresh_report = (report.get('schema_version') == 2 and report.get('successful') is True
                    and report.get('source_sha256') == manifest and report.get('source_digest') == source_hash
                    and report.get('mapping_sha256') == mapping_hash and not report.get('source_changed_during_run'))
    if not fresh_report:
        passed = set()
    scenarios = ledger.get('scenarios', {})
    all_rules = {}
    mapped, pending, offline, native_pending, exceptions = [], [], [], [], []
    native_pending_scenarios = set()
    if ledger.get('schema_version') != 2:
        errors.append('Rule-level schema_version 2 required')
    if strict:
        if report.get('schema_version') != 2 or not report.get('successful'):
            errors.append('Current schema-2 successful regression report required')
        recorded = report.get('source_sha256') or {}
        for name in sorted(set(manifest) ^ set(recorded)):
            errors.append('Regression source file set differs: ' + name)
        for name in sorted(set(manifest) & set(recorded)):
            if manifest[name] != recorded[name]:
                errors.append('Changed since regression: ' + name)
        if not recorded:
            errors.append('Regression source fingerprint cannot be empty')
        if report.get('source_digest') != source_hash or report.get('mapping_sha256') != mapping_hash:
            errors.append('Regression source/rule fingerprint mismatch')
        if report.get('source_changed_during_run'):
            errors.append('Sources changed during regression execution')
        final = ledger.get('final_review') or {}
        if (not final.get('id') or not final.get('reviewer') or not final.get('reviewed_at')
                or final.get('source_digest') != source_hash or final.get('mapping_sha256') != mapping_hash):
            errors.append('Final sharing and rule-mapping review is missing or stale')
    native = _current_native(ledger, root, source_hash, mapping_hash, errors)
    ids = [r['id'] for r in rows]
    if len(ids) != len(set(ids)):
        errors.append('Duplicate capability IDs')
    all_symbols = {(p, name, start) for p, source in sources.items()
                   for name, start, end in symbols(source)}
    nonempty = {p for p, source in sources.items() if source.strip()}
    covered_files = set()
    for row in rows:
        if not row.get('logic') or not row.get('baseline'):
            errors.append(row['id'] + ': missing logic or source evidence')
        for ref in row['baseline']:
            p = ref['file']
            if p not in sources:
                errors.append(row['id'] + ': unknown baseline file ' + p)
                continue
            covered_files.add(p)
            start, end = ref.get('start', 1), ref.get('end', len(sources[p].splitlines()))
            if not 1 <= start <= end <= len(sources[p].splitlines()):
                errors.append(row['id'] + ': invalid baseline range ' + p)
            covered.update((p, name, line) for name, line, _ in symbols(sources[p])
                           if start <= line <= end)
        review = row.get('sharing', {})
        paths = row.get('implementation', [])
        for p in paths:
            if not (root / p).is_file():
                errors.append(row['id'] + ': missing implementation ' + p)
        if strict:
            if not paths:
                errors.append(row['id'] + ': implementation review missing')
            if not review.get('decision') or not review.get('reason'):
                errors.append(row['id'] + ': sharing decision not explained')
        for rule in row.get('logic', []):
            if not isinstance(rule, dict) or not rule.get('id') or not rule.get('text'):
                errors.append(row['id'] + ': rule requires stable ID and text')
                continue
            rid = rule['id']
            if rid in all_rules:
                errors.append('Duplicate rule ID: ' + rid)
            all_rules[rid] = rule
            if not rid.startswith(row['id'] + '.'):
                errors.append(rid + ': wrong capability owner')
            refs = rule.get('scenarios', [])
            if rule.get('review_state') == 'approved-exception':
                if row['id'] != 'F03' or rule.get('exception') != 'specific-plugin-extension':
                    errors.append(rid + ': exception not authorized')
                else:
                    exceptions.append(rid)
                continue
            if rule.get('review_state') != 'mapped' or not refs or not rule.get('review_note'):
                pending.append(rid)
                if strict:
                    errors.append(rid + ': rule evidence mapping remains pending')
                continue
            mapped.append(rid)
            offline_ok = True
            has_offline = False
            for sid in refs:
                scenario = scenarios.get(sid)
                if not isinstance(scenario, dict) or rid not in scenario.get('rule_ids', []):
                    errors.append(rid + ': scenario is not explicitly mapped to this rule: ' + sid)
                    offline_ok = False
                    continue
                if not scenario.get('assertion') or not scenario.get('reviewer'):
                    errors.append(sid + ': assertion review missing')
                kind = scenario.get('kind')
                if kind == 'regression':
                    has_offline = True
                    tests = scenario.get('tests') or {}
                    if not tests:
                        errors.append(sid + ': no concrete tests')
                        offline_ok = False
                    for selector, expected in tests.items():
                        if not expected or definitions.get(selector) != expected:
                            errors.append(sid + ': reviewed test definition missing or changed: ' + selector)
                            offline_ok = False
                        if selector not in passed:
                            offline_ok = False
                            if strict:
                                errors.append(rid + ': test has not passed: ' + selector)
                elif kind == 'manual':
                    current = native.get(sid, {})
                    if (current.get('platform') != scenario.get('platform')
                            or current.get('method') != scenario.get('method')
                            or set(current.get('rule_ids', [])) != set(scenario.get('rule_ids', []))):
                        native_pending.append(rid)
                        native_pending_scenarios.add(sid)
                        if complete:
                            errors.append(rid + ': matching current native scenario not passed: ' + sid)
                else:
                    errors.append(sid + ': unsupported evidence kind')
            if has_offline and offline_ok:
                offline.append(rid)
    for sid, scenario in scenarios.items():
        refs = scenario.get('rule_ids') or []
        if not refs or any(rid not in all_rules or sid not in all_rules[rid].get('scenarios', []) for rid in refs):
            errors.append(sid + ': orphan or inconsistent rule mapping')
    for sid in native:
        if sid not in scenarios or scenarios[sid].get('kind') != 'manual':
            errors.append('Native evidence refers to unknown manual scenario: ' + sid)
    missing = sorted(all_symbols - covered)
    if missing:
        errors.append('Uninventoried symbols: ' + repr(missing))
    if nonempty - covered_files:
        errors.append('Uninventoried modules: ' + repr(sorted(nonempty - covered_files)))
    if strict:
        if ledger.get('inventory_status') != 'reviewed':
            errors.append('Rule inventory review is not complete')
        migration = json.loads((root / ledger['test_migration']).read_text())
        for scenario in migration['scenarios']:
            if scenario.get('status') not in {'replaced', 'baseline-aligned-change'} or not scenario.get('replacement'):
                errors.append('Unreviewed retired test: ' + scenario['old_test'])
            for target in scenario.get('replacement', []):
                file, case = target.split(':', 1)
                module = str(Path(file).relative_to('tests').with_suffix('')).replace('/', '.')
                if module + '.' + case not in passed:
                    errors.append('Retired test replacement has not passed: ' + target)
    if complete and not any(s.get('kind') == 'manual' and s.get('method') == 'telegram-ui'
                            for s in scenarios.values()):
        errors.append('No explicit Telegram UI acceptance scenarios')
    return {'capabilities': len(rows), 'logic_rules': sum(len(r['logic']) for r in rows),
            'baseline_modules': len(nonempty), 'baseline_symbols': len(all_symbols),
            'inventoried_symbols': len(covered), 'statuses': dict(Counter(r['status'] for r in rows)),
            'rule_mapping_reviewed': len(mapped) + len(exceptions),
            'rules_examined_in_current_review': sum(bool(r.get('reviewer') and r.get('reviewed_at')
                                                         and r.get('review_note')) for r in all_rules.values()),
            'offline_rules_passed_current_report': len(offline),
            'pending_rule_ids': sorted(set(pending)),
            'native_pending_rule_ids': sorted(set(native_pending)),
            'native_pending_scenarios': [{'id': sid, 'platform': scenarios[sid].get('platform'),
                                          'method': scenarios[sid].get('method'),
                                          'deferred_by_user': scenarios[sid].get('platform') == 'windows' and ledger.get('verification_scope', {}).get('windows_native') == 'deferred-by-user',
                                          'rule_ids': scenarios[sid].get('rule_ids', [])}
                                         for sid in sorted(native_pending_scenarios)],
            'approved_exception_rule_ids': exceptions,
            'source_digest': source_hash, 'mapping_sha256': mapping_hash,
            'errors': list(dict.fromkeys(errors))}


def render(ledger, summary, destination):
    escape = html.escape
    rows = []
    for row in ledger['capabilities']:
        refs = '<br>'.join(escape(r['file']) + ':' + str(r.get('start', 1)) for r in row['baseline'])
        impl = '<br>'.join(escape(p) for p in row.get('implementation', []))
        tests = '<br>'.join(escape(t) for t in sorted({test for rule in row['logic']
                            for sid in rule.get('scenarios', [])
                            for test in ledger.get('scenarios', {}).get(sid, {}).get('tests', {})}))
        rules = '<ol>' + ''.join('<li><b>' + escape(rule['id']) + '</b> ' + escape(rule['text'])
                                + ' — ' + escape(rule['review_state']) + '<br><small>'
                                + escape(rule.get('review_note', '')) + '</small></li>' for rule in row['logic']) + '</ol>'
        review = row['sharing']
        rows.append('<tr><td>' + escape(row['id']) + '</td><td><b>' + escape(row['name'])
                    + '</b>' + rules + '<details><summary>Windows 基线代码</summary><code>' + refs
                    + '</code></details><details><summary>当前实现与回归证据</summary><code>' + impl
                    + '</code><hr><code>' + tests + '</code></details></td><td>' + escape(row['status']) + '<p>'
                    + escape(row.get('finding', '尚未逐项核对')) + '</p></td><td>'
                    + escape(review.get('decision', 'pending')) + '<p>' + escape(review.get('reason', ''))
                    + '</p><small>最终复检：' + escape(str(ledger.get('final_review', {}).get('id') or '未完成')) + '</small></td></tr>')
    output = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>Windows / macOS 对齐清单</title>
<style>body{font:15px/1.6 system-ui;margin:32px;color:#18212b;background:#f8fafc}h1{font-size:26px}table{border-collapse:collapse;width:100%;background:white}th,td{border:1px solid #ccd5df;padding:12px;vertical-align:top}th{position:sticky;top:0;background:#e8eff7}td:first-child{white-space:nowrap}td:nth-child(2){width:52%}code{font-size:11px}li{margin:4px 0}.notice{padding:16px;background:#fff0c9;border:1px solid #d7b766}</style>
<h1>Windows / macOS 能力与逻辑清单</h1><p>唯一事实源：docs/parity.json。本页面由 tools/audit_parity.py 生成。</p>
<p class="notice">代码已列入清单，不等于功能已验收。任何待核对项、缺口或未完成的最终公共层复检都会阻止完成门禁。具体插件扩展可例外；Windows 实机另行验收。</p>'''
    output += '<p>基线：<code>' + escape(ledger['baseline_commit']) + '</code></p><pre>'
    output += escape(json.dumps(summary, ensure_ascii=False, indent=2)) + '</pre>'
    output += '<p>mapped 只表示逐条断言映射已审阅；本轮测试与实机是否通过以检查结果为准。历史实机不自动变成本轮通过。</p>'
    output += '<details open><summary>验收范围与实机证据</summary><pre>'
    output += escape(json.dumps({'scope': ledger.get('verification_scope', {}),
                                 'native': ledger.get('native_evidence', {})},
                                ensure_ascii=False, indent=2)) + '</pre></details>'
    output += '<ul>' + ''.join('<li>' + escape(note) + '</li>'
                              for note in ledger.get('review_notes', [])) + '</ul>'
    output += '<table><thead><tr><th>ID</th><th>能力与逻辑</th><th>macOS 核对</th><th>公共层归属</th></tr></thead><tbody>'
    output += ''.join(rows) + '</tbody></table></html>'
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--require-complete', action='store_true')
    parser.add_argument('--require-reviewed', action='store_true', help='require the complete code/sharing audit, without claiming live UI acceptance')
    parser.add_argument('--html', type=Path)
    parser.add_argument('--record-final-review', metavar='REVIEW_ID',
                        help='explicitly attest the reviewed rule mapping and source; never changes pending states')
    parser.add_argument('--reviewer', help='required with --record-final-review')
    args = parser.parse_args()
    ledger = json.loads(LEDGER.read_text())
    if args.record_final_review:
        if not args.reviewer:
            parser.error('--record-final-review requires --reviewer')
        structure = audit(ledger)
        if structure['errors']:
            print(json.dumps(structure, ensure_ascii=False, indent=2))
            return 1
        ledger['final_review'] = {'id': args.record_final_review, 'reviewer': args.reviewer,
                                 'reviewed_at': datetime.now(timezone.utc).isoformat(),
                                 'source_digest': structure['source_digest'],
                                 'mapping_sha256': structure['mapping_sha256']}
        LEDGER.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + '\n')
    result = audit(ledger, complete=args.require_complete, reviewed=args.require_reviewed)
    if args.html:
        render(ledger, result, args.html)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return bool(result['errors'])


if __name__ == '__main__':
    raise SystemExit(main())
