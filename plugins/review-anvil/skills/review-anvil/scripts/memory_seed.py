"""Compact, resumable PR seeding around the standard semantic prepare/apply flow."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import uuid

from memory_github import (GitHubError, _host, _name, _number, _url,
                           collect_feedback, github_api)
from memory_store import (MAX_FILE_BYTES, MemoryError, MemoryStore, _MISSING,
                          _digest, _integer, _json, _now, _paths, _repository)

_JOURNAL = 'seed-runs.json'
_RUN_FIELDS = {'invocation', 'segments', 'total', 'processed', 'skipped', 'skips',
               'cursor', 'issued', 'status', 'created_at', 'completed_generation'}
_ISSUED_FIELDS = {'digest', 'cursor', 'generation', 'audit_length', 'output'}


def parse_selectors(repository: str, selectors: list[str]) -> list[dict[str, int]]:
    """Validate every selector, subtract overlap, and keep first-occurrence order.

    Neither parsing nor cursor storage enumerates a range's PR numbers.
    """
    try:
        canonical = _repository(repository)
        host, owner, repo = canonical.split('/')
        _host(host)
        _name(owner + '/' + repo)
        if not isinstance(selectors, list) or not selectors:
            raise ValueError('At least one seed PR selector is required')
        result, covered = [], []
        for selector in selectors:
            if not isinstance(selector, str) or not selector or any(c.isspace() or ord(c) < 32 for c in selector):
                raise ValueError('Expected a PR number or inclusive PR range')
            selected_host, selected_name = host, owner + '/' + repo
            if '://' in selector:
                if '#' in selector:
                    if selector.count('#') != 1:
                        raise ValueError('Expected one PR range after the repository URL')
                    locator, raw = selector.split('#')
                    selected_host, selected_name = _url(locator)
                else:
                    match = re.fullmatch(r'(https://[^?#]+/pull/)([1-9][0-9]*(?:-[1-9][0-9]*)?)/?', selector)
                    if not match:
                        raise ValueError('Expected an HTTPS pull request URL or inclusive range')
                    raw = match[2]
                    selected_host, selected_name, _ = _url(match[1] + raw.split('-')[0], pull=True)
            elif '#' in selector and not selector.startswith('#'):
                if selector.count('#') != 1:
                    raise ValueError('Expected owner/repository#number[-number]')
                selected_name, raw = selector.split('#')
                _name(selected_name)
            else:
                raw = selector[1:] if selector.startswith('#') else selector
            if f'{selected_host}/{selected_name}'.lower() != canonical:
                raise ValueError('Every seed selector must belong to the initialized repository')
            if not re.fullmatch(r'[1-9][0-9]*(?:-[1-9][0-9]*)?', raw):
                raise ValueError('Expected a positive PR number or inclusive ascending range')
            ends = raw.split('-')
            start, end = _number(ends[0]), _number(ends[-1])
            if start > end:
                raise ValueError('Seed ranges must be ascending and inclusive')
            pending = [(start, end)]
            for left, right in covered:
                remaining = []
                for a, b in pending:
                    if b < left or a > right:
                        remaining.append((a, b))
                    else:
                        if a < left: remaining.append((a, left - 1))
                        if b > right: remaining.append((right + 1, b))
                pending = remaining
                if not pending: break
            for a, b in pending:
                if result and result[-1]['end'] + 1 == a:
                    result[-1]['end'] = b
                else:
                    result.append({'start': a, 'end': b})
            merged = []
            for a, b in sorted([*covered, (start, end)]):
                if merged and a <= merged[-1][1] + 1:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], b))
                else:
                    merged.append((a, b))
            covered = merged
        return result
    except (GitHubError, MemoryError) as exc:
        raise ValueError(str(exc)) from exc


def _read_prepared(path):
    """Read an untrusted artifact without following a link or opening a device."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size > MAX_FILE_BYTES):
            raise MemoryError('Seed preparation must be a private bounded regular JSON file')
        data = stream.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise MemoryError('Seed preparation exceeds the supported size')
    try:
        return json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise MemoryError('Invalid seed preparation JSON') from exc


