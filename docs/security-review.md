# Security Review

Date: 2026-07-28

## Scope

Repository-wide public-release review covering:

- Codex CLI candidate collection, selection, and source checks
- Paper API retrieval from arXiv, PubMed, bioRxiv, medRxiv, and Crossref
- Discord human-review and publish workflow
- SQLite draft, review, delivery, and feedback storage
- Local, systemd, cron, Docker Compose, and CI operation
- Current files and all Git history intended to remain reachable after release

## Trust Boundaries

- Web pages, paper metadata, and Codex output are untrusted input.
- Payloads are schema/config validated and deterministically ranked before storage.
- Discord reviewer actions are limited to configured user IDs and, when set, one
  guild.
- Weekly publication requires final human decisions and an explicit
  `Publish Digest` interaction.
- `.env`, Codex authentication, SQLite, payloads, prompts, logs, reports, API
  caches, and scan output are private operational data.

## Implemented Controls

- Codex prompts are sent on standard input, not command-line arguments.
- Codex child processes receive an allowlisted environment. Discord, X, and
  NCBI credentials are not inherited.
- Codex runs with Web search enabled. The application does not force a Codex
  sandbox because that mode has not been accepted as compatible with the
  required Web-search behavior in every deployment.
- NCBI API keys are used only for HTTP requests and are redacted from coverage,
  cache keys, prompts, errors, and Codex input.
- Paper API clients apply identification, rate limiting, bounded retry/backoff,
  persistent cache expiry, period filtering, and duplicate removal.
- SQL uses parameterized statements. Runtime umask and file modes restrict local
  secrets, SQLite, generated artifacts, and systemd-created files to the service
  owner.
- Docker dependencies are locked, the base image is digest-pinned, and runtime
  services use a read-only root filesystem, dropped capabilities,
  `no-new-privileges`, resource limits, and per-service secret assignment.
- CI compiles and tests Python, validates shell/Compose/Markdown, audits Python
  dependencies, builds an image with provenance/SBOM, and scans the image.
- `scripts/private_repo_check.py` checks tracked and untracked release files,
  compares against live local credentials without printing them, and scans all
  reachable Git history for secrets and forbidden runtime paths.

## Residual Risks

- Prompt injection and inaccurate Web content remain possible because Codex must
  be able to search the Web and is not forced into a sandbox. Keep human review,
  inspect source URLs, and run Codex under a dedicated low-privilege OS user or
  container.
- Codex necessarily has access to its own authentication state and any explicitly
  allowlisted OpenAI authentication variables.
- A compromised host, Docker daemon, CI runner, or service account can access
  runtime secrets and operational data.
- API metadata and abstracts can be copyrighted or subject to provider terms.
  Follow the [Legal Notices](legal-notices.md).
- SQLite is a single-host store. Do not run multiple bot replicas against the
  same database volume, and protect backups as sensitive operational data.
- Legacy webhook/X delivery remains available and carries additional credentials.
  Prefer the Discord Bot review workflow.

## Public Release Gate

Run from the repository root:

```bash
python scripts/private_repo_check.py
python scripts/check_markdown_links.py
python -m compileall -q newsbot tests scripts
python -m unittest discover -s tests
bash -n scripts/*.sh
docker compose config --quiet
```

The release is blocked unless every command succeeds. In particular, the
repository checker must pass after history cleanup; deleting a secret only from
the working tree is insufficient. Revoke and rotate any credential that was ever
exposed outside its intended environment.
