"""Immutable PR source observations; actual diff identity is not freshness authority."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from memory_github import GitHubError, github_api, read_pull_target


class TargetError(ValueError):
    pass


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()


def _oid(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{40}|[a-f0-9]{64}', value):
        raise TargetError('A full immutable Git object identity is required')
    return value


def _git(repo, *args, data=None):
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
               GIT_ATTR_NOSYSTEM='1', GIT_TERMINAL_PROMPT='0')
    command = ['git', '-C', str(repo), '-c', 'core.hooksPath=' + os.devnull,
               '-c', 'core.fsmonitor=false', '-c', 'core.attributesFile=' + os.devnull,
               '-c', 'core.quotePath=false', *args]
    try:
        result = subprocess.run(command, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=env, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TargetError('Immutable Git object read failed') from exc
    if result.returncode:
        raise TargetError('Immutable Git operation failed')
    return result.stdout


def _hash_object(kind, data, oid):
    algorithm = hashlib.sha1 if len(oid) == 40 else hashlib.sha256
    actual = algorithm(kind.encode() + b' ' + str(len(data)).encode() + b'\0' + data).hexdigest()
    if actual != oid:
        raise TargetError('Git object content does not match its identity')


def _safe_component(name):
    if not isinstance(name, str) or not name or name in {'.', '..', '.git'} or '/' in name or '\\' in name or any(ord(c) < 32 for c in name):
        raise TargetError('Unsupported or unsafe Git tree path')
    return name


class _Objects:
    def __init__(self, repository, cache, object_repo=None):
        self.repository, self.cache, self.local = repository, cache, object_repo
        self.trees = {}
        self.blobs = {}

    def _api(self, kind, oid):
        value = github_api(self.repository['host'], f"repos/{self.repository['name']}/git/{kind}/{oid}")
        if not isinstance(value, dict) or value.get('sha') != oid:
            raise TargetError('GitHub returned a foreign immutable object')
        return value

    def commit_tree(self, oid):
        _oid(oid)
        if self.local is not None:
            raw = _git(self.local, 'cat-file', 'commit', oid)
            _hash_object('commit', raw, oid)
            first = raw.split(b'\n', 1)[0]
            if not first.startswith(b'tree '):
                raise TargetError('Malformed Git commit')
            return _oid(first[5:].decode('ascii'))
        return _oid(self._api('commits', oid).get('tree', {}).get('sha'))

    def _write(self, kind, oid, raw):
        _hash_object(kind, raw, oid)
        actual = _git(self.cache, 'hash-object', '-w', '-t', kind, '--stdin', data=raw).decode().strip()
        if actual != oid:
            raise TargetError('Private object repository changed Git identity')

    def tree(self, oid):
        if oid in self.trees:
            return self.trees[oid]
        _oid(oid)
        if self.local is not None:
            raw = _git(self.local, 'cat-file', 'tree', oid)
        else:
            value = self._api('trees', oid)
            if value.get('truncated') is not False or not isinstance(value.get('tree'), list):
                raise TargetError('Complete immutable tree is unavailable')
            rows = []
            seen = set()
            for entry in value['tree']:
                if not isinstance(entry, dict):
                    raise TargetError('Malformed tree entry')
                name = _safe_component(entry.get('path'))
                mode = entry.get('mode')
                if mode not in {'040000', '100644', '100755', '120000', '160000'}:
                    raise TargetError('Unsupported Git mode')
                kind = 'tree' if mode == '040000' else 'commit' if mode == '160000' else 'blob'
                if entry.get('type') != kind or name in seen:
                    raise TargetError('Malformed or duplicate Git tree entry')
                seen.add(name)
                object_id = _oid(entry.get('sha'))
                rows.append((name.encode() + (b'/' if kind == 'tree' else b''),
                             mode.lstrip('0').encode() + b' ' + name.encode() + b'\0' + bytes.fromhex(object_id)))
            raw = b''.join(row for _, row in sorted(rows))
        self._write('tree', oid, raw)
        entries, position = {}, 0
        width = len(oid) // 2
        while position < len(raw):
            space = raw.index(b' ', position)
            end = raw.index(b'\0', space)
            mode = raw[position:space].decode('ascii').zfill(6)
            name = _safe_component(raw[space + 1:end].decode('utf-8'))
            object_bytes = raw[end + 1:end + 1 + width]
            if len(object_bytes) != width or mode not in {'040000', '100644', '100755', '120000', '160000'} or name in entries:
                raise TargetError('Malformed Git tree object')
            entries[name] = (mode, object_bytes.hex())
            position = end + 1 + width
        self.trees[oid] = entries
        return entries

    def blob(self, oid):
        if oid in self.blobs:
            return self.blobs[oid]
        _oid(oid)
        if self.local is not None:
            raw = _git(self.local, 'cat-file', 'blob', oid)
        else:
            value = self._api('blobs', oid)
            if value.get('encoding') != 'base64' or not isinstance(value.get('content'), str):
                raise TargetError('Complete immutable blob is unavailable')
            try:
                raw = base64.b64decode(''.join(value['content'].split()), validate=True)
            except ValueError as exc:
                raise TargetError('Invalid immutable blob encoding') from exc
            if type(value.get('size')) is not int or value['size'] != len(raw):
                raise TargetError('Truncated immutable blob')
        self._write('blob', oid, raw)
        self.blobs[oid] = raw
        return raw

    def ensure(self, entry):
        if entry is None:
            return
        mode, oid = entry
        if mode == '040000':
            for child in self.tree(oid).values():
                self.ensure(child)
        elif mode != '160000':
            self.blob(oid)


def canonical_diff(entries):
    if not isinstance(entries, list):
        raise TargetError('Complete diff entries must be an array')
    required = {'status', 'old_path', 'new_path', 'old_mode', 'new_mode', 'patch', 'binary'}
    result = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != required or entry['status'] not in {'A', 'D', 'M', 'R', 'C', 'T'}:
            raise TargetError('Malformed canonical diff entry')
        for key in ('old_path', 'new_path'):
            path = entry[key]
            if path is not None:
                if not isinstance(path, str) or path.startswith('/'):
                    raise TargetError('Unsafe canonical diff path')
                for component in path.split('/'):
                    _safe_component(component)
        identity = (entry['old_path'], entry['new_path'])
        if identity == (None, None) or identity in seen:
            raise TargetError('Missing or duplicate canonical diff path')
        seen.add(identity)
        for key in ('old_mode', 'new_mode'):
            if entry[key] not in {'000000', '100644', '100755', '120000', '160000'}:
                raise TargetError('Invalid canonical diff mode')
        if entry['binary'] is None:
            if not isinstance(entry['patch'], str):
                raise TargetError('Missing complete textual patch')
        else:
            binary = entry['binary']
            if entry['patch'] is not None or not isinstance(binary, dict) or set(binary) != {'old', 'new'}:
                raise TargetError('Invalid binary content identity')
            if any(value is not None and (not isinstance(value, str) or not re.fullmatch('[a-f0-9]{64}', value)) for value in binary.values()):
                raise TargetError('Invalid binary content digest')
        result.append(dict(entry))
    result.sort(key=lambda item: (item['new_path'] or item['old_path'], item['old_path'] or ''))
    return {'algorithm': 'git-myers-u3-renames50-copies50-v1', 'entries': result}


def _snapshot_identity(snapshot):
    if not isinstance(snapshot, dict) or snapshot.get('schema_version') != 1:
        raise TargetError('Invalid target snapshot')
    canonical = canonical_diff(snapshot['entries'])
    if _digest(canonical) != snapshot.get('diff_digest'):
        raise TargetError('Snapshot diff digest changed')
    if _digest({key: value for key, value in snapshot.items() if key != 'snapshot_digest'}) != snapshot.get('snapshot_digest'):
        raise TargetError('Complete source snapshot changed')
    return canonical


def compare_diff_snapshots(frozen, current):
    try:
        if frozen['repository'] != current['repository'] or frozen['head_repository'] != current['head_repository'] or frozen['target']['number'] != current['target']['number']:
            return {'relation': 'integrity_failure', 'metadata_equal': False, 'snapshot': current,
                    'diagnostic': 'Repository or PR identity changed'}
        a, b = _snapshot_identity(frozen), _snapshot_identity(current)
        return {'relation': 'same' if a == b else 'changed',
                'metadata_equal': frozen['target'] == current['target'], 'snapshot': current, 'diagnostic': None}
    except (KeyError, TypeError, ValueError) as exc:
        return {'relation': 'unknown', 'metadata_equal': None, 'snapshot': None, 'diagnostic': str(exc)}


def _binary(data):
    if b'\0' in data:
        return True
    try:
        data.decode('utf-8')
    except UnicodeDecodeError:
        return True
    return False


def _patch_path(path):
    """Match Git's C-style path quoting with core.quotePath=false."""
    raw = path.encode('utf-8')
    if b'"' in raw or b'\x7f' in raw:
        return b'"' + raw.replace(b'"', b'\\"').replace(b'\x7f', b'\\177') + b'"'
    return raw