def _write_prepared(path, prepared):
    """Do not overwrite a snapshot; identical orphan output is crash-recoverable."""
    path = Path(os.path.abspath(path))
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        if _read_prepared(path) != prepared:
            raise MemoryError('Seed output already exists with different content; choose a fresh output path') from None
        return str(path)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        stream.write(_json(prepared) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    return str(path)


class SeedRuns(MemoryStore):
    """Signed run journal using the MemoryStore lock, key, and atomic filesystem IO.

    Only a current cursor's issued digest/path is retained, never historical PR
    packets. GitHub collection happens outside the lock; publication rechecks the
    cursor and generation before issuing one standard MemoryStore preparation.
    """

    def _journal(self, directory, state):
        journal = self._read_json(directory, _JOURNAL, missing=True)
        if journal is _MISSING:
            return {'schema_version': 1, 'repository': self.repository,
                    'repository_id': state['repository_id'], 'runs': {}}
        fields = {'schema_version', 'repository', 'repository_id', 'runs', 'signature', 'digest'}
        if not isinstance(journal, dict) or set(journal) != fields:
            raise MemoryError('Invalid seed journal structure')
        self._authenticate(directory, 'seed-runs', journal)
        if (journal['schema_version'] != 1 or journal['repository'] != self.repository
                or journal['repository_id'] != state['repository_id'] or not isinstance(journal['runs'], dict)):
            raise MemoryError('Seed journal repository identity mismatch')
        for run_id, run in journal['runs'].items():
            if not isinstance(run_id, str) or not re.fullmatch(r'[a-f0-9]{32}', run_id):
                raise MemoryError('Invalid seed run ID')
            if not isinstance(run, dict) or set(run) != _RUN_FIELDS:
                raise MemoryError('Invalid seed run structure')
            segments = run['segments']
            if not isinstance(segments, list) or not segments:
                raise MemoryError('Invalid seed range segments')
            total = 0
            for segment in segments:
                if not isinstance(segment, dict) or set(segment) != {'start', 'end'}:
                    raise MemoryError('Invalid seed range segment')
                start = _integer(segment['start'], 'seed range start')
                end = _integer(segment['end'], 'seed range end', start)
                total += end - start + 1
            if run['total'] != total or run['invocation'] != self._invocation(segments):
                raise MemoryError('Invalid seed invocation or total')
            processed = _integer(run['processed'], 'processed PR count', 0)
            skipped = _integer(run['skipped'], 'skipped PR count', 0)
            cursor = run['cursor']
            if not isinstance(cursor, dict) or set(cursor) != {'segment', 'number'}:
                raise MemoryError('Invalid seed cursor')
            index = _integer(cursor['segment'], 'seed segment cursor', 0)
            if index > len(segments):
                raise MemoryError('Invalid seed cursor segment')
            position = sum(s['end'] - s['start'] + 1 for s in segments[:index])
            if index == len(segments):
                if cursor['number'] is not None:
                    raise MemoryError('Exhausted seed cursor has a PR number')
            else:
                number = _integer(cursor['number'], 'seed PR cursor', segments[index]['start'])
                if number > segments[index]['end']:
                    raise MemoryError('Seed cursor is outside its segment')
                position += number - segments[index]['start']
            if position != processed + skipped:
                raise MemoryError('Seed cursor and progress disagree')
            if run['status'] not in {'collecting', 'rules-pending', 'completed'} or ((position == total) != (run['status'] != 'collecting')):
                raise MemoryError('Seed status and cursor disagree')
            if not isinstance(run['skips'], list):
                raise MemoryError('Invalid seed skip records')
            skip_total = 0
            for skip in run['skips']:
                if not isinstance(skip, dict) or set(skip) != {'start', 'end', 'reason'} or skip['reason'] not in {'missing', 'not-a-pull-request'}:
                    raise MemoryError('Invalid seed skip record')
                skip_total += _integer(skip['end'], 'skip end', _integer(skip['start'], 'skip start')) - skip['start'] + 1
            if skip_total != skipped:
                raise MemoryError('Seed skip records and counter disagree')
            issued = run['issued']
            if issued is not None:
                if not isinstance(issued, dict) or set(issued) != _ISSUED_FIELDS or issued['cursor'] != position or run['status'] != 'collecting':
                    raise MemoryError('Invalid issued seed cursor')
                if not isinstance(issued['digest'], str) or not re.fullmatch(r'[a-f0-9]{64}', issued['digest']):
                    raise MemoryError('Invalid issued preparation digest')
                _integer(issued['generation'], 'issued generation', 0)
                _integer(issued['audit_length'], 'issued audit cursor', 0)
                if not isinstance(issued['output'], str) or not os.path.isabs(issued['output']):
                    raise MemoryError('Invalid seed preparation path')
            if run['status'] == 'completed':
                _integer(run['completed_generation'], 'completed seed generation', 0)
            elif run['completed_generation'] is not None:
                raise MemoryError('Incomplete seed run has a completion generation')
        return journal

    def _save_journal(self, directory, journal):
        unsigned = {key: value for key, value in journal.items() if key not in {'signature', 'digest'}}
        self._write_json(directory, _JOURNAL, self._seal(directory, 'seed-runs', unsigned))

    def _invocation(self, segments):
        return _digest({'repository': self.repository, 'segments': segments})

    @staticmethod
    def _run(journal, run_id):
        if not isinstance(run_id, str) or run_id not in journal['runs']:
            raise MemoryError('Unknown seed run ID for this repository')
        return journal['runs'][run_id]

    def _summary(self, run_id, run, skips=False):
        result = {'repository': self.repository, 'run_id': run_id, 'status': run['status'],
                  'total': run['total'], 'processed': run['processed'], 'skipped': run['skipped'],
                  'remaining': run['total'] - run['processed'] - run['skipped'],
                  'next_pr': run['cursor']['number']}
        if skips:
            result['skips'] = [dict(skip) for skip in run['skips']]
        return result

    def start(self, selectors):
        segments = parse_selectors(self.repository, selectors)
        invocation = self._invocation(segments)
        with self._locked(exclusive=True) as directory:
            state = self._state(directory)
            if state['repository_id'] is None:
                raise MemoryError('Resolve and initialize repository numeric identity before seeding')
            journal = self._journal(directory, state)
            for run_id, run in journal['runs'].items():
                if run['invocation'] == invocation and run['status'] != 'completed':
                    return self._summary(run_id, run)
            run_id = uuid.uuid4().hex
            run = {'invocation': invocation, 'segments': segments,
                   'total': sum(s['end'] - s['start'] + 1 for s in segments),
                   'processed': 0, 'skipped': 0, 'skips': [],
                   'cursor': {'segment': 0, 'number': segments[0]['start']},
                   'issued': None, 'status': 'collecting', 'created_at': _now(),
                   'completed_generation': None}
            journal['runs'][run_id] = run
            self._save_journal(directory, journal)
            return self._summary(run_id, run)

    @staticmethod
    def _move(run, reason=None):
        number = run['cursor']['number']
        if reason is None:
            run['processed'] += 1
        else:
            run['skipped'] += 1
            skips = run['skips']
            if skips and skips[-1]['end'] + 1 == number and skips[-1]['reason'] == reason:
                skips[-1]['end'] = number
            else:
                skips.append({'start': number, 'end': number, 'reason': reason})
        index = run['cursor']['segment']
        if number < run['segments'][index]['end']:
            run['cursor']['number'] += 1
        else:
            index += 1
            run['cursor'] = {'segment': index, 'number': run['segments'][index]['start'] if index < len(run['segments']) else None}
        run['issued'] = None
        if run['cursor']['number'] is None:
            run['status'] = 'rules-pending'

    def _issued_prepared(self, directory, state, run, prepared):
        if not isinstance(prepared, dict) or prepared.get('repo') != self.repository:
            raise MemoryError('A helper-issued preparation for this repository is required')
        self._authenticate(directory, 'prepare', prepared)
        self._packet(prepared.get('packet'), state)
        issued = run['issued']
        if (issued is None or issued['digest'] != prepared.get('digest')
                or issued['cursor'] != run['processed'] + run['skipped']
                or issued['generation'] != prepared.get('generation')
                or prepared['packet']['pr']['number'] != run['cursor']['number']):
            raise MemoryError('Preparation does not match this seed run and its current cursor')
        return issued

    def _committed(self, state, prepared, issued):
        if state['generation'] <= issued['generation']:
            return False
        packet = prepared['packet']
        pr = packet['pr']
        sources = {source['id']: source for source in packet['sources']}
        expected = {'head_sha': pr['head_sha'], 'base_sha': pr['base_sha'],
                    'files': _paths(pr['files'], glob=False), 'source_ids': sorted(sources)}
        if state['prs'].get(str(pr['number'])) != expected:
            return False
        if any(state['sources'].get(sid) != {'source': source, 'pr_number': pr['number'], 'version': _digest(source)}
               for sid, source in sources.items()):
            return False
        # A previously identical checkpoint followed only by an unrelated memory
        # mutation is not evidence that this preparation was actually applied.
        return any(event.get('action') == 'checkpoint' and event.get('pr') == pr['number']
                   and event.get('head_sha') == pr['head_sha']
                   and event.get('changed_sources') == [source['id'] for source in prepared['changed_sources']]
                   and event.get('deleted_sources') == prepared['deleted_sources']
                   for event in state['audit'][issued['audit_length']:])

    def _collect(self, number, repository_id):
        host, name = self.repository.split('/', 1)
        endpoint = f'repos/{name}/pulls/{number}'
        try:
            # Classify only an explicit pull endpoint 404, not a failure from a
            # later discussion/files endpoint in complete feedback collection.
            github_api(host, endpoint)
        except GitHubError as error:
            if error.http_status != 404:
                raise
            try:
                issue = github_api(host, f'repos/{name}/issues/{number}')
            except GitHubError as issue_error:
                if issue_error.http_status != 404:
                    raise
                repository = github_api(host, f'repos/{name}')
                if (not isinstance(repository, dict) or type(repository.get('id')) is not int
                        or repository['id'] != repository_id
                        or not isinstance(repository.get('full_name'), str)
                        or repository['full_name'].lower() != name):
                    raise GitHubError('Could not revalidate repository access and identity after missing PR')
                return None, 'missing'
            if (not isinstance(issue, dict) or type(issue.get('number')) is not int
                    or issue['number'] != number
                    or not isinstance(issue.get('html_url'), str)
                    or issue['html_url'].lower() != f'https://{self.repository}/issues/{number}'):
                raise GitHubError('Could not confirm the missing pull request is an ordinary issue')
            if 'pull_request' in issue:
                raise GitHubError('Pull request is unavailable but its issue still exists; resume after access is restored')
            return None, 'not-a-pull-request'
        return collect_feedback(f'https://{self.repository}/pull/{number}'), None

    def next(self, run_id, output):
        output = Path(os.path.abspath(output))
        while True:
            with self._locked(exclusive=True) as directory:
                state = self._state(directory)
                journal = self._journal(directory, state)
                run = self._run(journal, run_id)
                if run['status'] != 'collecting':
                    return self._summary(run_id, run)
                if run['issued'] is not None:
                    try:
                        prepared = _read_prepared(run['issued']['output'])
                    except FileNotFoundError:
                        # Temporary artifacts may disappear between host sessions.
                        # Recollect this cursor; an already applied packet replays
                        # idempotently rather than trusting a missing artifact.
                        run['issued'] = None
                        self._save_journal(directory, journal)
                    else:
                        issued = self._issued_prepared(directory, state, run, prepared)
                        if self._committed(state, prepared, issued):
                            # Recover an apply-committed / acknowledgement-lost crash.
                            self._move(run)
                            self._save_journal(directory, journal)
                            continue
                        if issued['generation'] == state['generation']:
                            issued['output'] = _write_prepared(output, prepared)
                            self._save_journal(directory, journal)
                            return {**self._summary(run_id, run), 'status': 'ready',
                                    'number': run['cursor']['number'], 'url': prepared['packet']['pr']['url'],
                                    'output': issued['output']}
                position = run['processed'] + run['skipped']
                number = run['cursor']['number']
                generation = state['generation']
                repository_id = state['repository_id']
            packet, reason = self._collect(number, repository_id)
            with self._locked(exclusive=True) as directory:
                state = self._state(directory)
                journal = self._journal(directory, state)
                run = self._run(journal, run_id)
                if run['processed'] + run['skipped'] != position or run['cursor']['number'] != number:
                    continue
                if state['generation'] != generation:
                    raise MemoryError('Memory changed during seed collection; resume seed-next at the same cursor')
                if run['issued'] is not None and run['issued']['generation'] == generation:
                    # A competing next caller already issued this cursor; reload
                    # its authenticated artifact instead of replacing the digest.
                    continue
                if reason:
                    self._move(run, reason)
                    self._save_journal(directory, journal)
                    continue
                if packet['pr']['number'] != number:
                    raise MemoryError('Collected PR does not match the seed cursor')
                prepared = self._seal(directory, 'prepare', self._prepared(packet, state))
                path = _write_prepared(output, prepared)
                run['issued'] = {'digest': prepared['digest'], 'cursor': position,
                                 'generation': generation, 'audit_length': len(state['audit']), 'output': path}
                self._save_journal(directory, journal)
                return {**self._summary(run_id, run), 'status': 'ready', 'number': number,
                        'url': prepared['packet']['pr']['url'], 'output': path}

    def advance(self, run_id, prepared):
        with self._locked(exclusive=True) as directory:
            state = self._state(directory)
            journal = self._journal(directory, state)
            run = self._run(journal, run_id)
            issued = self._issued_prepared(directory, state, run, prepared)
            if not self._committed(state, prepared, issued):
                raise MemoryError('Seed preparation has not been committed by matching MemoryStore.apply; apply before advancing')
            self._move(run)
            self._save_journal(directory, journal)
            return self._summary(run_id, run)

    def finish(self, run_id):
        from memory_rules import LocalRules
        with self._locked(exclusive=True) as directory:
            state = self._state(directory)
            journal = self._journal(directory, state)
            run = self._run(journal, run_id)
            if run['status'] == 'completed':
                return self._summary(run_id, run)
            if run['status'] != 'rules-pending':
                raise MemoryError('Seed collection is not exhausted; continue seed-next before finishing')
            metadata = LocalRules(self.repository, self.root)._metadata(directory, state)
            assessment = metadata['assessment']
            if (not isinstance(assessment, dict) or type(assessment.get('generation')) is not int
                    or assessment['generation'] != state['generation']):
                raise MemoryError('Seed rules are pending: apply an authenticated local-rule assessment at the current memory generation')
            run['status'] = 'completed'
            run['completed_generation'] = state['generation']
            self._save_journal(directory, journal)
            return self._summary(run_id, run)

    def status(self, run_id):
        with self._locked() as directory:
            state = self._state(directory)
            return self._summary(run_id, self._run(self._journal(directory, state), run_id), skips=True)
