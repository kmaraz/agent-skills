#!/usr/bin/env python3
"""A changed commit or description must not be mistaken for changed review content."""
import copy
import hashlib
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
            oid = (content.decode('ascii') if mode == '160000' else
                   self.git('hash-object', '-w', '--stdin', data=content).decode().strip())
            node[parts[-1]] = (mode, oid)
        def tree(node):
            rows = []
            for name, value in sorted(node.items()):
                mode, oid = ('040000', tree(value)) if isinstance(value, dict) else value
                kind = 'tree' if mode == '040000' else 'commit' if mode == '160000' else 'blob'
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

    def test_type_changes_preserve_both_patch_sections_and_following_entry(self):
        path = 'a "link" café'
        for old_mode, before, new_mode, after in [
                ('100644', b'original regular content\n', '120000', b'target/path'),
                ('120000', b'target/path', '100644', b'original regular content\n')]:
            with self.subTest(old_mode=old_mode, new_mode=new_mode):
                base = self.commit({path: (old_mode, before),
                                    'z-following.txt': ('100644', b'independent before\n')})
                head = self.commit({path: (new_mode, after),
                                    'z-following.txt': ('100644', b'independent after\n')}, base)
                snapshot = self.capture(self.target(base, head))
                self.assertEqual([(entry['status'], entry['old_path'], entry['new_path'],
                                   entry['old_mode'], entry['new_mode'])
                                  for entry in snapshot['entries']],
                                 [('T', path, path, old_mode, new_mode),
                                  ('M', 'z-following.txt', 'z-following.txt', '100644', '100644')])
                changed, following = snapshot['entries']
                self.assertIsNone(changed['binary'])
                self.assertIn('deleted file mode ' + old_mode, changed['patch'])
                self.assertIn('new file mode ' + new_mode, changed['patch'])
                self.assertIn('-' + before.decode().rstrip('\n'), changed['patch'])
                self.assertIn('+' + after.decode().rstrip('\n'), changed['patch'])
                self.assertIn('\\ No newline at end of file', changed['patch'])
                self.assertNotIn('independent', changed['patch'])
                self.assertIn('-independent before\n+independent after', following['patch'])
                self.assertNotIn('target/path', following['patch'])
                expected = self.git('-c', 'core.quotePath=false', 'diff-tree',
                                    '--no-commit-id', '-r', '--find-renames=50%',
                                    '--find-copies=50%', '--no-ext-diff', '--no-textconv',
                                    '--diff-algorithm=myers', '--no-indent-heuristic',
                                    '--patch', '--binary', '--full-index', '--unified=3',
                                    '--no-color', base, head)
                self.assertEqual(snapshot['diff'].encode(snapshot['diff_encoding']), expected)
                self.assertEqual(module.compare_diff_snapshots(snapshot, snapshot)['relation'], 'same')

    def test_type_change_rejects_incomplete_extra_or_mismatched_patch_sections(self):
        base = self.commit({'a-link': ('100644', b'original\n'),
                            'z-following.txt': ('100644', b'independent before\n')})
        head = self.commit({'a-link': ('120000', b'target/path'),
                            'z-following.txt': ('100644', b'independent after\n')}, base)
        real_git = module._git
        for corruption in ['missing', 'extra', 'path', 'mode']:
            with self.subTest(corruption=corruption):
                def git(repo, *args, **kwargs):
                    output = real_git(repo, *args, **kwargs)
                    if args[0] != 'diff-tree' or '--patch' not in args:
                        return output
                    sections = output.split(b'diff --git ')
                    if corruption == 'missing':
                        return b'diff --git '.join(sections[:2] + sections[3:])
                    if corruption == 'extra':
                        return output + b'diff --git ' + sections[1]
                    if corruption == 'path':
                        return output.replace(b'a/a-link b/a-link', b'a/other b/other', 1)
                    return output.replace(b'deleted file mode 100644', b'deleted file mode 100755', 1)
                with patch.object(module, '_git', side_effect=git):
                    with self.assertRaises(module.TargetError):
                        self.capture(self.target(base, head))

    def test_binary_type_changes_preserve_both_content_digests(self):
        for old_mode, before, new_mode, after in [
                ('100644', b'\0binary content\n', '120000', b'target/path'),
                ('120000', b'target/path', '100644', b'\0binary content\n')]:
            with self.subTest(old_mode=old_mode, new_mode=new_mode):
                base = self.commit({'a-link': (old_mode, before),
                                    'z-following.txt': ('100644', b'independent before\n')})
                head = self.commit({'a-link': (new_mode, after),
                                    'z-following.txt': ('100644', b'independent after\n')}, base)
                newer = self.commit({'a-link': (new_mode, after + b'changed'),
                                     'z-following.txt': ('100644', b'independent after\n')}, base)
                snapshot = self.capture(self.target(base, head))
                changed, following = snapshot['entries']
                self.assertEqual((changed['status'], changed['old_path'], changed['new_path'],
                                  changed['old_mode'], changed['new_mode']),
                                 ('T', 'a-link', 'a-link', old_mode, new_mode))
                self.assertIsNone(changed['patch'])
                self.assertEqual(changed['binary'], {'old': hashlib.sha256(before).hexdigest(),
                                                     'new': hashlib.sha256(after).hexdigest()})
                self.assertEqual(following['new_path'], 'z-following.txt')
                self.assertIn('-independent before\n+independent after', following['patch'])
                self.assertIn('GIT binary patch', snapshot['diff'])
                self.assertIn('target/path', snapshot['diff'])
                self.assertEqual(module.compare_diff_snapshots(snapshot, snapshot)['relation'], 'same')
                self.assertEqual(module.compare_diff_snapshots(
                    snapshot, self.capture(self.target(base, newer)))['relation'], 'changed')

    def test_binary_gitlink_type_changes_bind_the_gitlink_commit_identity(self):
        link_a = self.commit({'submodule.txt': ('100644', b'first commit\n')}).encode('ascii')
        link_b = self.commit({'submodule.txt': ('100644', b'second commit\n')}).encode('ascii')
        binary = b'\0binary content\n'
        for old_mode, new_mode in [('100644', '160000'), ('160000', '100644')]:
            with self.subTest(old_mode=old_mode, new_mode=new_mode):
                before = binary if old_mode == '100644' else link_a
                after = binary if new_mode == '100644' else link_a
                other_before = binary if old_mode == '100644' else link_b
                other_after = binary if new_mode == '100644' else link_b
                base = self.commit({'a-node': (old_mode, before)})
                head = self.commit({'a-node': (new_mode, after)}, base)
                other_base = self.commit({'a-node': (old_mode, other_before)})
                other_head = self.commit({'a-node': (new_mode, other_after)}, other_base)
                snapshot = self.capture(self.target(base, head))
                other = self.capture(self.target(other_base, other_head))
                entry = snapshot['entries'][0]
                self.assertEqual((entry['status'], entry['old_mode'], entry['new_mode']),
                                 ('T', old_mode, new_mode))
                self.assertIsNone(entry['patch'])
                self.assertEqual(module.compare_diff_snapshots(snapshot, other)['relation'], 'changed')
                self.assertNotEqual(snapshot['diff_digest'], other['diff_digest'])
                self.assertEqual(entry['binary'], {'old': hashlib.sha256(before).hexdigest(),
                                                  'new': hashlib.sha256(after).hexdigest()})

    def test_gitlink_updates_remain_textual(self):
        link_a = self.commit({'submodule.txt': ('100644', b'first commit\n')})
        link_b = self.commit({'submodule.txt': ('100644', b'second commit\n')})
        base = self.commit({'submodule': ('160000', link_a.encode('ascii'))})
        head = self.commit({'submodule': ('160000', link_b.encode('ascii'))}, base)
        entry = self.capture(self.target(base, head))['entries'][0]
        self.assertEqual(entry['status'], 'M')
        self.assertIsNone(entry['binary'])
        self.assertIn('-Subproject commit ' + link_a, entry['patch'])
        self.assertIn('+Subproject commit ' + link_b, entry['patch'])

    def test_observation_reports_regular_to_symlink_as_changed(self):
        base = self.commit({'a-link': ('100644', b'original\n')})
        head = self.commit({'a-link': ('100644', b'target/path')}, base)
        symlink_head = self.commit({'a-link': ('120000', b'target/path')}, base)
        frozen = self.capture(self.target(base, head))
        current = self.target(base, symlink_head)
        comparison = {'base_commit': {'sha': base}, 'merge_base_commit': {'sha': base}}
        with patch.object(module, 'github_api', return_value=comparison), \
             patch.object(module, 'read_pull_target', return_value=copy.deepcopy(current)):
            observed = module.observe_pull_target(frozen, object_repo=self.repo, cache_root=self.cache)
        self.assertEqual(observed['relation'], 'changed')
        self.assertIsNone(observed['diagnostic'])
        self.assertEqual(observed['snapshot']['target']['head_sha'], symlink_head)
        self.assertEqual(observed['snapshot']['entries'][0]['status'], 'T')
        self.assertEqual(observed['snapshot']['entries'][0]['new_mode'], '120000')
        self.assertNotEqual(observed['snapshot']['diff_digest'], frozen['diff_digest'])

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
            incomplete_repo = self.repo.parent / 'incomplete'
            module._git(self.repo.parent, 'init', '--bare', str(incomplete_repo))
            module._git(incomplete_repo, 'fetch', str(self.repo), base)
            recovered = module.capture_pull_diff(target, object_repo=incomplete_repo,
                                                 cache_root=self.cache)
            self.assertEqual(recovered, snapshot)
            corrupt = True
            with self.assertRaises(ValueError) as failure:
                module.capture_pull_diff(target, cache_root=self.cache)
            self.assertIsInstance(failure.exception, module.TargetError)


if __name__ == '__main__':
    unittest.main()