def capture_pull_diff(target, *, object_repo=None, cache_root):
    """Capture the complete pinned comparison; no user checkout or fetch occurs."""
    repository, pr = target['repository'], target['target']
    base, head = _oid(pr['base_sha']), _oid(pr['head_sha'])
    comparison = github_api(repository['host'], f"repos/{repository['name']}/compare/{base}...{head}")
    if not isinstance(comparison, dict) or comparison.get('base_commit', {}).get('sha') != base:
        raise TargetError('Foreign or incomplete comparison ancestor')
    ancestor = _oid(comparison.get('merge_base_commit', {}).get('sha'))
    root = Path(cache_root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise TargetError('Diff cache must be a private owned directory')
    with tempfile.TemporaryDirectory(prefix='objects-', dir=root) as directory:
        cache = Path(directory)
        args = ['init', '--bare', '-q']
        if len(head) == 64:
            args.append('--object-format=sha256')
        _git(cache, *args)
        left = _Objects(repository, cache, object_repo)
        right = _Objects(target['head_repository'], cache, object_repo)
        old_tree, new_tree = left.commit_tree(ancestor), right.commit_tree(head)
        # Two sources may be different fork namespaces; share a verified object cache,
        # but always read changed blobs from their originating repository.
        def populate(old, new):
            a, b = left.tree(old) if old else {}, right.tree(new) if new else {}
            for name in a.keys() | b.keys():
                x, y = a.get(name), b.get(name)
                if x == y:
                    continue
                if x and y and x[0] == y[0] == '040000':
                    populate(x[1], y[1])
                else:
                    left.ensure(x)
                    right.ensure(y)
        populate(old_tree, new_tree)
        options = ['--no-commit-id', '-r', '--find-renames=50%', '--find-copies=50%',
                   '--no-ext-diff', '--no-textconv', '--diff-algorithm=myers', '--no-indent-heuristic']
        raw = _git(cache, 'diff-tree', *options, '--raw', '-z', old_tree, new_tree).split(b'\0')
        patch_bytes = _git(cache, 'diff-tree', *options, '--patch', '--binary', '--full-index', '--unified=3', '--no-color', old_tree, new_tree)
        sections = re.split(br'(?m)^diff --git ', patch_bytes)
        if sections[0]:
            raise TargetError('Unexpected data before Git patch')
        blocks = sections[1:]
        entries, position, patch_position = [], 0, 0
        while position < len(raw) and raw[position]:
            fields = raw[position].decode('ascii').split()
            if len(fields) != 5 or not fields[0].startswith(':'):
                raise TargetError('Malformed complete Git diff inventory')
            old_mode, new_mode, old_oid, new_oid, status = fields
            old_mode = old_mode[1:]
            position += 1
            first = raw[position].decode('utf-8'); position += 1
            if status[0] in {'R', 'C'}:
                second = raw[position].decode('utf-8'); position += 1
            else:
                second = first
            old_path = None if status[0] == 'A' else first
            new_path = None if status[0] == 'D' else second
            count = 2 if status[0] == 'T' else 1
            if patch_position + count > len(blocks):
                raise TargetError('Git patch is incomplete')
            selected = blocks[patch_position:patch_position + count]
            header = (_patch_path('a/' + (old_path or new_path)) + b' ' +
                      _patch_path('b/' + (new_path or old_path)))
            for index, block in enumerate(selected):
                if block.split(b'\n', 1)[0] != header:
                    raise TargetError('Git patch/inventory path mismatch')
                if status[0] == 'T':
                    mode = ('deleted file mode ' + old_mode if index == 0 else
                            'new file mode ' + new_mode).encode('ascii')
                    if not block.startswith(header + b'\n' + mode + b'\n'):
                        raise TargetError('Git type-change patch mode mismatch')
            patch_position += count
            # Gitlinks are commit identities, not blobs, including in binary type changes.
            before = (b'' if old_mode == '000000' else old_oid.encode('ascii')
                      if old_mode == '160000' else left.blob(old_oid))
            after = (b'' if new_mode == '000000' else new_oid.encode('ascii')
                     if new_mode == '160000' else right.blob(new_oid))
            is_binary = _binary(before) or _binary(after)
            binary = {'old': hashlib.sha256(before).hexdigest() if old_path else None,
                      'new': hashlib.sha256(after).hexdigest() if new_path else None} if is_binary else None
            body = None if is_binary else ''.join(
                '\n'.join(line for line in block.decode('utf-8').split('\n')[1:]
                          if not line.startswith('index ') and not line.startswith('similarity index '))
                for block in selected)
            entries.append({'status': status[0], 'old_path': old_path, 'new_path': new_path,
                            'old_mode': old_mode, 'new_mode': new_mode, 'patch': body, 'binary': binary})
        if patch_position != len(blocks):
            raise TargetError('Git patch/inventory coverage mismatch')
        canonical = canonical_diff(entries)
        closing = read_pull_target(pr['url'])
        if closing != target:
            raise TargetError('PR metadata changed during immutable diff capture')
        try:
            diff, encoding = patch_bytes.decode('utf-8'), 'utf-8'
        except UnicodeDecodeError:
            # Latin-1 is a reversible byte projection, not a guessed source codec.
            diff, encoding = patch_bytes.decode('latin-1'), 'latin-1'
        result = {**target, 'entries': canonical['entries'], 'diff_digest': _digest(canonical),
                  'diff': diff, 'diff_encoding': encoding,
                  'comparison_sha': ancestor, 'old_tree': old_tree, 'new_tree': new_tree,
                  'source_access': {'object_repo': str(object_repo) if object_repo else None,
                                    'head_repository': target['head_repository'], 'head_sha': head,
                                    'head_tree': new_tree, 'read_only': True}}
        result['snapshot_digest'] = _digest(result)
        return result


def observe_pull_target(frozen, *, object_repo=None, cache_root):
    try:
        _snapshot_identity(frozen)
        operation = 'read_pull_target'
        current = read_pull_target(frozen['target']['url'])
        if current['repository'] != frozen['repository'] or current['head_repository'] != frozen['head_repository']:
            return {'relation': 'integrity_failure', 'metadata_equal': False, 'snapshot': None,
                    'diagnostic': 'Source repository identity changed'}
        fields = ('head_sha', 'base_sha')
        if all(current['target'][key] == frozen['target'][key] for key in fields):
            snapshot = {**frozen, **current}
            snapshot['snapshot_digest'] = _digest({key: value for key, value in snapshot.items() if key != 'snapshot_digest'})
            return compare_diff_snapshots(frozen, snapshot)
        operation = 'capture_pull_diff'
        snapshot = capture_pull_diff(current, object_repo=object_repo, cache_root=cache_root)
        return compare_diff_snapshots(frozen, snapshot)
    except GitHubError as exc:
        return {'relation': 'unknown', 'metadata_equal': None, 'snapshot': None,
                'diagnostic': ('GitHub request failed during ' + operation),
                'github_error': {'operation': operation, 'exit_code': exc.exit_code,
                                 'http_status': exc.http_status, 'category': exc.category}}
    except Exception as exc:
        return {'relation': 'unknown', 'metadata_equal': None, 'snapshot': None,
                'diagnostic': f'{type(exc).__name__}: {exc}'}
