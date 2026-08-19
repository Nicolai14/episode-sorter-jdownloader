"""Background loop that drives the pipeline."""
from __future__ import annotations

import threading
import time
import traceback
from typing import Any

from .. import config
from ..db import session_scope
from . import pipeline


class Scheduler:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._run_lock = threading.Lock()
        self.last_run: dict[str, Any] | None = None
        self.last_error: str | None = None
        self.running = False

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="episode-sorter-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def trigger(self) -> None:
        self._wake.set()

    def run_once(self) -> dict[str, Any]:
        """Run one full pass. Safe to call from the API while the loop is idle."""
        with self._run_lock:
            self.running = True
            started = time.time()
            try:
                with session_scope() as session:
                    result = pipeline.tick(session)
                self.last_error = None
            except Exception as exc:  # noqa: BLE001 - the loop must survive everything
                self.last_error = f"{exc}\n{traceback.format_exc(limit=3)}"
                result = {"error": str(exc)}
                try:
                    with session_scope() as session:
                        pipeline.log(session, f"Fehler im Ablauf: {exc}", level="error", source="scheduler")
                except Exception:  # noqa: BLE001
                    pass
            finally:
                self.running = False
            result["duration_seconds"] = round(time.time() - started, 2)
            result["at"] = time.time()
            self.last_run = result
            return result

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            interval = max(15, int(config.get("scan_interval_seconds", 60)))
            self._wake.wait(timeout=interval)
            self._wake.clear()


scheduler = Scheduler()
