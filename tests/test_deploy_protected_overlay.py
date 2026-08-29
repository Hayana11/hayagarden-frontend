from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "deploy-protected-overlay.sh"
DEPLOY = ROOT / "scripts" / "deploy-frontend.sh"
A = "artifacts/treegpt-cache-probe-baseline.json"
B = "artifacts/treegpt-cache-probe-live.json"


class OverlayBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.repo = Path(self.tempdir.name)
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "overlay-test")
        (self.repo / "artifacts").mkdir()
        (self.repo / A).write_text("baseline\n", encoding="utf-8")
        (self.repo / B).write_text("live\n", encoding="utf-8")
        (self.repo / "tracked.txt").write_text("clean\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD").stdout.strip()

    def tearDown(self):
        self.tempdir.cleanup()

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.repo), *args],
            check=True,
            text=True,
            capture_output=True,
        )

    def helper(self, function, *args, env=None):
        command = 'source "$1"; shift; fn="$2"; shift 2; "$fn" "$@"'
        return subprocess.run(
            ["bash", "-c", command, "helper-test", str(HELPER), str(self.repo), function, *args],
            text=True,
            capture_output=True,
            env=env,
        )

    def remove_overlay(self):
        (self.repo / A).unlink()
        (self.repo / B).unlink()

    def empty_target(self):
        self.git("commit", "--allow-empty", "-qm", "target")
        return self.git("rev-parse", "HEAD").stdout.strip()

    def test_accepted_exact_unstaged_deletions(self):
        self.remove_overlay()
        result = self.helper("protected_overlay_validate_current", self.repo, self.base)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_deletion_fails(self):
        (self.repo / A).unlink()
        result = self.helper("protected_overlay_validate_current", self.repo, self.base)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PROTECTED_OVERLAY_STATE_MISMATCH", result.stderr)

    def test_staged_deletion_fails(self):
        self.git("rm", "-q", A)
        (self.repo / B).unlink()
        result = self.helper("protected_overlay_validate_current", self.repo, self.base)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PROTECTED_OVERLAY_STATE_MISMATCH", result.stderr)

    def test_target_blob_unchanged_passes(self):
        self.remove_overlay()
        target = self.empty_target()
        result = self.helper("protected_overlay_validate_target", self.repo, self.base, target)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_target_blob_modified_fails(self):
        self.git("checkout", "-q", "--detach", self.base)
        (self.repo / A).write_text("changed\n", encoding="utf-8")
        self.git("add", A)
        self.git("commit", "-qm", "changed target")
        target = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("checkout", "-q", "--detach", self.base)
        self.remove_overlay()
        result = self.helper("protected_overlay_validate_target", self.repo, self.base, target)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PROTECTED_OVERLAY_TARGET_CHANGED", result.stderr)

    def test_target_artifact_removed_fails(self):
        self.git("checkout", "-q", "--detach", self.base)
        (self.repo / A).unlink()
        self.git("add", A)
        self.git("commit", "-qm", "removed target")
        target = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("checkout", "-q", "--detach", self.base)
        self.remove_overlay()
        result = self.helper("protected_overlay_validate_target", self.repo, self.base, target)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PROTECTED_OVERLAY_TARGET_CHANGED", result.stderr)

    def test_apply_after_checkout_and_exact_postcheck(self):
        self.remove_overlay()
        target = self.empty_target()
        self.git("checkout", "-q", "--detach", target)
        applied = self.helper("protected_overlay_apply", self.repo)
        self.assertEqual(applied.returncode, 0, applied.stderr)
        verified = self.helper("protected_overlay_verify", self.repo)
        self.assertEqual(verified.returncode, 0, verified.stderr)

    def test_exact_postcheck_rejects_third_tracked_diff(self):
        self.remove_overlay()
        target = self.empty_target()
        self.git("checkout", "-q", "--detach", target)
        self.assertEqual(self.helper("protected_overlay_apply", self.repo).returncode, 0)
        (self.repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")
        verified = self.helper("protected_overlay_verify", self.repo)
        self.assertNotEqual(verified.returncode, 0)
        self.assertIn("PROTECTED_OVERLAY_POSTCHECK_FAILED", verified.stderr)

    def test_rollback_reapplies_overlay(self):
        target = self.empty_target()
        self.git("checkout", "-q", "--detach", self.base)
        self.remove_overlay()
        self.git("checkout", "-q", "--detach", target)
        self.assertEqual(self.helper("protected_overlay_apply", self.repo).returncode, 0)
        self.git("checkout", "-q", "--detach", self.base)
        self.assertEqual(self.helper("protected_overlay_apply", self.repo).returncode, 0)
        verified = self.helper("protected_overlay_verify", self.repo)
        self.assertEqual(verified.returncode, 0, verified.stderr)

    def test_manifest_records_absent_runtime_baseline_without_creating_log(self):
        self.remove_overlay()
        target = self.empty_target()
        manifest = self.repo / "manifest.txt"
        result = self.helper("protected_overlay_write_manifest", self.repo, self.base, target, manifest)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.repo / "client_errors.log.1").exists())
        text = manifest.read_text(encoding="utf-8")
        self.assertIn("state=ABSENT", text)
        self.assertIn("classification=NORMAL_RUNTIME_DRIFT", text)

    def test_no_arbitrary_paths_or_environment_allowlist(self):
        target = self.empty_target()
        self.git("checkout", "-q", "--detach", target)
        extra = self.repo / "third.txt"
        extra.write_text("third\n", encoding="utf-8")
        env = os.environ.copy()
        env["PROTECTED_OVERLAY_PATH_A"] = "third.txt"
        result = self.helper("protected_overlay_apply", self.repo, extra, env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(extra.exists())
        self.assertTrue((self.repo / A).exists())
        self.assertTrue((self.repo / B).exists())

    def test_rotated_runtime_logs_snapshot_clear_restore(self):
        backup = self.repo / "runtime-backup"
        (self.repo / "client_errors.log").write_bytes(b"current")
        (self.repo / "client_errors.log.1").write_bytes(b"one")
        (self.repo / "client_errors.log.2.gz").write_bytes(b"two")
        os.chmod(self.repo / "client_errors.log.1", 0o640)
        os.chmod(self.repo / "client_errors.log.2.gz", 0o600)
        command = f'''
set -Eeuo pipefail
ROOT={shlex.quote(str(self.repo))}
runtime_backup={shlex.quote(str(backup))}
fail() {{ printf '%s\\n' "$*" >&2; return 1; }}
{self.runtime_functions()}
mkdir -p "$runtime_backup/static" "$runtime_backup/app"
snapshot_runtime
clear_runtime_for_checkout
test ! -e "$ROOT/client_errors.log"
test ! -e "$ROOT/client_errors.log.1"
test ! -e "$ROOT/client_errors.log.2.gz"
restore_runtime
cmp "$ROOT/client_errors.log" "$runtime_backup/client_errors.log"
cmp "$ROOT/client_errors.log.1" "$runtime_backup/client_errors.log.1"
cmp "$ROOT/client_errors.log.2.gz" "$runtime_backup/client_errors.log.2.gz"
test "$(stat -c %a "$ROOT/client_errors.log.1")" = 640
test "$(stat -c %a "$ROOT/client_errors.log.2.gz")" = 600
'''
        result = subprocess.run(["bash", "-c", command], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_absent_rotated_log_remains_absent(self):
        backup = self.repo / "runtime-backup"
        command = f'''
set -Eeuo pipefail
ROOT={shlex.quote(str(self.repo))}
runtime_backup={shlex.quote(str(backup))}
fail() {{ printf '%s\\n' "$*" >&2; return 1; }}
{self.runtime_functions()}
mkdir -p "$runtime_backup/static" "$runtime_backup/app"
snapshot_runtime
clear_runtime_for_checkout
restore_runtime
test ! -e "$ROOT/client_errors.log.1"
'''
        result = subprocess.run(["bash", "-c", command], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unrelated_untracked_file_is_not_excluded(self):
        self.remove_overlay()
        (self.repo / "random-debug.txt").write_text("unexpected\n", encoding="utf-8")
        command = [
            "git", "-C", str(self.repo), "status", "--porcelain", "--untracked-files=all", "--",
            ".",
            ":(exclude)artifacts/treegpt-cache-probe-baseline.json",
            ":(exclude)artifacts/treegpt-cache-probe-live.json",
            ":(exclude)client_errors.log",
            ":(exclude)client_errors.log.[0-9]*",
        ]
        result = subprocess.run(command, text=True, capture_output=True, check=True)
        self.assertIn("random-debug.txt", result.stdout)

    def runtime_functions(self):
        source = DEPLOY.read_text(encoding="utf-8")
        start = source.index("snapshot_runtime() {")
        end = source.index('bash "$ROOT/tools/backup.sh"', start)
        return source[start:end]


if __name__ == "__main__":
    unittest.main()