# Gallery R1 production recovery acknowledgement

This record acknowledges the exact production recovery HEAD so the deployment ancestry guard can recognize it. It changes deployment provenance only and introduces no product behavior.

AUDIT_DATE: 2026-09-26
ACK_PREPARED_DATE: 2026-09-27
PRODUCTION_PRE_RECOVERY_HEAD: 7ab8d0e9c42715484c2c5d5865ca69496e22c0c8
PRODUCTION_RECOVERY_HEAD: 98de3a7f0d71e16954405307ea0cdf914b13e347
DEPLOYED_SHA: 7ab8d0e9c42715484c2c5d5865ca69496e22c0c8
TARGET_MAIN_AT_ACK_BASE: c5b1b12c3814563ea9648d21c18a85b69b3edfc2
RECOVERY_BRANCH: recovery/prod-dirty-before-gallery-r1-20260926-vps
RECOVERY_TREE: dc69626ed9295f6aaad7935f285caf5abdde6171
PREVIOUS_AUDITED_TREE_EQUIVALENT_COMMIT: 69ce6702d3386e0cd7421a0818541e3d43adef52

## Recovered paths

1. app.py
2. app/package.json
3. app/scripts/test-settings-effort.mjs
4. app/src/index.css
5. app/src/lib/groupChat.ts
6. app/src/lib/systemConfig.ts
7. app/src/screens/SettingsScreen.tsx
8. codex_app_server.py
9. config_store.py
10. tests/test_cc_effort.py
11. tests/test_codex_app_server.py

The localized VPS commit 98de3a7f0d71e16954405307ea0cdf914b13e347 has the same tree as the previously audited recovery commit 69ce6702d3386e0cd7421a0818541e3d43adef52; the tree SHA is `dc69626ed9295f6aaad7935f285caf5abdde6171`. The prior turn verified the VPS commit parent, 11 paths, exact tree identity, and published recovery branch. This PR relies on that evidence and does not repeat a review of 98de3a7f0d71e16954405307ea0cdf914b13e347.

## Blob comparison against target main

- 10 of the 11 recovered paths are blob-identical to target main at c5b1b12c3814563ea9648d21c18a85b69b3edfc2.
- `app.py` recovery blob: `64c2ba629def5a28ceed3a570a593639cd2f0c1d`.
- `app.py` at Gallery R1 pre-merge base `c9864d8f43c90bdc6f44cbc9b60f7f97fe658a8e`: `64c2ba629def5a28ceed3a570a593639cd2f0c1d`.
- `app.py` at target main: `376bc6065a6731fecd408b6cab5b2d9d2255a1a4`.
- Comparing `c9864d8f43c90bdc6f44cbc9b60f7f97fe658a8e` to `c5b1b12c3814563ea9648d21c18a85b69b3edfc2` shows the merged PR #500 Gallery R1 change; its `app.py` addition is the `visual_description` and `first_impression` API fields.
- The other ten recovered path blobs are unchanged by PR #500.
- No source path from the recovered tree is missing from target main.

## Recovery decision

- Acknowledge only the exact current production HEAD `98de3a7f0d71e16954405307ea0cdf914b13e347` in `deploy/recovered-production-shas.txt`.
- Do not merge or cherry-pick the recovery branch or commit into main.
- After this acknowledgement lands, the ancestry guard can allow a guarded deploy from the recovery HEAD to the audited main commit.
- Runtime data and the two protected TreeGPT overlay paths remain managed by the existing deployment script; this PR does not change them.
- No product behavior is introduced by this acknowledgement.

## Current worktree boundary

A read-only VPS status check on 2026-09-27 showed additional uncommitted continuity/context source and test files beyond the acknowledged commit, as well as the pre-existing overlay deletions and runtime logs. Their contents were not audited or changed here. This acknowledgement does not make the current worktree clean and does not authorize deployment; those changes must be preserved and handled separately before any deploy.

