#!/usr/bin/env python3
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import FrameType
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


APP_DIR = Path(os.getenv("NEWSBOT_APP_DIR", "/app"))
RUNNER = APP_DIR / "scripts" / "run_cron_task.sh"


@dataclass(frozen=True)
class ScheduledTask:
    name: str
    schedule: str
    command: tuple[str, ...]


def task_specs(env: Mapping[str, str] | None = None) -> tuple[ScheduledTask, ...]:
    values = env if env is not None else os.environ
    generate_schedule = values.get("NEWSBOT_GENERATE_REVIEW_CRON", "0 8 * * *").strip()
    prepare_schedule = values.get("NEWSBOT_PREPARE_WEEKLY_CRON", "0 8 * * 1").strip()
    for name, schedule in (
        ("NEWSBOT_GENERATE_REVIEW_CRON", generate_schedule),
        ("NEWSBOT_PREPARE_WEEKLY_CRON", prepare_schedule),
    ):
        if len(schedule.split()) != 5:
            raise ValueError(f"{name} must contain exactly 5 cron fields: {schedule}")
    return (
        ScheduledTask("generate-review", generate_schedule, (str(RUNNER), "generate-review")),
        ScheduledTask("prepare-weekly", prepare_schedule, (str(RUNNER), "prepare-weekly")),
    )


def scheduler_timezone(env: Mapping[str, str] | None = None) -> ZoneInfo:
    values = env if env is not None else os.environ
    name = values.get("NEWSBOT_CRON_TZ", "Asia/Tokyo").strip() or "Asia/Tokyo"
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown NEWSBOT_CRON_TZ timezone: {name}") from exc


def run_task(task: ScheduledTask, stop_requested: threading.Event) -> int:
    print(f"[newsbot-scheduler] starting task={task.name}", flush=True)
    process = subprocess.Popen(task.command, cwd=APP_DIR, start_new_session=True)
    while process.poll() is None:
        if stop_requested.wait(1):
            print(f"[newsbot-scheduler] stopping task={task.name}", flush=True)
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return int(process.wait() or 0)
            try:
                return process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                return process.wait()
    print(f"[newsbot-scheduler] finished task={task.name} exit_code={process.returncode}", flush=True)
    return int(process.returncode or 0)


def main(argv: Sequence[str] | None = None) -> int:
    os.umask(0o077)
    if argv:
        raise ValueError("run_docker_scheduler.py does not accept arguments")
    try:
        from croniter import croniter
    except ImportError:
        print("croniter is required. Install the project with the docker extra.", file=sys.stderr)
        return 2

    try:
        tasks = task_specs()
        timezone = scheduler_timezone()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    for task in tasks:
        if not croniter.is_valid(task.schedule):
            print(f"Invalid cron schedule for {task.name}: {task.schedule}", file=sys.stderr)
            return 2

    stop_requested = threading.Event()

    def request_stop(signum: int, frame: FrameType | None) -> None:
        del frame
        print(f"[newsbot-scheduler] received signal={signum}", flush=True)
        stop_requested.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    now = datetime.now(timezone)
    next_runs = {
        task.name: croniter(task.schedule, now).get_next(datetime)
        for task in tasks
    }
    print(
        f"[newsbot-scheduler] timezone={timezone.key} "
        + " ".join(f"{task.name}={next_runs[task.name].isoformat()}" for task in tasks),
        flush=True,
    )

    while not stop_requested.is_set():
        now = datetime.now(timezone)
        due = [task for task in tasks if next_runs[task.name] <= now]
        if due:
            for task in due:
                run_task(task, stop_requested)
                next_runs[task.name] = croniter(
                    task.schedule,
                    datetime.now(timezone),
                ).get_next(datetime)
                if stop_requested.is_set():
                    break
            continue

        seconds_until_next = min(
            (next_runs[task.name] - now).total_seconds()
            for task in tasks
        )
        stop_requested.wait(max(1.0, min(seconds_until_next, 60.0)))

    print("[newsbot-scheduler] shutdown complete", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
