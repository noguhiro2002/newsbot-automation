from __future__ import annotations

import unittest

from scripts.run_docker_scheduler import scheduler_timezone, task_specs


class DockerSchedulerTests(unittest.TestCase):
    def test_task_specs_use_documented_defaults(self):
        generate, prepare = task_specs({})

        self.assertEqual(generate.name, "generate-review")
        self.assertEqual(generate.schedule, "0 8 * * *")
        self.assertEqual(generate.command[-1], "generate-review")
        self.assertEqual(prepare.name, "prepare-weekly")
        self.assertEqual(prepare.schedule, "0 8 * * 1")
        self.assertEqual(prepare.command[-1], "prepare-weekly")

    def test_task_specs_read_schedule_overrides(self):
        generate, prepare = task_specs(
            {
                "NEWSBOT_GENERATE_REVIEW_CRON": "30 7 * * *",
                "NEWSBOT_PREPARE_WEEKLY_CRON": "15 9 * * 2",
            }
        )

        self.assertEqual(generate.schedule, "30 7 * * *")
        self.assertEqual(prepare.schedule, "15 9 * * 2")

    def test_task_specs_reject_wrong_field_count(self):
        with self.assertRaisesRegex(ValueError, "exactly 5 cron fields"):
            task_specs({"NEWSBOT_GENERATE_REVIEW_CRON": "0 8 * *"})

    def test_scheduler_timezone_uses_configured_zone(self):
        timezone = scheduler_timezone({"NEWSBOT_CRON_TZ": "UTC"})

        self.assertEqual(timezone.key, "UTC")

    def test_scheduler_timezone_rejects_unknown_zone(self):
        with self.assertRaisesRegex(ValueError, "Unknown NEWSBOT_CRON_TZ"):
            scheduler_timezone({"NEWSBOT_CRON_TZ": "Not/A_Real_Zone"})


if __name__ == "__main__":
    unittest.main()
