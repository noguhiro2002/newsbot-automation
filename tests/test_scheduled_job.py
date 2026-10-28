from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import run_scheduled_job as job


class ScheduledJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.notices = Mock()
        for context in (patch.dict(os.environ, {}, clear=True), patch.object(job, "PROJECT_ROOT", self.root), patch.object(job, "log")):
            context.start()
            self.addCleanup(context.stop)

    def run_stage(self, results, **settings):
        with patch.object(job, "run_attempt", side_effect=results) as run, patch.object(job.time, "sleep") as sleep:
            code = job.retry_stage("generate", lambda _: ["fake"], settings=job.Settings(**settings),
                                   notices=self.notices, run_id="test", log_path=self.root / "job.log")
        return code, run, sleep

    def test_temporary_failure_recovers_and_announces_recovery(self):
        code, run, sleep = self.run_stage([(1, "Network unreachable"), (0, "OK")])
        self.assertEqual(code, 0)
        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once_with(300)
        self.assertEqual([c.kwargs.get("event", "failed") for c in self.notices.send.call_args_list], ["retry", "recovered"])

    def test_attempts_are_bounded_and_delays_configurable(self):
        code, run, sleep = self.run_stage([(1, "invalid JSON")] * 3, retry_seconds=7, retry_backoff=3, retry_max_seconds=15)
        self.assertEqual(code, 1)
        self.assertEqual(run.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [7, 15])
        self.assertEqual(self.notices.send.call_count, 2)
        self.assertIn("最終失敗", self.notices.send.call_args.args[0])

    def test_permanent_failures_stop_immediately_and_do_not_leak_raw_errors(self):
        for reason in ("refresh_token_invalidated", "model_not_found", "failed to spawn code-mode host", "Discord API error (403)"):
            with self.subTest(reason=reason):
                self.notices.reset_mock()
                code, run, sleep = self.run_stage([(1, reason + ' token=secret @everyone')])
                self.assertEqual(code, 1)
                self.assertEqual(run.call_count, 1)
                sleep.assert_not_called()
                message = self.notices.send.call_args.args[0]
                self.assertIn("最終失敗", message)
                self.assertNotIn("secret", message)
                self.assertNotIn("@everyone", message)

    def test_diagnostic_logs_are_restricted_to_payloads(self):
        payloads = self.root / "payloads"
        payloads.mkdir()
        path = payloads / "phase.codex.log"
        path.write_text("refresh_token_invalidated")
        outside = self.root / "private.codex.log"
        outside.write_text("secret")
        detail = job.diagnostic_text(f"see {path}\nsee {outside}")
        self.assertIn("refresh_token_invalidated", detail)
        self.assertNotIn("secret", detail)

    def test_run_attempt_records_output_and_exit_code(self):
        path = self.root / "attempt.log"
        code, text = job.run_attempt([sys.executable, "-c", "print('Network unreachable'); raise SystemExit(7)"], 10, path)
        self.assertEqual(code, 7)
        self.assertIn("Network unreachable", text)
        self.assertIn("Network unreachable", path.read_text())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_run_attempt_timeout_terminates_child(self):
        code, text = job.run_attempt([sys.executable, "-c", "import time; print('started',flush=True); time.sleep(30)"], 0.2, self.root / "attempt.log")
        self.assertEqual(code, 124)
        self.assertIn("TimeoutError", text)

    def test_settings_reject_unbounded_retries_and_timeouts(self):
        for key, value in (("NEWSBOT_JOB_MAX_ATTEMPTS", "4"), ("NEWSBOT_JOB_ATTEMPT_TIMEOUT_SECONDS", "10801"), ("NEWSBOT_JOB_RETRY_SECONDS", "-1")):
            with self.subTest(key=key), patch.dict(os.environ, {key: value}):
                with self.assertRaises(ValueError):
                    job.Settings.from_env()

    def test_submission_retries_reuse_payload_without_regenerating(self):
        calls = []
        submit_calls = 0
        state = Mock()

        def attempt(command, timeout, log_path):
            nonlocal submit_calls
            calls.append(command)
            if "--output" in command:
                output = Path(command[command.index("--output") + 1])
                output.parent.mkdir()
                output.write_text(json.dumps({"topic": "lab_automation", "cadence": "weekly", "items": [{"title": "news"}]}))
                self.assertIn("--defer-run-state", command)
                self.assertNotIn("--submit-review", command)
                return 0, ""
            submit_calls += 1
            state.assert_not_called()
            return (1, "DiscordApiError: Network unreachable") if submit_calls == 1 else (0, "")

        with patch.object(job, "run_attempt", side_effect=attempt), patch.object(job, "resolve_period", return_value="fixed period"), patch.object(job, "write_last_run", state), patch.object(job.time, "sleep"):
            code = job.generate_review(job.Settings(retry_seconds=0), self.notices, run_id="test")
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[1], calls[2])
        state.assert_called_once()

    def test_generation_retries_keep_period_and_separate_audit_files(self):
        commands = []

        def attempt(command, timeout, log_path):
            commands.append(command)
            if len(commands) == 1:
                return 1, "Could not find a complete JSON object"
            output = Path(command[command.index("--output") + 1])
            output.parent.mkdir()
            output.write_text(json.dumps({"topic": "lab_automation", "cadence": "weekly", "items": []}))
            return 0, ""

        with patch.object(job, "run_attempt", side_effect=attempt), patch.object(job, "resolve_period", return_value="fixed period"), patch.object(job, "write_last_run") as state, patch.object(job.time, "sleep"):
            self.assertEqual(job.generate_review(job.Settings(retry_seconds=0), self.notices, run_id="test"), 0)
        self.assertEqual(len(commands), 2)
        self.assertEqual([c[c.index("--period") + 1] for c in commands], ["fixed period", "fixed period"])
        self.assertNotEqual(commands[0][commands[0].index("--output") + 1], commands[1][commands[1].index("--output") + 1])
        state.assert_called_once()

    def test_failed_delivery_never_advances_collection_state(self):
        def attempt(command, timeout, log_path):
            if "--output" in command:
                output = Path(command[command.index("--output") + 1])
                output.parent.mkdir()
                output.write_text(json.dumps({"topic": "lab_automation", "cadence": "weekly", "items": [{"title": "news"}]}))
                return 0, ""
            return 1, "Discord API error (403)"

        with patch.object(job, "run_attempt", side_effect=attempt), patch.object(job, "resolve_period", return_value="fixed period"), patch.object(job, "write_last_run") as state:
            self.assertEqual(job.generate_review(job.Settings(), self.notices, run_id="test"), 1)
        state.assert_not_called()

    def test_notification_queue_survives_delivery_failure_and_uses_review_channel(self):
        with patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "private-token", "DISCORD_REVIEW_CHANNEL_ID": "review", "DISCORD_PUBLISH_CHANNEL_ID": "publish"}), patch.object(job, "DiscordBotClient") as factory:
            client = factory.return_value
            client.create_message.side_effect = RuntimeError("Network unreachable")
            notices = job.Notifications(self.root)
            self.assertFalse(notices.send("通知テスト"))
            paths = list(notices.queue.glob("*.json"))
            self.assertEqual(len(paths), 1)
            self.assertNotIn("private-token", paths[0].read_text())
            self.assertEqual(paths[0].stat().st_mode & 0o777, 0o600)
            client.create_message.side_effect = None
            self.assertTrue(notices.flush())
            client.create_message.assert_called_with("review", "通知テスト", allowed_mentions={"parse": []})
            self.assertEqual(list(notices.queue.glob("*.json")), [])

    def test_notification_event_filter_and_global_switch(self):
        with patch.dict(os.environ, {"NEWSBOT_JOB_NOTIFICATION_EVENTS": "failed"}), patch.object(job.Notifications, "flush") as flush:
            notices = job.Notifications(self.root)
            notices.send("retry", event="retry")
            notices.send("recovery", event="recovered")
            flush.assert_not_called()
            notices.send("failed")
            flush.assert_called_once()
        with patch.dict(os.environ, {"NEWSBOT_JOB_NOTIFICATIONS_ENABLED": "false"}), patch.object(job, "DiscordBotClient") as client:
            notices = job.Notifications(self.root)
            self.assertTrue(notices.send("failed"))
            self.assertTrue(notices.flush())
            client.assert_not_called()

    def test_corrupt_notification_does_not_block_remaining_queue(self):
        with patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "test", "DISCORD_REVIEW_CHANNEL_ID": "review"}), patch.object(job, "DiscordBotClient") as client:
            notices = job.Notifications(self.root)
            notices.queue.mkdir()
            (notices.queue / "bad.json").write_text("bad")
            job.private_json(notices.queue / "valid.json", {"content": "good"})
            self.assertTrue(notices.flush())
            self.assertTrue((notices.queue / "bad.invalid").exists())
            client.return_value.create_message.assert_called_once_with("review", "good", allowed_mentions={"parse": []})


class CronRunnerIntegrationTests(unittest.TestCase):
    def test_shell_invokes_retry_wrapper_and_updates_state_only_on_success(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "run_cron_task.sh"
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "scripts").mkdir()
            (root / ".env").write_text("")
            wrapper = root / "scripts" / "run_scheduled_job.py"
            env = {**os.environ, "NEWSBOT_ENV_FILE": str(root / ".env"), "NEWSBOT_APP_DIR": str(root),
                   "NEWSBOT_PYTHON_BIN": sys.executable, "NEWSBOT_CRON_STATE_DIR": str(root / "state"),
                   "NEWSBOT_GENERATE_REVIEW_EVERY_DAYS": "0"}
            for code in (1, 0):
                wrapper.write_text(f"raise SystemExit({code})\n")
                result = subprocess.run(["bash", str(script), "generate-review"], env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, code, result.stderr)
                self.assertEqual((root / "state" / "generate-review.last_success").exists(), code == 0)


if __name__ == "__main__":
    unittest.main()
