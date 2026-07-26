# Persona v2 recovery evidence (2026-07-26 UTC)

## Comparison result

Production `prompts/persona.md` at HEAD `30d375b` (blob `badf05a`) does **not** match the
user-supplied canonical persona v2 text.

| Artifact | git blob / hash | lines | bytes |
|----------|-----------------|------:|------:|
| `main@605e697` baseline | `12c85a4` | 393 | 43033 |
| Production `aed5b2e` / `30d375b` | `badf05a` | 166 | 23371 |
| **Canonical v2 (this PR)** | `992560d` | 120 | 31542 |

## Production-only commit chain

```
605e697  GitHub main
  └─ 41ff653  auto backup 2026-07-24  (empty)
      └─ aed5b2e  auto backup 2026-07-25  (persona.md → badf05a)
          └─ 30d375b  auto backup 2026-07-26  (empty, production HEAD)
```

## Files in this directory

- `persona-main-605e697.md` — GitHub baseline
- `persona-production-badf05a.md` — production blob before canonical v2 PR
- `persona-target-v2-canonical.md` — exact text approved for GitHub recovery
- `persona-diff-605e697-aed5b2e.patch` — historical production-only diff
- `production-state.txt` — read-only VPS snapshot
- `crontab-backup-20260726.txt` — crontab before disabling auto git commit

## Cron change (VPS, 2026-07-26)

Disabled:

```cron
5 4 * * * cd /opt/frontend && git add . && git commit ... && git push origin main
```

Replaced with read-only inline drift logging to `/var/log/production-git-drift.log`.
Full script `scripts/check-production-git-drift.sh` ships in this PR for post-deploy cron.

## PR scope

- `prompts/persona.md` → canonical v2 (`992560d`)
- `deploy/recovered-production-shas.txt` → acknowledge `41ff653`, `aed5b2e`, `30d375b`
- Evidence + drift-check script only
- **No** PR #134 / #135 logic

## Post-merge deploy note

After merge, deploy must verify:

```bash
git hash-object prompts/persona.md  # expect 992560d...
```

Production will align to new `main` SHA; persona content must equal canonical v2, not `badf05a`.
