"""One-time, explicit migration from the legacy npm runtime to native Claude Code.

This command is never called by app deployment. Run only during an authorized
runtime migration; it downloads/canaries without sending a model turn.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIVE_ROOT = Path(os.environ.get('HAYAGARDEN_LIVE_ROOT', '/opt/frontend'))
SYSTEMD_DIR = Path('/etc/systemd/system')
SERVICE_NAME = 'hayagarden-claude-runtime-update.service'
TIMER_NAME = 'hayagarden-claude-runtime-update.timer'


def _install_systemd_file(source: Path, target: Path) -> None:
    payload = source.read_bytes()
    if target.exists():
        if target.read_bytes() != payload:
            raise RuntimeError('existing Claude runtime unit differs; refusing overwrite')
        return
    fd, temp_name = tempfile.mkstemp(prefix='.%s.' % target.name, dir=str(target.parent))
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
        os.chown(target, 0, 0)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _ensure_timer() -> None:
    _install_systemd_file(
        ROOT / 'deploy' / 'systemd' / SERVICE_NAME,
        SYSTEMD_DIR / SERVICE_NAME,
    )
    _install_systemd_file(
        ROOT / 'deploy' / 'systemd' / TIMER_NAME,
        SYSTEMD_DIR / TIMER_NAME,
    )
    subprocess.run(['systemctl', 'daemon-reload'], check=True, timeout=60)
    subprocess.run(['systemctl', 'enable', '--now', TIMER_NAME], check=True, timeout=60)


def _legacy_runtime_version() -> str:
    package = LIVE_ROOT / '.claude-runtime' / 'node_modules' / '@anthropic-ai' / 'claude-code' / 'package.json'
    try:
        raw = json.loads(package.read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        return 'unavailable'
    return str(raw.get('version') or 'unknown')


def main() -> int:
    if os.geteuid() != 0:
        print('CLAUDE_RUNTIME_BOOTSTRAP_REFUSED=requires_root')
        return 2
    from chat.cc_runtime import (
        MINIMUM_CLAUDE_CODE_VERSION,
        NATIVE_VERSIONS_RELATIVE,
        active_claude_binary,
        active_claude_version,
        service_home,
        version_tuple,
        probe_claude_version,
    )
    from chat.claude_runtime_state import (
        atomic_write,
        read_version,
        runtime_state_dir,
        utc_now_iso,
        write_update_state,
        write_version,
    )
    from tools.claude_runtime_updater import (
        canary_candidate,
        sync_native_update_settings,
        update_locks,
    )

    with update_locks() as acquired:
        if not acquired:
            print('CLAUDE_RUNTIME_BOOTSTRAP_REFUSED=lock_busy')
            return 3
        legacy_version = _legacy_runtime_version()
        active = read_version('active-version')
        if active:
            try:
                if version_tuple(active) < version_tuple(MINIMUM_CLAUDE_CODE_VERSION):
                    raise RuntimeError('active runtime below minimum')
                if probe_claude_version(active_claude_binary()) != active:
                    raise RuntimeError('active runtime mismatch')
            except Exception:
                print('CLAUDE_RUNTIME_BOOTSTRAP_REFUSED=existing_active_unhealthy')
                return 4
            version = active
        else:
            home = service_home()
            env = os.environ.copy()
            env['HOME'] = str(home)
            # Official installer owns acquisition; HayaGarden does not scrape
            # npm/releases or overwrite the retained legacy .claude-runtime.
            installer = subprocess.run(
                ['bash', '-o', 'pipefail', '-c', 'curl -fsSL https://claude.ai/install.sh | bash'],
                cwd=str(ROOT),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=1800,
                check=False,
            )
            if installer.returncode != 0:
                print('CLAUDE_RUNTIME_BOOTSTRAP_REFUSED=native_install_failed')
                return 5
            versions = home / NATIVE_VERSIONS_RELATIVE
            candidates = []
            try:
                paths = list(versions.iterdir())
            except OSError:
                paths = []
            for path in paths:
                try:
                    candidate_tuple = version_tuple(path.name)
                    if candidate_tuple < version_tuple(MINIMUM_CLAUDE_CODE_VERSION):
                        continue
                    actual = probe_claude_version(path, env=env, timeout=30.0)
                    if actual == path.name and path.is_file() and os.access(str(path), os.X_OK):
                        candidates.append((candidate_tuple, path.name))
                except Exception:
                    continue
            if not candidates:
                print('CLAUDE_RUNTIME_BOOTSTRAP_REFUSED=no_supported_native_version')
                return 6
            version = max(candidates)[1]
            if not canary_candidate(version, env=env):
                print('CLAUDE_RUNTIME_BOOTSTRAP_REFUSED=initial_canary_failed')
                return 7
            checked_at = utc_now_iso()
            # First migration has no prior active native version. Preserve the
            # 2.1.220 npm install as rollback evidence; do not remove or edit it.
            state_dir = runtime_state_dir()
            state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(state_dir, 0o700)
            write_version('last-good-version', version)
            write_version('candidate-version', version)
            write_update_state({
                'status': 'promoting',
                'from': None,
                'to': version,
                'channel': 'latest',
                'checked_at': checked_at,
                'last_check_at': checked_at,
                'canary': 'pass',
            })
            write_version('active-version', version)
            write_version('candidate-version', None)
            write_update_state({
                'status': 'healthy',
                'from': None,
                'to': version,
                'channel': 'latest',
                'checked_at': checked_at,
                'promoted_at': checked_at,
                'last_check_at': checked_at,
                'last_promoted_at': checked_at,
                'canary': 'pass',
                'last_error': None,
            })
        sync_native_update_settings(enabled=True, channel='latest')
        _ensure_timer()
        print(
            'CLAUDE_RUNTIME_BOOTSTRAP_OK source=native active=%s minimum=%s '
            'channel=latest auto_update=true legacy_npm_version=%s '
            'model_generation_requests=0'
            % (version, MINIMUM_CLAUDE_CODE_VERSION, legacy_version)
        )
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
