# GitHub公開前チェックリスト

test完了後、GitHub repositoryをpublicへ変更する前に実行します。

## 1. 公開snapshotを確定する

modified/untracked fileがすべて公開対象か確認します。`.env`、Codex認証、
SQLite、API cache、log、payload、report、security scan出力はstageしません。

```bash
git status --short
git diff --check
python scripts/private_repo_check.py
```

repository checkerはsnapshotだけでなく、公開後も到達可能な全Git refについて
成功する必要があります。

## 2. 安全でない旧履歴を置き換える

旧commitにはruntime log、Git内部file、backup file、個人author emailが
到達可能な状態で含まれていました。旧commit graphを公開せず、新しい
単一root snapshotを公開します。

最初に全local worktreeと切替時刻を調整してください。稼働中deploymentで
checkoutされているbranchを移動・削除してはいけません。stage範囲を全件
reviewし、GitHubの`noreply` author addressで新root commitを作成し、
そのcommitだけを`--force-with-lease`で`main`へ送ります。
`git push --all`と`git push --mirror`は使いません。

切替後:

- 不要なpublic remote branch/tagを削除する。
- remoteを新しい一時directoryへcloneする。
- fresh cloneで`python scripts/private_repo_check.py`を実行する。
- 意図したsecret store以外へ露出した可能性があるcredentialはrotateする。
  履歴置換はcredential revokeの代わりになりません。

## 3. Releaseを検証する

```bash
uv sync --frozen --all-extras
python -m compileall -q newsbot tests scripts
python -m unittest discover -s tests
python scripts/check_markdown_links.py
bash -n scripts/*.sh
docker compose config --quiet
docker compose build
```

GitHub Actionsの`test`と`container` jobが成功するまで公開完了としません。

## 4. GitHub設定

公開前または公開直後に設定します。

- `main`をprotectし、CI status checkを必須化する。履歴切替後のforce pushは
  禁止する。
- Dependabot updateとalertを有効化する。
- private vulnerability reportingを有効化し、`SECURITY.md`からprivate
  reportできる状態にする。
- Actionsのdefault permissionをread-onlyにし、必要なworkflowだけ明示的に
  追加権限を与える。
- repository description、topic、default branch、social preview、
  release note、MIT license検出を確認する。

GitHub公式資料:

- [Repository visibilityの設定](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/managing-repository-settings/setting-repository-visibility)
- [Branch protection ruleの管理](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/managing-a-branch-protection-rule)
- [Private vulnerability reportingの設定](https://docs.github.com/en/code-security/how-tos/report-and-fix-vulnerabilities/configure-vulnerability-reporting/configure-for-a-repository)
