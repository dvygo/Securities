"""Run logging: tee stderr to a timestamped log file."""
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import paths


class StderrTee:
    """Tee stderr to both console and a log file."""

    def __init__(self, log_file: Path):
        self.log_file = log_file
        self.original_stderr = sys.stderr
        self._lock = threading.Lock()

    def write(self, message: str) -> None:
        """Write to both stderr and log file."""
        with self._lock:
            self.original_stderr.write(message)
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(message)

    def flush(self) -> None:
        """Flush both stderr and log file."""
        with self._lock:
            self.original_stderr.flush()


def setup(binary_name: str, date_dir: str) -> tuple[Callable[[], None], str]:
    """
    Setup stderr tee logging to bin/LOGS/<binary>_<date_dir>_<timestamp>.log.

    Returns: (cleanup_func, log_path)
    """
    paths.ensure_bin_dirs()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = paths.logs_dir() / f"{binary_name}_{date_dir}_{timestamp}.log"

    # Initialize log file
    log_path.write_text("", encoding="utf-8")

    # Set up stderr tee
    tee = StderrTee(log_path)
    original_stderr = sys.stderr
    sys.stderr = tee

    def cleanup() -> None:
        """Restore original stderr."""
        sys.stderr = original_stderr
        tee.flush()

    return cleanup, str(log_path)
