# Production deployment

Production is deployed only from the exact commit currently fetched as
`origin/main`. Editing files under `/opt/frontend` is not a deployment
workflow.

## Before merging

- Put every code change on a branch and open a pull request.
- The **Context continuity guard** check must pass.
- Do not copy individual files from another branch onto the VPS.
- Do not use `git checkout FETCH_HEAD -- <files>`.

## Deploy

After the pull request is merged, note the full SHA shown on GitHub and run:

```bash
cd /opt/frontend
sudo scripts/deploy-frontend.sh <full-origin-main-sha>
```

The script refuses to proceed when:

- the production worktree contains tracked or untracked changes;
- production `HEAD` contains commits that are not ancestors of the target;
- the supplied SHA is no longer the fetched `origin/main` SHA;
- compilation or context-continuity tests fail;
- another deployment is in progress.

It validates the target in a temporary worktree before changing production.
After switching the whole checkout to the exact commit, it restarts
`frontend` and `frontend-gw`, checks both services, and calls the wake
health endpoint. A failed health check rolls the checkout and services back
to the previous commit.

The deployed SHA is written to
`/var/lib/hayagarden/DEPLOYED_SHA`.

## If the worktree is dirty

Stop. Do not clean, reset, or overwrite it. Create a recovery branch and
commit the changes first, then compare that branch with GitHub. The dirty
worktree is evidence that production contains source code not yet preserved
in version control. A clean worktree can still contain production-only
commits; the ancestry guard prints them and refuses deployment until they are
recovered onto a branch and merged into `main`.

Runtime data such as `.env`, `memories.db`, credentials, uploads, and
backups must remain ignored or live outside the repository. They are backed
up separately and are never committed by the deploy script.
