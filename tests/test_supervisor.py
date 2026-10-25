import os
import tempfile
import time
import unittest
from pathlib import Path

from herdr_orch.supervisor import Supervisor


def script(d: Path, name: str, body: str, executable: bool = True) -> None:
    p = d / name
    p.write_text("#!/bin/sh\n" + body)
    if executable:
        p.chmod(0o755)


def drain(s: Supervisor, until: float, want: int) -> list[tuple[str, str]]:
    got: list[tuple[str, str]] = []
    deadline = time.monotonic() + until
    while time.monotonic() < deadline and len(got) < want:
        s.poll(now=time.monotonic())
        got += s.lines()
        time.sleep(0.05)
    return got


class SupervisorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_runs_only_executables_and_collects_their_lines(self) -> None:
        script(self.dir, "jira", "echo 'WAKI-1 Done'; sleep 30\n")
        script(self.dir, "notes.txt", "echo nope\n", executable=False)
        s = Supervisor(self.dir, restart_delay=0.1)
        try:
            self.assertEqual(drain(s, until=3, want=1), [("jira", "WAKI-1 Done")])
            self.assertEqual(s.names(), ["jira"])
        finally:
            s.stop()

    def test_a_crashed_watch_is_restarted(self) -> None:
        script(self.dir, "flaky", "echo up; exit 1\n")
        s = Supervisor(self.dir, restart_delay=0.1)
        try:
            got = drain(s, until=5, want=2)
            self.assertEqual(got, [("flaky", "up"), ("flaky", "up")])
        finally:
            s.stop()

    def test_missing_folder_means_no_watches(self) -> None:
        s = Supervisor(self.dir / "absent", restart_delay=0.1)
        s.poll(now=0)
        self.assertEqual(s.names(), [])
        s.stop()


if __name__ == "__main__":
    unittest.main()
