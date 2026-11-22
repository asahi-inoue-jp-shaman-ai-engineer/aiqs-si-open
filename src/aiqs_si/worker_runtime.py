"""Process exclusion and non-sensitive worker heartbeat for the dedicated host."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import threading
import time


@contextmanager
def exclusive_worker(path):
    """Hold an OS lock, not a stale PID-file check. All instances use the same path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    locked = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("Worker runtime is already owned") from exc
        locked = True
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


class Heartbeat:
    def __init__(self, path, *, mode, interval=5):
        self.path, self.mode, self.interval = Path(path), mode, interval
        self.state = "starting"
        self.failure = None
        self.guard = threading.Lock()
        self.stop = threading.Event()

    def update(self, state):
        with self.guard:
            if state is not None:
                self.state = state
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"pid": os.getpid(), "mode": self.mode,
                "state": self.state, "timestamp_epoch": time.time()}), encoding="utf-8")
            # Windows readers can temporarily deny replacement; bounded retry only.
            for attempt in range(5):
                try:
                    temporary.replace(self.path)
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.01)

    def __enter__(self):
        self.update("starting")
        def refresh():
            while not self.stop.wait(self.interval):
                try:
                    self.update(None)
                except OSError:
                    self.failure = "heartbeat_write_failed"
                    return
        self.thread = threading.Thread(target=refresh, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join()
        self.update("stopped" if exc[0] is None else "stopped_with_error")


def run_loop(worker, heartbeat, *, once=False, wait=time.sleep, stop_requested=lambda: False):
    while True:
        # Checked between jobs only: never interrupt device shutdown/result storage.
        if stop_requested():
            heartbeat.update("drained")
            return
        if heartbeat.failure:
            raise RuntimeError(heartbeat.failure)
        heartbeat.update("polling_or_executing")
        try:
            worked = worker.run_one()
        except (ConnectionError, TimeoutError):
            heartbeat.update("queue_unavailable")
            if once:
                raise
        else:
            heartbeat.update("idle" if not worked else "delivery_complete")
            if once:
                return
        wait(2)
