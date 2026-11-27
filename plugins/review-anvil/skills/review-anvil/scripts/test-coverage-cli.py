#!/usr/bin/env python3
"""Local coverage scheduling works offline; publication still verifies GitHub."""
import importlib.util
import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import unittest
from unittest.mock import patch


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CoverageCliTests(unittest.TestCase):
    def setUp(self):
        fixtures = load('coverage_cli_fixtures', 'test-memory-coverage.py')
        self.fixture = fixtures.CoverageTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.prepared = self.fixture.activate(45)
        self.run = self.fixture.coverage.start(self.prepared)
        self.cli = load('offline_coverage_cli', 'local-memory.py')
        environment = patch.dict(os.environ, {'REVIEW_ANVIL_MEMORY_HOME': str(self.fixture.root)})
        environment.start()
        self.addCleanup(environment.stop)

    def invoke(self, arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = self.cli.main(arguments)
        self.assertEqual(code, 0, stderr.getvalue())
        return json.loads(stdout.getvalue())

    def test_canonical_local_status_and_next_do_not_need_network(self):
        with patch.object(self.cli, 'resolve_repository', side_effect=AssertionError('Unexpected network')):
            status = self.invoke(['coverage-status', self.fixture.coverage.repository,
                                  '--run-id', self.run['run_id']])
            self.assertEqual(status['remaining_rules'], 45)
            destination = self.fixture.root / 'batch.json'
            result = self.invoke(['coverage-next', self.fixture.coverage.repository,
                                 '--run-id', self.run['run_id'], '--output', str(destination)])
            batch = json.loads(destination.read_text())
            self.assertEqual(result['batch_index'], batch['batch_index'])
            self.assertEqual(len(batch['rules']), 20)
            self.assertEqual(batch['target'], self.prepared['packet']['pr'])

    def test_reserve_and_outstanding_preserve_signed_batches_in_private_outputs(self):
        destination = self.fixture.root / 'reserved.json'
        inventory = self.fixture.root / 'outstanding.json'
        with patch.object(self.cli, 'resolve_repository', side_effect=AssertionError('Unexpected network')):
            reserved = self.invoke(['coverage-reserve', self.fixture.coverage.repository,
                '--run-id', self.run['run_id'], '--workers', '2', '--output', str(destination)])
            batches = json.loads(destination.read_text())['batches']
            self.assertEqual([batch['batch_index'] for batch in batches], [0, 1])
            self.assertNotIn('batches', reserved)
            self.invoke(['coverage-outstanding', self.fixture.coverage.repository,
                '--run-id', self.run['run_id'], '--output', str(inventory)])
            self.assertEqual(json.loads(inventory.read_text())['batches'], batches)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)

    def test_mixed_case_remote_name_keeps_numeric_identity_and_full_target_checks(self):
        self.fixture.assess(self.run['run_id'])
        repo = {'host': 'github.com', 'name': 'Acme/Widgets', 'id': 42}
        current = {'repository': repo, 'pr': self.prepared['packet']['pr'], 'sources': []}
        report_path = self.fixture.root / 'complete.json'
        with patch.object(self.cli, 'resolve_repository', return_value=repo), \
                patch.object(self.cli, 'collect_feedback', return_value=current):
            finished = self.invoke(['coverage-finish', 'github.com/Acme/Widgets',
                '--run-id', self.run['run_id'], '--output', str(report_path)])
            self.assertEqual(finished['status'], 'completed')
            checked = self.invoke(['coverage-check', '--report', str(report_path),
                '--repository', 'github.com/Acme/Widgets', '--number', '7',
                '--head-sha', self.prepared['packet']['pr']['head_sha']])
            self.assertEqual(checked['status'], 'completed')

    def test_new_start_uses_single_pass_without_reinterpreting_legacy_run(self):
        prepared_path = self.fixture.root / 'prepared-criteria.json'
        prepared_path.write_text(json.dumps(self.prepared))
        with patch.object(self.cli, 'resolve_repository', side_effect=AssertionError('Unexpected network')):
            started = self.invoke(['coverage-start', '--prepared', str(prepared_path)])
            self.assertEqual(started.get('assessment_strategy'), 'single-pass')
            self.assertNotEqual(started['run_id'], self.run['run_id'])
            status = self.invoke(['coverage-status', self.fixture.coverage.repository,
                                  '--run-id', started['run_id']])
            self.assertEqual(status['total_rules'], 45)
            self.assertEqual(status['assessment_strategy'], 'single-pass')
            self.assertEqual(self.fixture.coverage.status(self.run['run_id'])['assessed_rules'], 0)

    def criteria_start(self):
        from memory_criteria import CriteriaCoverage
        return CriteriaCoverage(self.fixture.coverage.repository, self.fixture.root).start(self.prepared)

    def test_criteria_run_cannot_dispatch_legacy_full_evidence_workers(self):
        started = self.criteria_start()
        destination = self.fixture.root / 'forbidden-legacy-batch.json'
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = self.cli.main(['coverage-next', self.fixture.coverage.repository,
                '--run-id', started['run_id'], '--output', str(destination)])
        self.assertEqual(code, 1)
        self.assertFalse(destination.exists())
        self.assertEqual(self.cli.coverage_backend(self.fixture.coverage.repository,
                         started['run_id']).status(started['run_id'])['assessed_rules'], 0)

    def test_missing_applicability_id_is_rejected_without_advancing_phase(self):
        started = self.criteria_start()
        context_path = self.fixture.root / 'criteria-context.json'
        context_path.write_text(json.dumps({
            'repository': self.fixture.coverage.repository, 'target': self.prepared['packet']['pr'],
            'diff': '+widget change', 'manual_policy': 'Keep credentials private.',
            'guidance': 'Review the complete widget change.',
            'source_access': {'base': str(self.fixture.root), 'head': str(self.fixture.root)}}))
        packets_path = self.fixture.root / 'criteria-packets.json'
        self.invoke(['coverage-criteria', self.fixture.coverage.repository,
            '--run-id', started['run_id'], '--context', str(context_path),
            '--output', str(packets_path)])
        packet = json.loads(packets_path.read_text())['packets'][0]
        packet_path = self.fixture.root / 'criteria-packet.json'
        packet_path.write_text(json.dumps(packet))
        results_path = self.fixture.root / 'criteria-results.json'
        results_path.write_text(json.dumps([{'rule_id': rule['id'], 'disposition': 'uncertain',
            'evidence': 'The original convention needs current-code inspection.'}
            for rule in packet['rules'][:-1]]))
        backend = self.cli.coverage_backend(self.fixture.coverage.repository, started['run_id'])
        before = backend.status(started['run_id'])
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = self.cli.main(['coverage-criteria-record', '--packet', str(packet_path),
                                 '--results', str(results_path)])
        self.assertEqual(code, 1)
        self.assertEqual(backend.status(started['run_id']), before)

    def direct_start(self):
        path = self.fixture.root / 'single-prepared.json'
        path.write_text(json.dumps(self.prepared))
        return self.invoke(['coverage-start', '--prepared', str(path)])

    def direct_context(self):
        path = self.fixture.root / 'single-context.json'
        path.write_text(json.dumps({
            'repository': self.fixture.coverage.repository, 'target': self.prepared['packet']['pr'],
            'diff': '+widget change', 'manual_policy': 'Keep credentials private.',
            'guidance': 'Review indirect consumers.', 'source_access': {'root': str(self.fixture.root)}}))
        return path

    def test_actual_direct_cli_context_assign_evidence_record_commit_finish_check(self):
        from memory_criteria import CriteriaCoverage
        with patch.object(self.cli, 'resolve_repository', side_effect=AssertionError('Unexpected network')), \
                patch.object(self.cli, 'collect_feedback', side_effect=AssertionError('Unexpected network')):
            started = self.direct_start()
            context = self.direct_context()
            destination = self.fixture.root / 'direct-assignments.json'
            args = [self.fixture.coverage.repository, '--run-id', started['run_id']]
            result = self.invoke(['coverage-assign', *args, '--context', str(context),
                                  '--reviewers', 'correctness', 'security', '--output', str(destination)])
            self.assertEqual(result['assessment_strategy'], 'single-pass')
            self.assertFalse(result['applicability_required'])
            assignments = json.loads(destination.read_text())['assignments']
            again = self.fixture.root / 'direct-assignments-again.json'
            self.invoke(['coverage-assign', *args, '--reviewers', 'correctness', 'security', '--output', str(again)])
            self.assertEqual(json.loads(again.read_text())['assignments'], assignments)
            evidence = self.fixture.root / 'direct-originals.json'
            self.invoke(['coverage-evidence', *args, '--output', str(evidence)])
            self.assertEqual(len(json.loads(evidence.read_text())['packets']), 45)
            for index, assignment in enumerate(assignments):
                response = {'reviewer_id': assignment['reviewer_id'],
                            'review_markdown': 'Actual review.\n```findings\n[]\n```',
                            'result_groups': [{'indices': list(range(len(assignment['rule_ids']))),
                                               'verdict': 'violated' if index == 0 else 'not_applicable',
                                               'evidence': 'Inspected frozen widget consumers and every criterion condition.'}],
                            'candidates_json': json.dumps([
                                {'rule_id': rule['id'], 'source_url': rule['source_urls'][0],
                                 'custom': {'nested': [True, {'value': 7}]}} for rule in assignment['rules']]) if index == 0 else '[]'}
                a, r = self.fixture.root / f'assign-{index}.json', self.fixture.root / f'response-{index}.json'
                a.write_text(json.dumps(assignment)); r.write_text(json.dumps(response))
                self.invoke(['coverage-review-record', '--assignment', str(a), '--response', str(r)])
            self.invoke(['coverage-commit', *args])
            status = self.invoke(['coverage-status', *args])
            self.assertEqual((status['assessed_rules'], status['applicability_remaining_rules']), (45, 0))
            self.assertEqual(status['pending_reviewer_ids'], [])
        repo = {'host': 'github.com', 'name': 'Acme/Widgets', 'id': 42}
        current = {'repository': repo, 'pr': self.prepared['packet']['pr'], 'sources': []}
        report = self.fixture.root / 'direct-report.json'
        with patch.object(self.cli, 'resolve_repository', return_value=repo), \
                patch.object(self.cli, 'collect_feedback', return_value=current):
            self.assertEqual(self.invoke(['coverage-finish', *args, '--output', str(report)])['status'], 'completed')
            self.assertEqual(self.invoke(['coverage-check', '--report', str(report)])['status'], 'completed')
        backend = CriteriaCoverage(self.fixture.coverage.repository, self.fixture.root)
        saved = json.loads((backend.directory / f"coverage-criteria-{started['run_id']}.json").read_text())
        candidate = next(iter(saved['reviews'].values()))['candidates'][0]
        self.assertEqual(candidate['custom'], {'nested': [True, {'value': 7}]})
        self.assertEqual(candidate['coverage_provenance']['run_id'], started['run_id'])

    def test_direct_cli_requires_context_and_rejects_applicability_and_legacy_scheduling(self):
        started = self.direct_start()
        args = [self.fixture.coverage.repository, '--run-id', started['run_id']]
        for command, extra in [
                ('coverage-assign', ['--reviewers', 'one']),
                ('coverage-criteria', ['--context', str(self.direct_context())]),
                ('coverage-next', []), ('coverage-reserve', []), ('coverage-outstanding', [])]:
            path = self.fixture.root / f'forbidden-{command}.json'
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = self.cli.main([command, *args, *extra, '--output', str(path)])
            self.assertEqual(code, 1, command)
            self.assertFalse(path.exists(), command)

    def test_direct_applicability_record_reports_protocol_error_before_mutation(self):
        started = self.direct_start()
        args = [self.fixture.coverage.repository, '--run-id', started['run_id']]
        assignments_path = self.fixture.root / 'direct-protocol-assignments.json'
        self.invoke(['coverage-assign', *args, '--context', str(self.direct_context()),
                     '--reviewers', 'one', '--output', str(assignments_path)])
        assignment = json.loads(assignments_path.read_text())['assignments'][0]
        packet_path = self.fixture.root / 'direct-protocol-packet.json'
        results_path = self.fixture.root / 'direct-protocol-results.json'
        packet_path.write_text(json.dumps(assignment)); results_path.write_text('[]')
        phase_path = self.fixture.memory.directory / f"coverage-criteria-{started['run_id']}.json"
        before = phase_path.read_bytes()
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = self.cli.main(['coverage-criteria-record', '--packet', str(packet_path),
                                 '--results', str(results_path)])
        self.assertEqual(code, 1)
        self.assertRegex(stderr.getvalue(), '(?i)single-pass.*applicability')
        self.assertEqual(phase_path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
