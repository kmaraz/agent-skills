#!/usr/bin/env python3
"""Sequential, source-backed seed learning and a GET-only CLI fixture.

For an isolated native-agent smoke, import install_gh_fixture(directory) and use
its returned environment with REVIEW_ANVIL_MEMORY_HOME under that directory.
The fixture supports acme/widgets PRs, ordinary issue 2, missing 3, and optional
HTTP failures selected by the JSON file at SEED_GH_CONFIG.
"""
import contextlib
import copy
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import memory_github
from memory_rules import LocalRules
from memory_seed import SeedRuns, parse_selectors
from memory_store import MemoryError, MemoryStore

REPOSITORY = 'github.com/acme/widgets'
REMOTE = {'host': 'github.com', 'name': 'acme/widgets', 'id': 42}
HELPER = Path(__file__).with_name('local-memory.py')


def packet(number):
    url = f'https://github.com/acme/widgets/pull/{number}'
    return {'schema_version': 1, 'repository': dict(REMOTE),
            'pr': {'number': number, 'head_sha': 'a' * 40, 'base_sha': 'b' * 40,
                   'title': f'Feedback {number}', 'body': '', 'url': url, 'files': [f'src/item{number}.py']},
            'sources': [{'id': f'issue_comment:{number}', 'kind': 'issue_comment',
                         'body': f'Keep the item {number} snapshot immutable.',
                         'url': url + f'#issuecomment-{number}',
                         'author': {'login': 'maintainer', 'type': 'User'},
                         'updated_at': '2026-11-07T00:00:00Z'}]}


def extract(prepared):
    """Controlled semantic extraction, not an empty apply/checkpoint shortcut."""
    source = next(s for s in prepared['packet']['sources'] if s['kind'] == 'issue_comment')
    number = prepared['packet']['pr']['number']
    return [{'claim': source['body'], 'rationale': 'The maintainer explicitly explains the immutable snapshot contract.',
             'paths': [f'src/item{number}.py'], 'sources': [source['id']], 'kind': 'observation'}]


def assess(rules):
    context = rules.prepare({'repository': REPOSITORY, 'repository_id': 42,
                             'branch': 'main', 'sha': 'b' * 40, 'content': None})
    rules.apply(context, {'context_digest': context['digest'],
                         'dispositions': [{'observation_id': item['id'], 'disposition': 'context-only',
                                           'reason': 'Specific implementation knowledge, not a standing review preference.'}
                                          for item in context['observations']], 'rules': []})


