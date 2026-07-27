# Public Release Checklist

Use this checklist after tests pass and before changing the GitHub repository
visibility.

## 1. Review the exact snapshot

Confirm that every modified and untracked file belongs in the release. Do not
stage `.env`, Codex authentication, SQLite, API caches, logs, payloads, reports,
or security-scan output.

```bash
git status --short
git diff --check
python scripts/private_repo_check.py
```

The repository checker must pass for both the snapshot and every Git ref that
will remain reachable.

## 2. Replace unsafe history

This repository previously had reachable commits containing runtime logs, Git
internal files, backup files, and a personal author email. Publish a new
single-root snapshot rather than the old commit graph.

Coordinate this cutover with every local worktree first. In particular, do not
move or delete a branch that is checked out by a running deployment. Review the
complete staging scope, create a new root commit with a GitHub `noreply` author
address, and push only that intended commit to `main` using
`--force-with-lease`. Do not use `git push --all` or `git push --mirror`.

After the cutover:

- Delete obsolete public remote branches and tags.
- Clone the remote into a fresh temporary directory.
- Run `python scripts/private_repo_check.py` in that fresh clone.
- Rotate any credential that was ever exposed outside its intended secret
  store; history replacement is not a substitute for revocation.

## 3. Verify the release

```bash
uv sync --frozen --all-extras
python -m compileall -q newsbot tests scripts
python -m unittest discover -s tests
python scripts/check_markdown_links.py
bash -n scripts/*.sh
docker compose config --quiet
docker compose build
```

Wait for the `test` and `container` GitHub Actions jobs to pass.

## 4. Configure GitHub

Before or immediately after making the repository public:

- Protect `main`; require the CI status checks and block force pushes after the
  one-time history cutover.
- Enable Dependabot updates and alerts.
- Enable private vulnerability reporting so `SECURITY.md` can direct reporters
  to a private channel.
- Confirm Actions permissions are read-only by default unless a workflow
  explicitly needs more.
- Review repository description, topics, default branch, social preview,
  release notes, and MIT license detection.

Official references:

- [Setting repository visibility](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/managing-repository-settings/setting-repository-visibility)
- [Managing a branch protection rule](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/managing-a-branch-protection-rule)
- [Configuring private vulnerability reporting](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/configure-for-a-repository)
