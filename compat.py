"""What Windows does differently, for the launcher, the bridge and Claude plays.

replace() swaps a freshly written file into place: on Windows that fails while another process
has the old file open, and the game, the bridge and the panel read these files many times a
second, so it tries again for a moment. stop() ends a child and everything it started: on
Windows terminate() kills just that one process, with no SIGTERM for it to pass on, so its
children (claude, say) would carry on without it.
"""
import os
import subprocess
import time

WINDOWS = os.name == "nt"


def replace(tmp, path):
    """tmp.replace(path), retried for up to a second on Windows while a reader has path open."""
    for _ in range(50):
        try:
            return tmp.replace(path)
        except PermissionError:
            if not WINDOWS:
                raise
            time.sleep(0.02)
    return tmp.replace(path)


def stop(proc, timeout=10):
    """Stop proc and its children, waiting up to timeout seconds before killing it."""
    if proc.poll() is not None:
        return
    if WINDOWS and isinstance(proc, subprocess.Popen):
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        proc.terminate()
    try:
        proc.wait(timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