def install_gh_fixture(root):
    """Install a strict, dynamic read-only gh for CLI/native-agent integration."""
    root = Path(root)
    binary = root / 'bin'
    binary.mkdir()
    config = root / 'gh-config.json'
    config.write_text(json.dumps({'issues': [2], 'missing': [3], 'failures': {}}))
    executable = binary / 'gh'
    executable.write_text('''#!/usr/bin/env python3
import json, os, re, sys
args = sys.argv[1:]
with open(os.environ['GH_CALL_LOG'], 'a') as stream:
    stream.write(json.dumps(args) + '\\n')
if not args or args[0] != 'api' or '--method' not in args or args[args.index('--method') + 1] != 'GET':
    sys.exit('forbidden non-GET GitHub operation')
endpoint = args[-1]
config = json.load(open(os.environ['SEED_GH_CONFIG']))
status = config.get('failures', {}).get(endpoint)
if status:
    if status == 'network': sys.exit('private network URL must never leak')
    sys.exit('gh: private diagnostic (HTTP ' + str(status) + ')')
repo = {'id': 42, 'full_name': 'acme/widgets', 'default_branch': 'main'}
if endpoint == 'repos/acme/widgets': value = repo
elif endpoint == 'repos/acme/widgets/git/ref/heads/main':
    value = {'ref': 'refs/heads/main', 'object': {'type': 'commit', 'sha': 'b' * 40}}
elif endpoint == 'repos/acme/widgets/git/commits/' + 'b' * 40:
    value = {'sha': 'b' * 40, 'tree': {'sha': 'd' * 40}}
elif endpoint == 'repos/acme/widgets/git/trees/' + 'd' * 40:
    value = {'sha': 'd' * 40, 'truncated': False, 'tree': []}
else:
    match = re.fullmatch(r'repos/acme/widgets/(pulls|issues)/([1-9][0-9]*)(.*)', endpoint)
    if not match: sys.exit('unexpected endpoint')
    kind, raw, suffix = match.groups()
    number = int(raw)
    if number in config.get('missing', []) or (kind == 'pulls' and number in config.get('issues', [])):
        sys.exit('gh: Not Found (HTTP 404)')
    if kind == 'issues' and not suffix:
        value = {'number': number, 'html_url': 'https://github.com/acme/widgets/issues/' + raw}
        if number not in config.get('issues', []): value['pull_request'] = {'url': 'pull request'}
    elif not suffix:
        value = {'number': number, 'head': {'sha': 'a' * 40}, 'base': {'sha': 'b' * 40},
                 'title': 'Feedback ' + raw, 'body': '', 'changed_files': 1,
                 'html_url': 'https://github.com/acme/widgets/pull/' + raw,
                 'user': {'login': 'author', 'type': 'User'}, 'updated_at': '2026-11-07T00:00:00Z'}
    elif suffix.startswith('/files?'):
        value = [[{'filename': 'src/item' + raw + '.py'}]]
    elif kind == 'issues' and suffix.startswith('/comments?'):
        value = [[{'id': number, 'body': 'Keep the item ' + raw + ' snapshot immutable.',
                   'html_url': 'https://github.com/acme/widgets/pull/' + raw + '#issuecomment-' + raw,
                   'user': {'login': 'maintainer', 'type': 'User'}, 'updated_at': '2026-11-07T00:00:00Z'}]]
    elif suffix.startswith('/reviews?') or suffix.startswith('/comments?'): value = [[]]
    else: sys.exit('unexpected PR endpoint')
print(json.dumps(value))
''')
    executable.chmod(0o700)
    return dict(os.environ, PATH=str(binary) + os.pathsep + os.environ['PATH'],
                REVIEW_ANVIL_MEMORY_HOME=str(root / 'memory'),
                GH_CALL_LOG=str(root / 'gh-calls.jsonl'), SEED_GH_CONFIG=str(config))


class SeedSelectorTests(unittest.TestCase):
    def numbers(self, segments):
        return [n for segment in segments for n in range(segment['start'], segment['end'] + 1)]

    def test_inclusive_overlap_keeps_first_occurrence_order(self):
        self.assertEqual(self.numbers(parse_selectors(REPOSITORY, ['acme/widgets#20-22', '#21-24', '19', '#18-21'])),
                         [20, 21, 22, 23, 24, 19, 18])

    def test_thousand_and_larger_ranges_remain_compact(self):
        self.assertEqual(parse_selectors(REPOSITORY, ['acme/widgets#1-1000', '#998-1002']), [{'start': 1, 'end': 1002}])
        self.assertEqual(parse_selectors(REPOSITORY, ['1-1000000000']), [{'start': 1, 'end': 1000000000}])

    def test_github_and_enterprise_url_forms(self):
        for repository, selectors in [(REPOSITORY, ['https://github.com/acme/widgets/pull/5-7', 'https://github.com/acme/widgets#8']),
                                      ('github.example.com/acme/widgets', ['acme/widgets#5-7', 'https://github.example.com/acme/widgets/pull/8'])]:
            self.assertEqual(self.numbers(parse_selectors(repository, selectors)), [5, 6, 7, 8])

    def test_all_invalid_inputs_reject_the_whole_invocation(self):
        for bad in ('other/widgets#1-3', '#8-2', '#0-3', '#1-', '#1-2-3', '', '#-1', '1 2',
                    'https://evil.example/acme/widgets/pull/1', 'https://github.com/acme/widgets/pull/1?token=x',
                    'https://user@github.com/acme/widgets/pull/1', '9223372036854775808'):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_selectors(REPOSITORY, ['1', bad])
        with self.assertRaises(ValueError):
            parse_selectors(REPOSITORY, [])


class SeedRunTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runs = SeedRuns(REPOSITORY, self.root / 'memory')
        self.runs.init(42)
        self.store = MemoryStore(REPOSITORY, self.root / 'memory')
        self.patch = patch('memory_seed.collect_feedback', side_effect=lambda locator: packet(int(locator.rsplit('/', 1)[1])))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        preflight = patch('memory_seed.github_api', return_value={'number': 1})
        preflight.start()
        self.addCleanup(preflight.stop)

    def start(self, selectors=('1-3',)):
        return self.runs.start(list(selectors))['run_id']

    def ready(self, run_id, name='prepared.json'):
        output = self.root / name
        status = self.runs.next(run_id, output)
        return status, json.loads(Path(status['output']).read_text()) if status['status'] == 'ready' else None

    def learn(self, run_id, name='prepared.json'):
        status, prepared = self.ready(run_id, name)
        self.store.apply(prepared, extract(prepared))
        return self.runs.advance(run_id, prepared)

    def test_ready_requires_real_apply_and_resumes_after_crash_before_ack(self):
        run_id = self.start()
        status, prepared = self.ready(run_id)
        self.assertEqual((status['status'], status['number'], status['processed']), ('ready', 1, 0))
        self.assertEqual(stat.S_IMODE(Path(status['output']).stat().st_mode), 0o600)
        self.assertEqual(prepared['packet']['pr']['number'], 1)
        with self.assertRaises(MemoryError):
            self.runs.advance(run_id, prepared)
        self.assertEqual(self.runs.start(['1-3'])['run_id'], run_id)
        same, replay = self.ready(run_id, 'retry.json')
        self.assertEqual(replay, prepared)
        self.assertEqual(same['number'], 1)
        self.store.apply(prepared, extract(prepared))
        # A restarted coordinator must not attempt to apply the stale transaction twice.
        restarted = SeedRuns(REPOSITORY, self.root / 'memory')
        next_status = restarted.next(run_id, self.root / 'second.json')
        self.assertEqual((next_status['number'], next_status['processed']), (2, 1))
        with self.assertRaises(MemoryError):
            restarted.advance(run_id, prepared)
        self.assertEqual(len(self.store.inspect()['observations']), 1)

    def test_missing_unapplied_preparation_is_recollected_at_same_cursor(self):
        run_id = self.start(('1',))
        status, _ = self.ready(run_id)
        Path(status['output']).unlink()
        resumed, prepared = self.ready(run_id, 'new-session.json')
        self.assertEqual((resumed['number'], resumed['processed']), (1, 0))
        self.store.apply(prepared, extract(prepared))
        self.assertEqual(self.runs.advance(run_id, prepared)['processed'], 1)
        self.assertEqual(len(self.store.inspect()['observations']), 1)

    def test_missing_preparation_after_apply_replays_without_duplicate_knowledge(self):
        run_id = self.start(('1',))
        status, prepared = self.ready(run_id)
        self.store.apply(prepared, extract(prepared))
        Path(status['output']).unlink()
        resumed, replay = self.ready(run_id, 'new-session.json')
        self.assertEqual((resumed['number'], resumed['processed']), (1, 0))
        self.store.apply(replay, extract(replay))
        completed = self.runs.advance(run_id, replay)
        self.assertEqual((completed['processed'], completed['status']), (1, 'rules-pending'))
        self.assertEqual(len(self.store.inspect()['observations']), 1)

    def test_complete_requires_current_authenticated_rule_assessment_and_repeat_refreshes(self):
        run_id = self.start(('1',))
        result = self.learn(run_id)
        self.assertEqual(result['status'], 'rules-pending')
        self.assertEqual(self.runs.start(['1'])['status'], 'rules-pending')
        with self.assertRaises(MemoryError): self.runs.finish(run_id)
        assess(LocalRules(REPOSITORY, self.root / 'memory'))
        oid = next(iter(self.store.inspect()['observations']))
        self.store.correct(oid, 'Only the current item snapshot is immutable.', 'User correction')
        with self.assertRaises(MemoryError): self.runs.finish(run_id)
        assess(LocalRules(REPOSITORY, self.root / 'memory'))
        self.assertEqual(self.runs.finish(run_id)['status'], 'completed')
        self.assertEqual(self.runs.status(run_id)['processed'], 1)
        self.assertNotEqual(self.runs.start(['1'])['run_id'], run_id)
        self.assertEqual(self.runs.next(run_id, self.root / 'unused.json')['status'], 'completed')
        self.assertFalse((self.root / 'unused.json').exists())

    def test_forged_or_unissued_or_wrong_checkpoint_cannot_advance(self):
        run_id = self.start(('1',))
        _, prepared = self.ready(run_id)
        forged = copy.deepcopy(prepared)
        forged['packet']['pr']['number'] = 2
        with self.assertRaises(MemoryError): self.runs.advance(run_id, forged)
        other = self.store.prepare(packet(2))
        self.store.apply(other, extract(other))
        with self.assertRaises(MemoryError): self.runs.advance(run_id, other)
        with self.assertRaises(MemoryError): self.runs.advance(run_id, prepared)
        # A legitimate but different source/head/base/file checkpoint is not this transaction.
        changed = packet(1)
        changed['pr'].update(head_sha='c' * 40, base_sha='d' * 40, files=['src/other.py'])
        changed['sources'][0]['body'] = 'Changed evidence'
        current = self.store.prepare(changed)
        self.store.apply(current, extract(current))
        with self.assertRaises(MemoryError): self.runs.advance(run_id, prepared)
        self.assertEqual(self.runs.status(run_id)['processed'], 0)

    def test_renamed_file_encounter_order_matches_the_normalized_checkpoint(self):
        def renamed(locator):
            value = packet(int(locator.rsplit('/', 1)[1]))
            value['pr']['files'] = ['src/z.py', 'src/old-z.py', 'src/a.py']
            return value
        with patch('memory_seed.collect_feedback', side_effect=renamed):
            run_id = self.start(('1',))
            _, prepared = self.ready(run_id)
        self.assertEqual(prepared['packet']['pr']['files'], ['src/z.py', 'src/old-z.py', 'src/a.py'])
        self.store.apply(prepared, extract(prepared))
        self.assertEqual(self.store.inspect()['prs']['1']['files'], ['src/a.py', 'src/old-z.py', 'src/z.py'])
        self.assertEqual(self.runs.advance(run_id, prepared)['status'], 'rules-pending')

    def test_each_checkpoint_dimension_is_required_for_acknowledgement(self):
        changes = {
            'head': lambda p: p['pr'].update(head_sha='c' * 40),
            'base': lambda p: p['pr'].update(base_sha='c' * 40),
            'files': lambda p: p['pr'].update(files=['src/different.py']),
            'source-body': lambda p: p['sources'][0].update(body='A different explanation.'),
            'source-id': lambda p: p['sources'][0].update(id='issue_comment:200'),
            'deleted-source': lambda p: p.update(sources=[]),
        }
        for name, change in changes.items():
            with self.subTest(name=name):
                runs = SeedRuns(REPOSITORY, self.root / name)
                runs.init(42)
                store = MemoryStore(REPOSITORY, self.root / name)
                run_id = runs.start(['1'])['run_id']
                ready = runs.next(run_id, self.root / f'{name}.json')
                prepared = json.loads(Path(ready['output']).read_text())
                changed = packet(1)
                change(changed)
                replacement = store.prepare(changed)
                proposals = extract(replacement) if changed['sources'] else []
                store.apply(replacement, proposals)
                with self.assertRaises(MemoryError): runs.advance(run_id, prepared)
                self.assertEqual(runs.status(run_id)['processed'], 0)

    def test_prior_matching_checkpoint_plus_unrelated_generation_is_not_an_apply_ack(self):
        prior = self.store.prepare(packet(1))
        self.store.apply(prior, extract(prior))
        run_id = self.start(('1',))
        _, prepared = self.ready(run_id)
        other = self.store.prepare(packet(2))
        self.store.apply(other, extract(other))
        with self.assertRaises(MemoryError): self.runs.advance(run_id, prepared)
        self.assertEqual(self.runs.status(run_id)['processed'], 0)

    def test_concurrent_next_and_ack_advance_only_once(self):
        run_id = self.start()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda index: self.runs.next(run_id, self.root / f'parallel-{index}.json'), range(2)))
        packets = [json.loads(Path(result['output']).read_text()) for result in results]
        self.assertEqual(packets[0]['digest'], packets[1]['digest'])
        self.store.apply(packets[0], extract(packets[0]))
        def acknowledge(_):
            try: return self.runs.advance(run_id, packets[0])
            except MemoryError: return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(acknowledge, range(2)))
        self.assertTrue(any(results))
        status = self.runs.status(run_id)
        self.assertEqual((status['processed'], status['next_pr']), (1, 2))

    def test_journal_is_private_authenticated_and_has_no_prepared_history(self):
        run_id = self.start(('1-1000',))
        for number in range(1, 4): self.learn(run_id, f'{number}.json')
        path = self.runs.directory / 'seed-runs.json'
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertLess(path.stat().st_size, 4096)
        value = json.loads(path.read_text())
        value['runs'][run_id]['processed'] = 999
        path.write_text(json.dumps(value))
        with self.assertRaises(MemoryError): self.runs.status(run_id)



class SeedCLITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env = install_gh_fixture(self.root)

    def cli(self, *args, success=True):
        result = subprocess.run([sys.executable, str(HELPER), *map(str, args)], cwd=self.root,
                                env=self.env, text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode == 0, success, result.stderr)
        return json.loads(result.stdout if success else result.stderr)

    def configure(self, **updates):
        path = Path(self.env['SEED_GH_CONFIG'])
        value = json.loads(path.read_text())
        value.update(updates)
        path.write_text(json.dumps(value))

    def test_thousand_real_cli_transactions_write_knowledge_sequentially_and_resume(self):
        self.configure(issues=[], missing=[])
        # Exercise the real parser/command route in-process; every GitHub read
        # still executes the controlled gh binary. Small CLI tests additionally
        # cover process isolation without starting 3000 extra Python helpers.
        spec = importlib.util.spec_from_file_location('seed_thousand_cli', HELPER)
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        def invoke(*args):
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = helper.main(list(map(str, args)))
            self.assertEqual(code, 0, stderr.getvalue())
            return json.loads(stdout.getvalue())
        with patch.dict(os.environ, self.env):
            run_id = invoke('seed-start', 'acme/widgets', '--seeds', '1-1000')['run_id']
            for number in range(1, 1001):
                path = self.root / f'pr-{number}.json'
                ready = invoke('seed-next', 'acme/widgets', '--run-id', run_id, '--output', path)
                self.assertEqual((ready['number'], ready['processed']), (number, number - 1))
                prepared = json.loads(path.read_text())
                self.assertEqual({source['id'] for source in prepared['packet']['sources']},
                                 {f'pr:{number}:body', f'issue_comment:{number}'})
                observations = self.root / 'observations.json'
                observations.write_text(json.dumps(extract(prepared)))
                invoke('apply', '--prepared', path, '--observations', observations)
                result = invoke('seed-advance', 'acme/widgets', '--run-id', run_id, '--prepared', path)
                self.assertEqual(result['processed'], number)
                if number == 500:
                    # A new process reopens the actual durable run at its cursor.
                    resumed = self.cli('seed-start', 'acme/widgets', '--seeds', '1-1000')
                    self.assertEqual((resumed['run_id'], resumed['next_pr']), (run_id, 501))
            self.assertEqual(result['status'], 'rules-pending')
            rules = LocalRules(REPOSITORY, self.root / 'memory')
            assess(rules)
            self.assertEqual(invoke('seed-finish', 'acme/widgets', '--run-id', run_id)['status'], 'completed')
        store = MemoryStore(REPOSITORY, self.root / 'memory')
        state = store.inspect()
        self.assertEqual(len(state['observations']), 1000)
        self.assertEqual(len(state['prs']), 1000)
        self.assertTrue(all(record['source_versions'] for record in state['observations'].values()))
        self.assertLess((store.directory / 'seed-runs.json').stat().st_size, 4096)

    def test_real_cli_skips_only_confirmed_issue_and_missing_and_finishes_local_only(self):
        start = self.cli('seed-start', 'acme/widgets', '--seeds', 'acme/widgets#1-4', '#4-5')
        run_id = start['run_id']
        self.assertEqual(start['total'], 5)
        for number in (1, 4, 5):
            path = self.root / f'prepared-{number}.json'
            status = self.cli('seed-next', 'acme/widgets', '--run-id', run_id, '--output', path)
            self.assertEqual((status['status'], status['number']), ('ready', number))
            prepared = json.loads(path.read_text())
            observations = self.root / f'observations-{number}.json'
            observations.write_text(json.dumps(extract(prepared)))
            self.cli('apply', '--prepared', path, '--observations', observations)
            self.cli('seed-advance', 'acme/widgets', '--run-id', run_id, '--prepared', path)
        status = self.cli('seed-status', 'acme/widgets', '--run-id', run_id)
        self.assertEqual((status['status'], status['processed'], status['skipped']), ('rules-pending', 3, 2))
        self.assertEqual(status['skips'], [{'start': 2, 'end': 2, 'reason': 'not-a-pull-request'},
                                           {'start': 3, 'end': 3, 'reason': 'missing'}])
        self.cli('seed-finish', 'acme/widgets', '--run-id', run_id, success=False)
        context_path = self.root / 'rules-context.json'
        self.cli('rules-prepare', 'acme/widgets', '--output', context_path)
        context = json.loads(context_path.read_text())
        assessment = self.root / 'assessment.json'
        assessment.write_text(json.dumps({'context_digest': context['digest'], 'rules': [],
            'dispositions': [{'observation_id': observation['id'], 'disposition': 'context-only',
                              'reason': 'Specific implementation knowledge.'} for observation in context['observations']]}))
        self.cli('rules-apply', '--context', context_path, '--assessment', assessment, '--output', self.root / 'rules.json')
        self.assertEqual(self.cli('seed-finish', 'acme/widgets', '--run-id', run_id)['status'], 'completed')
        memory = MemoryStore(REPOSITORY, self.root / 'memory').inspect()
        self.assertEqual(len(memory['observations']), 3)
        calls = [json.loads(line) for line in Path(self.env['GH_CALL_LOG']).read_text().splitlines()]
        self.assertTrue(all(call[call.index('--method') + 1] == 'GET' for call in calls))
        self.assertTrue(all('/pulls?' not in call[-1] for call in calls))

    def test_404_from_discussion_collection_is_not_a_missing_pr(self):
        run_id = self.cli('seed-start', 'acme/widgets', '--seeds', '1')['run_id']
        self.configure(failures={'repos/acme/widgets/issues/1/comments?per_page=100': 404})
        failure = self.cli('seed-next', 'acme/widgets', '--run-id', run_id,
                           '--output', self.root / 'incomplete.json', success=False)
        self.assertEqual(failure['http_status'], 404)
        current = self.cli('seed-status', 'acme/widgets', '--run-id', run_id)
        self.assertEqual((current['next_pr'], current['processed'], current['skipped']), (1, 0, 0))

    def test_invalid_late_selector_never_processes_an_earlier_valid_pr(self):
        self.cli('seed-start', 'acme/widgets', '--seeds', '1', 'other/widgets#2-5', success=False)
        calls = [json.loads(line) for line in Path(self.env['GH_CALL_LOG']).read_text().splitlines()]
        self.assertEqual([call[-1] for call in calls], ['repos/acme/widgets'])
        self.assertFalse((self.root / 'memory').exists())

    def test_rate_auth_network_and_repository_revalidation_stop_at_same_cursor(self):
        run_id = self.cli('seed-start', 'acme/widgets', '--seeds', '3-4')['run_id']
        for status in (403, 429, 'network'):
            self.configure(failures={'repos/acme/widgets/pulls/3': status})
            failure = self.cli('seed-next', 'acme/widgets', '--run-id', run_id, '--output', self.root / f'error-{status}.json', success=False)
            self.assertNotIn('private', failure['error'])
            self.configure(failures={})
            current = self.cli('seed-status', 'acme/widgets', '--run-id', run_id)
            self.assertEqual((current['next_pr'], current['skipped'], current['processed']), (3, 0, 0))
        # A repository-access failure after both 404s may not be called missing.
        with patch.dict(os.environ, self.env):
            spec = importlib.util.spec_from_file_location('seed_cli_inline', HELPER)
            helper = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(helper)
            original = memory_github.github_api
            repository_reads = 0
            def api(host, endpoint, paginate=False):
                nonlocal repository_reads
                if endpoint == 'repos/acme/widgets':
                    repository_reads += 1
                    if repository_reads > 1: raise memory_github.GitHubError('Access lost', http_status=403)
                return original(host, endpoint, paginate)
            with patch('memory_seed.github_api', side_effect=api), patch('memory_github.github_api', side_effect=api):
                with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(helper.main(['seed-next', 'acme/widgets', '--run-id', run_id,
                                                  '--output', str(self.root / 'missing.json')]), 1)
        self.assertEqual(self.cli('seed-status', 'acme/widgets', '--run-id', run_id)['next_pr'], 3)
        status = self.cli('seed-next', 'acme/widgets', '--run-id', run_id, '--output', self.root / 'resumed.json')
        self.assertEqual((status['number'], status['skipped']), (4, 1))


if __name__ == '__main__':
    unittest.main()
