# Context Window v0.2 — Nightly Forge Compatibility Canary (R0)

**Baseline:** `bbbbbb0939e1cebc5a81c341c626ded0da000ad2`

Owner-triggered one-shot. No cron. No flag-on. No Capacity Swap / warmth.

## CLI

```bash
python3 tools/context_window_admin.py nightly-forge-canary --confirm-live
python3 tools/context_window_admin.py nightly-forge-canary --confirm-live --login
```

## Flow

1. Temp root under `/tmp` (outside repo and `/opt/frontend`)
2. `HOME=<temp>/fake-home`, `CLAUDE_CONFIG_DIR=<temp>/claude-home`
3. Clear API / OAuth / Bedrock / Vertex overlays
4. Auth preflight (+ optional official `--login`)
5. Pin check: `@anthropic-ai/claude-code@2.1.220`
6. Create isolated native session
7. Formal `transform_transcript` + `validate_transcript_events`
8. Write forged JSONL only under temp Claude home
9. `--resume` forged session with fixed synthetic user
10. Require text delta + success result + JSONL prefix unchanged + growth + append user/assistant
11. Cleanup deletes entire temp root

## Reader uuid-less metadata note (R2/R3)

Formal `chat/claude_transcript_reader.py` ignores a narrow allowlist of Claude
Code raw bookkeeping rows that omit `uuid`. The allowlist is aligned with
types already classified as `EventRole.META` by `_classify_event`
(`queue-operation`, `last-prompt`, `result`, `file-history-snapshot`) plus
live-gate extras (`agent-name`, `custom-title`, `progress`),
`system/turn_duration`, and assistant usage observation. Conversational /
unknown uuid-less objects still raise `READER_MISSING_UUID`. Canary must not
pre-filter metadata before the formal Reader.

## Flag

Keep `DAILY_SOFT_WINDOW_ENABLED` unset/off. FAIL/BLOCKED keeps flag-off and blocks owner-only enable. No auto flag-off writer.
