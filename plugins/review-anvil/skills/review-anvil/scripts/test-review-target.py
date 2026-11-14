#!/usr/bin/env python3
"""A changed commit or description must not be mistaken for changed review content."""
import copy
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('review_target', HERE / 'review_target.py')
module = None
if (HERE / 'review_target.py').exists():
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)


class TargetTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(module, 'complete diff snapshot implementation is missing')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init', '-q')
        self.cache = self.root / 'cache'

    def git(self, *args, data=None):
        env = dict(os.environ, GIT_AUTHOR_NAME='Fixture', GIT_AUTHOR_EMAIL='fixture@example.invalid',
                   GIT_COMMITTER_NAME='Fixture', GIT_COMMITTER_EMAIL='fixture@example.invalid')
        return subprocess.check_output(['git', '-C', str(self.repo), *args], input=data, env=env)

    def commit(self, files, parent=None, message='fixture'):
        nested = {}
        for path, (mode, content) in files.items():
            node = nested
            parts = path.split('/')
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            oid = self.git('hash-object', '-w', '--stdin', data=content).decode().strip()
            node[parts[-1]] = (mode, oid)
        def tree(node):
            rows = []
            for name, value in sorted(node.items()):
                mode, oid = ('040000', tree(value)) if isinstance(value, dict) else value
                kind = 'tree' if mode == '040000' else 'blob'
                rows.append(f'{mode} {kind} {oid}\t{name}'.encode() + b'\0')
            return self.git('mktree', '-z', data=b''.join(rows)).decode().strip()
        args = ['commit-tree', tree(nested), '-m', message]
        if parent:
            args += ['-p', parent]
        return self.git(*args).decode().strip()

    def target(self, base, head, title='Review fixtures'):
        repo = {'host': 'github.com', 'name': 'acme/widgets', 'id': 42}
        return {'schema_version': 1, 'repository': repo, 'head_repository': dict(repo),
                'target': {'number': 7, 'head_sha': head, 'base_sha': base, 'title': title,
                           'body': 'Preserve consent.', 'url': 'https://github.com/acme/widgets/pull/7',
                           'files': ['config/defaults.json']}, 'labels': []}

    def capture(self, target, ancestor=None):
        ancestor = ancestor or target['target']['base_sha']
        compare = {'base_commit': {'sha': target['target']['base_sha']},
                   'merge_base_commit': {'sha': ancestor}}
        with patch.object(module, 'github_api', return_value=compare), \
             patch.object(module, 'read_pull_target', return_value=copy.deepcopy(target)):
            return module.capture_pull_diff(target, object_repo=self.repo, cache_root=self.cache)

    def test_same_patch_different_commits_and_metadata(self):
        base = self.commit({'config/defaults.json': ('100644', b'{"consent":true}\n')})
        head = self.commit({'config/defaults.json': ('100644', b'{"consent":false}\n')}, base)
        advanced = {'config/defaults.json': ('100644', b'{"consent":true}\n'),
                    'unrelated.txt': ('100644', b'outside the PR delta\n')}
        base2 = self.commit(advanced, base, 'unrelated base change')
        advanced['config/defaults.json'] = ('100644', b'{"consent":false}\n')
        head2 = self.commit(advanced, base2)
        first = self.capture(self.target(base, head))
        second = self.capture(self.target(base2, head2, 'Edited description only'))
        result = module.compare_diff_snapshots(first, second)
        self.assertEqual(result['relation'], 'same')
        self.assertIs(result['metadata_equal'], False)
        self.assertIn('-{"consent":true}', first['diff'])
        self.assertIn('+{"consent":false}', first['diff'])

    def test_binary_and_executable_mode_changes_are_real_changes(self):
        base = self.commit({'binary.bin': ('100644', b'\0a'), 'script.sh': ('100644', b'exit 0\n')})
        head = self.commit({'binary.bin': ('100644', b'\0b'), 'script.sh': ('100644', b'exit 0\n')}, base)
        other = self.commit({'binary.bin': ('100644', b'\0c'), 'script.sh': ('100755', b'exit 0\n')}, base)
        first, second = self.capture(self.target(base, head)), self.capture(self.target(base, other))
        self.assertEqual(module.compare_diff_snapshots(first, second)['relation'], 'changed')
        self.assertEqual({entry['new_path'] for entry in second['entries']}, {'binary.bin', 'script.sh'})

    def test_non_utf8_text_diff_is_losslessly_preserved_and_compared(self):
        base = self.commit({'latin1.txt': ('100644', b'caf\xe9 old\n')})
        head = self.commit({'latin1.txt': ('100644', b'caf\xe9 new\n')}, base)
        newer = self.commit({'latin1.txt': ('100644', b'caf\xe9 changed\n')}, base)
        first = self.capture(self.target(base, head))
        second = self.capture(self.target(base, newer))
        self.assertIn(b'+caf\xe9 new', first['diff'].encode(first['diff_encoding']))
        self.assertIsNotNone(first['entries'][0]['binary'])
        self.assertEqual(module.compare_diff_snapshots(first, second)['relation'], 'changed')

    def test_rename_and_missing_final_newline_are_preserved(self):
        base = self.commit({'old name.txt': ('100644', b'one\ntwo\n')})
        head = self.commit({'new name.txt': ('100644', b'one\ntwo\n')}, base)
        snapshot = self.capture(self.target(base, head))
        self.assertEqual([(x['status'], x['old_path'], x['new_path']) for x in snapshot['entries']],
                         [('R', 'old name.txt', 'new name.txt')])
        no_newline = self.commit({'new name.txt': ('100644', b'one\ntwo')}, base)
        self.assertEqual(module.compare_diff_snapshots(snapshot, self.capture(self.target(base, no_newline)))['relation'], 'changed')

    def test_source_worktree_index_and_external_diff_are_not_touched(self):
        base = self.commit({'config/defaults.json': ('100644', b'old\n')})
        head = self.commit({'config/defaults.json': ('100644', b'new\n')}, base)
        self.git('read-tree', base)
        index = (self.repo / '.git/index').read_bytes()
        sentinel = self.repo / 'dirty.txt'
        sentinel.write_bytes(b'USER DIRTY DATA\n')
        self.git('config', 'diff.external', '/this/must/not/run')
        self.capture(self.target(base, head))
        self.assertEqual(sentinel.read_bytes(), b'USER DIRTY DATA\n')
        self.assertEqual((self.repo / '.git/index').read_bytes(), index)

    def test_metadata_moves_during_capture_and_unknown_observation_do_not_restart(self):
        base = self.commit({'a': ('100644', b'old\n')})
        head = self.commit({'a': ('100644', b'new\n')}, base)
        target = self.target(base, head)
        moved = copy.deepcopy(target)
        moved['target']['body'] = 'changed during acquisition'
        with patch.object(module, 'github_api', return_value={'base_commit': {'sha': base}, 'merge_base_commit': {'sha': base}}), \
             patch.object(module, 'read_pull_target', return_value=moved):
            with self.assertRaises(module.TargetError):
                module.capture_pull_diff(target, object_repo=self.repo, cache_root=self.cache)
        frozen = self.capture(target)
        with patch.object(module, 'read_pull_target', side_effect=RuntimeError('unavailable')):
            result = module.observe_pull_target(frozen, object_repo=self.repo, cache_root=self.cache)
        self.assertEqual(result['relation'], 'unknown')
        self.assertIsNone(result['snapshot'])

    def test_recreated_repository_and_tampered_diff_are_not_equal(self):
        base = self.commit({'a': ('100644', b'old\n')})
        head = self.commit({'a': ('100644', b'new\n')}, base)
        first = self.capture(self.target(base, head))
        other = copy.deepcopy(first)
        other['repository']['id'] = 43
        self.assertEqual(module.compare_diff_snapshots(first, other)['relation'], 'integrity_failure')
        other = copy.deepcopy(first)
        other['entries'][0]['patch'] += '\n+tampered'
        self.assertEqual(module.compare_diff_snapshots(first, other)['relation'], 'unknown')
        other = copy.deepcopy(first)
        other['diff'] = 'incomplete reviewer input'
        self.assertEqual(module.compare_diff_snapshots(first, other)['relation'], 'unknown')

    def test_current_target_requires_complete_files_and_pins_fork_identity(self):
        import memory_github
        repo = {'host': 'github.com', 'name': 'acme/widgets', 'id': 42}
        pr = {'number': 7, 'head': {'sha': 'a' * 40, 'repo': {'id': 43, 'full_name': 'fork/widgets'}},
              'base': {'sha': 'b' * 40, 'repo': {'id': 42, 'full_name': 'acme/widgets'}},
              'title': 'Change', 'body': None, 'html_url': 'https://github.com/acme/widgets/pull/7',
              'changed_files': 1, 'labels': [{'name': 'bug'}]}
        def api(host, endpoint, paginate=False):
            return [{'filename': 'new.txt', 'previous_filename': 'old.txt'}] if '/files?' in endpoint else pr
        with patch.object(memory_github, 'resolve_pull_request', return_value=(repo, 7)), \
             patch.object(memory_github, 'github_api', side_effect=api):
            result = memory_github.read_pull_target('acme/widgets#7')
            self.assertEqual(result['head_repository'], {'host': 'github.com', 'name': 'fork/widgets', 'id': 43})
            self.assertEqual(result['target']['files'], ['new.txt', 'old.txt'])
            pr['changed_files'] = 2
            with self.assertRaises(memory_github.GitHubError):
                memory_github.read_pull_target('acme/widgets#7')

    def test_complete_fork_objects_and_corrupt_remote_blob(self):
        import base64
        base = self.commit({'config/defaults.json': ('100644', b'old\n')})
        head = self.commit({'config/defaults.json': ('100644', b'new\n')}, base)
        target = self.target(base, head)
        target['head_repository'] = {'host': 'github.com', 'name': 'fork/widgets', 'id': 43}
        corrupt = False
        def api(host, endpoint, paginate=False):
            if '/compare/' in endpoint:
                return {'base_commit': {'sha': base}, 'merge_base_commit': {'sha': base}}
            kind, oid = endpoint.split('/')[-2:]
            if kind == 'commits':
                if oid == head and not endpoint.startswith('repos/fork/widgets/'):
                    raise AssertionError('head must be read from its captured fork')
                tree = self.git('rev-parse', oid + '^{tree}').decode().strip()
                return {'sha': oid, 'tree': {'sha': tree}}
            if kind == 'trees':
                entries = []
                for row in self.git('ls-tree', '-z', oid).split(b'\0'):
                    if row:
                        meta, name = row.split(b'\t')
                        mode, node_kind, node_oid = meta.decode().split()
                        entries.append({'mode': mode, 'type': node_kind, 'sha': node_oid, 'path': name.decode()})
                return {'sha': oid, 'truncated': False, 'tree': entries}
            data = self.git('cat-file', 'blob', oid)
            return {'sha': oid, 'encoding': 'base64', 'size': len(data),
                    'content': 'NOT BASE64 !' if corrupt else base64.b64encode(data).decode()}
        with patch.object(module, 'github_api', side_effect=api), \
             patch.object(module, 'read_pull_target', return_value=copy.deepcopy(target)):
            snapshot = module.capture_pull_diff(target, cache_root=self.cache)
            self.assertIn('+new', snapshot['entries'][0]['patch'])
            corrupt = True
            with self.assertRaises(ValueError) as failure:
                module.capture_pull_diff(target, cache_root=self.cache)
            self.assertIsInstance(failure.exception, module.TargetError)


if __name__ == '__main__':
    unittest.main()
