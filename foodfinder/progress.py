"""Terminal progress for one scan, including honest quiet-operation updates."""

from contextvars import ContextVar
import logging
import sys
import threading
import time


_LOG = logging.getLogger("foodfinder.progress")
_ACTIVE = ContextVar("scan_progress_reporter", default=None)


def configure_progress_logging():
    logger = logging.getLogger("foodfinder")
    if not any(getattr(handler, "foodfinder_progress", False) for handler in logger.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.foodfinder_progress = True
        handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", datefmt="%H:%M:%S"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def report(message: str):
    # Club names are external text; keep every progress update on one line.
    message = " ".join(message.splitlines())
    active = _ACTIVE.get()
    if active is not None:
        active.note(message)
    _LOG.info(message)


class ScanProgress:
    def __init__(self, interval: float = 30):
        self.interval = interval
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._message = "Starting scan"
        self._last_update = time.monotonic()
        self._thread = None
        self._token = None

    def start(self):
        self._token = _ACTIVE.set(self)
        self._thread = threading.Thread(target=self._watch, name="scan-progress", daemon=True)
        self._thread.start()

    def note(self, message: str):
        with self._lock:
            self._message = message
            self._last_update = time.monotonic()

    def _heartbeat(self):
        with self._lock:
            elapsed = time.monotonic() - self._last_update
            message = self._message
        if elapsed >= self.interval:
            _LOG.info("Waiting for progress (%ds without a new update): %s", int(elapsed), message)

    def _watch(self):
        while not self._stop.wait(self.interval):
            self._heartbeat()

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
        if self._token is not None:
            _ACTIVE.reset(self._token)
            self._token = None
