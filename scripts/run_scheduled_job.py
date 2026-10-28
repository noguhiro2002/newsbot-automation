from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from newsbot.discord_client import DiscordBotClient
from scripts.generate_payload_openai import load_env_file, resolve_period, run_state_path, write_last_run


JST = ZoneInfo("Asia/Tokyo")
STAGES = {"generate": "候補生成", "submit": "レビュー投稿"}


def log(message: str) -> None:
    print(f"[newsbot-job] {message}", flush=True)


def private_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            temporary.chmod(0o600)
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class Notifications:
    """Keep undelivered alerts on disk; the notification timer retries them."""

    def __init__(self, state_dir: Path):
        self.queue = state_dir / "job-notifications"
        value = os.environ.get("NEWSBOT_JOB_NOTIFICATIONS_ENABLED", "true").strip().lower()
        if value not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
            raise ValueError("NEWSBOT_JOB_NOTIFICATIONS_ENABLED must be true or false")
        self.enabled = value in {"1", "true", "yes", "on"}
        self.events = {part.strip() for part in os.environ.get("NEWSBOT_JOB_NOTIFICATION_EVENTS", "retry,failed,recovered").split(",") if part.strip()}
        if self.events - {"retry", "failed", "recovered"}:
            raise ValueError("NEWSBOT_JOB_NOTIFICATION_EVENTS accepts retry,failed,recovered")

    def send(self, content: str, *, event: str = "failed") -> bool:
        if not self.enabled or (event != "test" and event not in self.events):
            log(f"notification disabled for event={event}")
            return True
        key = uuid.uuid4().hex
        private_json(self.queue / f"{key}.json", {
            "content": content[:1900],
            "created_at": datetime.now(JST).isoformat(timespec="seconds"),
        })
        return self.flush()

    def flush(self) -> bool:
        if not self.enabled:
            return True
        self.queue.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.queue / ".lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            paths = sorted(self.queue.glob("*.json"), key=lambda p: p.stat().st_mtime)
            if not paths:
                return True
            try:
                channel = os.environ.get("DISCORD_REVIEW_CHANNEL_ID", "").strip()
                if not channel:
                    raise ValueError("DISCORD_REVIEW_CHANNEL_ID is not set")
                client = DiscordBotClient(os.environ.get("DISCORD_BOT_TOKEN", ""), max_rate_limit_retries=0)
                for path in paths[:20]:
                    try:
                        content = json.loads(path.read_text(encoding="utf-8"))["content"]
                        if not isinstance(content, str):
                            raise ValueError("invalid queued content")
                    except (ValueError, KeyError, TypeError):
                        path.rename(path.with_suffix(".invalid"))
                        log(f"invalid queued notification quarantined: {path.name}")
                        continue
                    client.create_message(channel, content, allowed_mentions={"parse": []})
                    path.unlink()
                    log(f"review-channel notification delivered: {path.name}")
            except Exception as exc:
                # Keep raw remote errors and credentials out of operational notices.
                log(f"notification delivery failed ({type(exc).__name__}); queued for retry")
                return False
        return True


@dataclass(frozen=True)
class Settings:
    attempts: int = 3
    retry_seconds: int = 300
    timeout_seconds: int = 10800
    retry_backoff: int = 2
    retry_max_seconds: int = 1800

    @classmethod
    def from_env(cls) -> Settings:
        fields = (
            ("NEWSBOT_JOB_MAX_ATTEMPTS", 3, 1, 3),
            ("NEWSBOT_JOB_RETRY_SECONDS", 300, 0, 600),
            ("NEWSBOT_JOB_ATTEMPT_TIMEOUT_SECONDS", 10800, 1, 10800),
            ("NEWSBOT_JOB_RETRY_BACKOFF", 2, 1, 4),
            ("NEWSBOT_JOB_RETRY_MAX_SECONDS", 1800, 0, 1800),
        )
        values = []
        for key, default, minimum, maximum in fields:
            value = int(os.environ.get(key, "").strip() or default)
            if not minimum <= value <= maximum:
                raise ValueError(f"{key} must be between {minimum} and {maximum}")
            values.append(value)
        return cls(*values)


