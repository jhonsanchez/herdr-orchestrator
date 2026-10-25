"""Keeps every executable in a Topic's watches/ folder running.

Each line a Watch prints is collected as (watch name, line); a Watch that exits
is started again after restart_delay seconds. New scripts are picked up on the
next poll.
"""

from __future__ import annotations

import os
import queue
import subprocess
import threading
from pathlib import Path


class Supervisor:
    def __init__(self, folder: Path, restart_delay: float = 10.0) -> None:
        self.folder = folder
        self.restart_delay = restart_delay
        self._procs: dict[str, subprocess.Popen[str]] = {}
        self._exited_at: dict[str, float] = {}
        self._lines: queue.Queue[tuple[str, str]] = queue.Queue()

    def _discover(self) -> list[Path]:
        if not self.folder.is_dir():
            return []
        return sorted(
            p for p in self.folder.iterdir()
            if p.is_file() and not p.name.startswith(".") and os.access(p, os.X_OK)
        )

    def _start(self, path: Path) -> None:
        proc = subprocess.Popen(
            [str(path)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, text=True, cwd=str(self.folder),
        )
        self._procs[path.name] = proc
        threading.Thread(target=self._read, args=(path.name, proc), daemon=True).start()

    def _read(self, name: str, proc: subprocess.Popen[str]) -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            if line.strip():
                self._lines.put((name, line))

    def poll(self, now: float) -> None:
        for path in self._discover():
            proc = self._procs.get(path.name)
            if proc is None:
                self._start(path)
            elif proc.poll() is not None:
                exited = self._exited_at.setdefault(path.name, now)
                if now - exited >= self.restart_delay:
                    del self._exited_at[path.name]
                    self._start(path)

    def lines(self) -> list[tuple[str, str]]:
        out = []
        while True:
            try:
                out.append(self._lines.get_nowait())
            except queue.Empty:
                return out

    def names(self) -> list[str]:
        return sorted(self._procs)

    def stop(self) -> None:
        for proc in self._procs.values():
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
