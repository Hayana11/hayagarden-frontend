from __future__ import annotations

import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import disk_housekeeping as dh


class FakeLocks:
    def __init__(self, busy: set[Path] | None = None):
        self.busy = busy or set()
        self.released: list[Path] = []

    def status(self, path: Path) -> str:
        return 'busy' if path in self.busy else 'free'

    def acquire(self, path: Path):
        if path in self.busy:
            return None
        return path

    def release(self, handle) -> None:
        if handle is not None:
            self.released.append(handle)


def _completed(command, *, stdout='', returncode=0, stderr=''):
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


class DiskHousekeepingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.now = 2_000_000_000.0

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _old_dir(self, name: str) -> Path:
        path = self.root / name
        path.mkdir()
        (path / 'payload').write_bytes(b'x' * 32)
        os.utime(path, (self.now - 2 * 86400, self.now - 2 * 86400))
        os.utime(path / 'payload', (self.now - 2 * 86400, self.now - 2 * 86400))
        return path

    def _clean(self, *, execute: bool, **kwargs):
        lines: list[str] = []
        kwargs.setdefault('worktree_paths', [])
        kwargs.setdefault('proc_checker', lambda _path: (False, False))
        kwargs.setdefault('now', self.now)
        result = dh.clean_tmp(
            execute=execute,
            tmp_root=self.root,
            emit=lines.append,
            **kwargs,
        )
        return result, lines

    def test_default_is_dry_run(self):
        args = dh.parse_args(['--daily'])
        self.assertTrue(args.dry_run)
        self.assertEqual(args.mode, 'daily')

    def test_dry_run_does_not_delete(self):
        path = self._old_dir('forge-switch-dry-run')
        result, lines = self._clean(execute=False)
        self.assertTrue(path.exists())
        self.assertEqual(result[1:], (0, 0, []))
        self.assertTrue(any(line.startswith('WOULD_DELETE_TMP') for line in lines))

    def test_non_allowlisted_path_is_never_deleted(self):
        path = self._old_dir('haya-runtime-parity-unproven')
        result, _lines = self._clean(execute=True)
        self.assertTrue(path.exists())
        self.assertEqual(result[1], 0)

    def test_symlink_candidate_is_rejected(self):
        target = self.root / 'target'
        target.mkdir()
        link = self.root / 'forge-switch-link'
        try:
            link.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('symlink creation unavailable')
        result, lines = self._clean(execute=True)
        self.assertTrue(link.is_symlink())
        self.assertEqual(result[1], 0)
        self.assertTrue(any('symlink' in line for line in lines))

    def test_git_worktree_candidate_is_rejected(self):
        path = self._old_dir('forge-vision-live-worktree')
        lines: list[str] = []
        result = dh.clean_tmp(
            execute=True,
            tmp_root=self.root,
            worktree_paths=[path],
            proc_checker=lambda _path: (False, False),
            now=self.now,
            emit=lines.append,
        )
        self.assertTrue(path.exists())
        self.assertEqual(result[1], 0)
        self.assertTrue(any('git-worktree' in line for line in lines))

    def test_socket_candidate_is_rejected(self):
        path = self.root / 'forge-switch-socket'
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.bind(str(path))
        except (OSError, AttributeError):
            self.skipTest('Unix sockets unavailable')
        result, lines = self._clean(execute=True)
        sock.close()
        path.unlink(missing_ok=True)
        self.assertEqual(result[1], 0)
        self.assertTrue(any('special-object' in line for line in lines))

    def test_younger_than_24_hours_is_rejected(self):
        path = self.root / 'forge-switch-young'
        path.mkdir()
        os.utime(path, (self.now - 60, self.now - 60))
        result, _lines = self._clean(execute=True)
        self.assertTrue(path.exists())
        self.assertEqual(result[1], 0)

    def test_old_allowlisted_candidate_is_reported(self):
        path = self._old_dir('forge-id-old')
        result, lines = self._clean(execute=False)
        self.assertTrue(path.exists())
        self.assertEqual(result[0], 1)
        self.assertTrue(any('forge-id-old' in line for line in lines))

    def test_old_allowlisted_candidate_is_deleted_only_in_execute(self):
        path = self._old_dir('forge-id-execute')
        result, lines = self._clean(execute=True)
        self.assertFalse(path.exists())
        self.assertEqual(result[1], 1)
        self.assertTrue(any(line.startswith('DELETED_TMP') for line in lines))

    def test_private_runtime_subtree_is_rejected(self):
        path = self._old_dir('forge-post-private')
        private = path / 'systemd-private-example'
        private.mkdir()
        result, lines = self._clean(execute=True)
        self.assertTrue(path.exists())
        self.assertEqual(result[1], 0)
        self.assertTrue(any('private-runtime-path' in line for line in lines))

    def test_active_proc_reference_is_rejected(self):
        path = self._old_dir('forge-post-active')
        result, lines = self._clean(
            execute=True,
            proc_checker=lambda _path: (True, False),
        )
        self.assertTrue(path.exists())
        self.assertEqual(result[1], 0)
        self.assertTrue(any('active-proc-reference' in line for line in lines))

    def test_deploy_lock_busy_skips_execute(self):
        lines: list[str] = []
        locks = FakeLocks({dh.DEPLOY_LOCK})
        result = dh.run_housekeeping(
            'weekly',
            dry_run=False,
            lock_backend=locks,
            stats_provider=lambda: dh.DiskStats(50.0, 100, 2.0),
            emit=lines.append,
        )
        self.assertEqual(result.result, 'SKIPPED')
        self.assertIn('SKIP_ACTIVE_LOCK', lines)

    def test_housekeeping_lock_busy_skips_execute(self):
        lines: list[str] = []
        locks = FakeLocks({dh.HOUSEKEEPING_LOCK})
        result = dh.run_housekeeping(
            'weekly',
            dry_run=False,
            lock_backend=locks,
            stats_provider=lambda: dh.DiskStats(50.0, 100, 2.0),
            emit=lines.append,
        )
        self.assertEqual(result.result, 'SKIPPED')
        self.assertIn('SKIP_ACTIVE_LOCK', lines)

    def test_pip_cache_path_mismatch_fails_closed(self):
        def runner(command, **_kwargs):
            if command[:3] == ['npm', 'config', 'get']:
                return _completed(command, stdout=f'{dh.NPM_CACHE}\n')
            return _completed(command, stdout='/tmp/not-pip\n')

        lines: list[str] = []
        reclaimed, errors = dh.weekly_cache_actions(
            execute=False,
            runner=runner,
            emit=lines.append,
        )
        self.assertEqual(reclaimed, 0)
        self.assertTrue(errors)
        self.assertTrue(any('REFUSE_UNEXPECTED_NPM_CACHE_PATH' in line for line in lines))

    def test_backup_disk_alert_is_not_fatal(self):
        def runner(command, **_kwargs):
            return _completed(command, returncode=1, stderr='DISK ALERT: root filesystem >= 80%')

        reclaimed, disk_alert, errors = dh.backup_retention_action(
            execute=False,
            repo_root=self.root,
            backup_root=self.root / 'backups',
            runner=runner,
            emit=lambda _line: None,
        )
        self.assertEqual(reclaimed, 0)
        self.assertTrue(disk_alert)
        self.assertEqual(errors, [])

    def test_disk_alert_does_not_expand_cleanup(self):
        path = self._old_dir('forge-vision-alert')

        def runner(command, **_kwargs):
            return _completed(command, returncode=1, stderr='DISK ALERT: root filesystem >= 80%')

        result = dh.run_housekeeping(
            'daily',
            dry_run=True,
            tmp_root=self.root,
            worktree_paths=[],
            proc_checker=lambda _path: (False, False),
            lock_backend=FakeLocks(),
            runner=runner,
            backup_root=self.root / 'backups',
            stats_provider=lambda: dh.DiskStats(95.0, 10, 2.0),
            emit=lambda _line: None,
        )
        self.assertEqual(result.result, 'PASS_WITH_DISK_ALERT')
        self.assertEqual(result.tmp_deleted, 0)
        self.assertTrue(path.exists())

    def test_anchors_are_left_to_existing_retention(self):
        commands: list[list[str]] = []

        def runner(command, **_kwargs):
            commands.append(list(command))
            return _completed(command, stdout='backup_retention dry-run: keep 1 tar(s)')

        dh.backup_retention_action(
            execute=False,
            repo_root=self.root,
            backup_root=self.root / 'backups',
            runner=runner,
            emit=lambda _line: None,
        )
        self.assertEqual(len(commands), 1)
        self.assertIn('backup_retention.py', commands[0][1])
        self.assertNotIn('backup_anchors.txt', ' '.join(commands[0]))

    def test_output_summary_contract(self):
        npm_cache = self.root / 'npm'
        pip_cache = self.root / 'pip'
        npm_cache.mkdir()
        pip_cache.mkdir()

        def runner(command, **_kwargs):
            if command[:3] == ['npm', 'config', 'get']:
                return _completed(command, stdout=f'{npm_cache}\n')
            if command[:4] == [dh.PYTHON, '-m', 'pip', 'cache']:
                return _completed(command, stdout=f'{pip_cache}\n')
            if command[:3] == ['npm', 'cache', '--help']:
                return _completed(command, stdout='cache npx clean\n')
            return _completed(command)

        lines: list[str] = []
        result = dh.run_housekeeping(
            'weekly',
            dry_run=True,
            lock_backend=FakeLocks(),
            runner=runner,
            npm_cache_path=npm_cache,
            pip_cache_path=pip_cache,
            stats_provider=lambda: dh.DiskStats(50.0, 100, 2.0),
            emit=lines.append,
        )
        self.assertEqual(result.result, 'PASS')
        for field in ('HOUSEKEEPING_START', 'HOUSEKEEPING_END', 'tmp_candidates=',
                      'tmp_deleted=', 'cache_reclaimed_bytes=', 'result=PASS'):
            self.assertTrue(any(line.startswith(field) for line in lines), field)
        self.assertIn('WOULD_NPX_CLEAN', lines)
        self.assertIn('WOULD_NPM_VERIFY', lines)
        self.assertTrue(any(line.startswith('WOULD_PIP_PURGE') for line in lines))


if __name__ == '__main__':
    unittest.main()