def classify_failure(text: str) -> tuple[str, bool]:
    lowered = text.lower()
    if any(value in lowered for value in (
        "refresh_token_invalidated", "refresh_token_reused", "refresh token was revoked",
        "access token could not be refreshed", "token_expired", "401 unauthorized",
        "discord api error (401)", "discord api error (403)",
    )):
        return "認証・権限エラー：Codexの再ログイン、またはDiscordのトークン・権限確認が必要です。", False
    if "model_not_found" in lowered or re.search(r"model.*not supported", lowered):
        return "モデル設定エラー：指定モデルとCodex CLIの対応状況を確認してください。", False
    if any(value in lowered for value in (
        "failed to spawn code-mode host", "host executable was not found", "filenotfounderror",
        "permissionerror", "permission denied",
    )):
        return "実行環境エラー：実行ファイルの配置とアクセス権を確認してください。", False
    if any(value in lowered for value in (
        "network unreachable", "connection failed", "connection reset", "timed out", "timeouterror",
        "temporary failure", "name resolution", "429", "503", "502", "stream disconnected",
        "discordapierror",
    )):
        return "通信・一時的なサービスエラーが発生しました。", True
    return "生成・検証または投稿処理が失敗しました。詳細は実行ログを確認してください。", True


def diagnostic_text(tail: str) -> str:
    """Read only referenced Codex logs under payloads, never arbitrary paths."""
    pieces = [tail]
    for match in re.findall(r"(?:see |from )([^\s;]+\.codex\.log)", tail)[-3:]:
        path = Path(match).resolve()
        if not path.is_relative_to((PROJECT_ROOT / "payloads").resolve()) or not path.is_file():
            continue
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - 65536))
            pieces.append(handle.read().decode("utf-8", errors="replace"))
    return "\n".join(pieces)


def stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def run_attempt(command: list[str], timeout: int, log_path: Path) -> tuple[int, str]:
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tail: deque[str] = deque(maxlen=100)
    with log_path.open("a", encoding="utf-8") as handle:
        log_path.chmod(0o600)
        handle.write(f"\n--- {datetime.now(JST).isoformat()} ---\n")
        handle.flush()
        try:
            process = subprocess.Popen(command, cwd=PROJECT_ROOT, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, errors="replace", start_new_session=True)
        except OSError as exc:
            return 1, f"{type(exc).__name__}: {exc}"

        def consume() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                handle.write(line)
                handle.flush()
                print(line, end="", flush=True)
                tail.append(line[-4096:])

        thread = threading.Thread(target=consume, daemon=True)
        thread.start()
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            stop_process(process)
            tail.append(f"TimeoutError: attempt exceeded {timeout} seconds")
            code = 124
        except BaseException:
            stop_process(process)
            raise
        finally:
            thread.join(timeout=15)
            if process.stdout is not None and not thread.is_alive():
                process.stdout.close()
    return code, "".join(tail)[-65536:]


def notice(event: str, stage: str, attempt: int, settings: Settings, run_id: str, detail: str) -> str:
    return (
        f"【Newsbot 定期ニュース収集：{event}】\n"
        f"日時：{datetime.now(JST):%Y-%m-%d %H:%M:%S} JST\n"
        f"ホスト：{socket.gethostname()} / 処理：{STAGES[stage]}\n"
        f"試行：{attempt}/{settings.attempts} / 実行ID：{run_id}\n"
        f"{detail}\n"
        "ログ：`journalctl -u newsbot-generate-review.service -n 100 --no-pager`\n"
        "保存ログ：`logs/newsbot-scheduled-job.log`"
    )


def retry_stage(stage: str, command_for_attempt, *, settings: Settings, notices: Notifications,
                run_id: str, log_path: Path) -> int:
    for attempt in range(1, settings.attempts + 1):
        log(f"run={run_id} stage={stage} attempt={attempt}/{settings.attempts}")
        timeout = settings.timeout_seconds if stage == "generate" else min(600, settings.timeout_seconds)
        code, tail = run_attempt(command_for_attempt(attempt), timeout, log_path)
        if code == 0:
            if attempt > 1:
                notices.send(notice("復旧", stage, attempt, settings, run_id, "再試行に成功しました。"), event="recovered")
            return 0
        summary, retryable = classify_failure(diagnostic_text(tail))
        if not retryable or attempt == settings.attempts:
            notices.send(notice("最終失敗", stage, attempt, settings, run_id,
                                summary + " 自動再試行を終了しました。"))
            return code if code > 0 else 1
        delay = min(settings.retry_max_seconds, settings.retry_seconds * settings.retry_backoff ** (attempt - 1))
        if attempt == 1:
            notices.send(notice("失敗・再試行予定", stage, attempt, settings, run_id,
                                f"{summary} {delay}秒後に再試行します。"), event="retry")
        log(f"run={run_id} stage={stage} failed; retry in {delay}s")
        time.sleep(delay)
        notices.flush()
    return 1


