import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import container_healthcheck


class ContainerHealthcheckTests(unittest.TestCase):
    def test_process_command_lines_reads_nul_separated_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc_root = Path(tmp)
            cmdline = proc_root / "101" / "cmdline"
            cmdline.parent.mkdir()
            cmdline.write_bytes(b"python\0-m\0newsbot.cli\0run-discord-bot\0")

            self.assertEqual(
                container_healthcheck.process_command_lines(proc_root),
                ["python -m newsbot.cli run-discord-bot "],
            )

    def test_main_requires_all_terms_in_one_process(self) -> None:
        commands = [
            "python -m newsbot.cli run-discord-bot",
            "python scripts/run_docker_scheduler.py",
        ]
        with patch.object(
            container_healthcheck,
            "process_command_lines",
            return_value=commands,
        ):
            self.assertEqual(
                container_healthcheck.main(["newsbot.cli", "run-discord-bot"]),
                0,
            )
            self.assertEqual(
                container_healthcheck.main(["newsbot.cli", "scheduler"]),
                1,
            )

    def test_main_rejects_empty_terms(self) -> None:
        self.assertEqual(container_healthcheck.main([]), 2)


if __name__ == "__main__":
    unittest.main()