def generate_review(settings: Settings, notices: Notifications, *, run_id: str) -> int:
    topic = os.environ.get("NEWSBOT_GENERATE_REVIEW_TOPIC", "").strip() or "lab_automation"
    cadence = os.environ.get("NEWSBOT_GENERATE_REVIEW_CADENCE", "").strip() or "weekly"
    # Freeze the period for all attempts; advance it only after successful delivery.
    cutoff = datetime.now(JST).replace(tzinfo=None)
    lookback = os.environ.get("NEWSBOT_LOOKBACK_DAYS", "").strip()
    period = resolve_period(topic=topic, cadence=cadence, lookback_days=int(lookback) if lookback else None, now=cutoff)
    output: Path | None = None
    log_path = PROJECT_ROOT / "logs" / "newsbot-scheduled-job.log"

    def generation_command(attempt: int) -> list[str]:
        nonlocal output
        output = PROJECT_ROOT / "payloads" / f"scheduled_{run_id}_attempt_{attempt}.json"
        command = [sys.executable, str(PROJECT_ROOT / "scripts" / "generate_payload_openai.py"),
                   "--topic", topic, "--cadence", cadence, "--period", period,
                   "--output", str(output), "--validate-only", "--defer-run-state"]
        codex = os.environ.get("NEWSBOT_CODEX_BIN", "").strip()
        if codex:
            command.extend(["--codex-bin", codex])
        return command

    code = retry_stage("generate", generation_command, settings=settings, notices=notices,
                       run_id=run_id, log_path=log_path)
    if code:
        return code
    assert output is not None
    payload = json.loads(output.read_text(encoding="utf-8"))
    if payload.get("topic") != topic or payload.get("cadence") != cadence or not isinstance(payload.get("items"), list):
        raise ValueError("invalid generated payload")
    if payload.get("items"):
        code = retry_stage("submit", lambda _: [sys.executable, "-m", "newsbot.cli", "submit-review",
                                                 "--input", str(output)],
                           settings=settings, notices=notices, run_id=run_id, log_path=log_path)
        if code:
            return code
    write_last_run(run_state_path(topic, cadence), cutoff)
    log(f"run={run_id} complete; payload={output}")
    return 0


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    load_env_file(Path(os.environ.get("NEWSBOT_ENV_FILE") or PROJECT_ROOT / ".env"))
    parser = argparse.ArgumentParser(description="Bounded scheduled generation retries and review-channel failure alerts.")
    parser.add_argument("task", choices=["generate-review", "flush-notifications", "test-notification"])
    args = parser.parse_args(argv)
    state_dir = Path(os.environ.get("NEWSBOT_CRON_STATE_DIR") or PROJECT_ROOT / "data" / "cron")
    notices = Notifications(state_dir)
    if args.task == "flush-notifications":
        return 0 if notices.flush() else 1
    if args.task == "test-notification":
        delivered = notices.send(f"【Newsbot 通知テスト】\n{datetime.now(JST):%Y-%m-%d %H:%M:%S} JST\n定期ニュース収集の失敗・再試行・復旧通知を、このレビューチャンネルへ送信します。", event="test")
        return 0 if delivered else 1
    run_id = datetime.now(JST).strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]
    try:
        settings = Settings.from_env()
        notices.flush()
        return generate_review(settings, notices, run_id=run_id)
    except Exception as exc:
        log(f"run={run_id} unexpected failure ({type(exc).__name__})")
        notices.send(f"【Newsbot 定期ニュース収集：最終失敗】\n実行ID：{run_id}\n実行設定またはジョブ制御でエラーが発生しました（{type(exc).__name__}）。\n`journalctl -u newsbot-generate-review.service -n 100 --no-pager` を確認してください。")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
