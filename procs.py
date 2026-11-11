# ============================================================
#  procs.py  -  small helpers for running things in the background
#
#  Used by run_all.py, lookup_server.py and launch.py:
#   - is a process still alive? is a port in use / answering?
#   - start a program hidden (no window) and detached (keeps running)
#   - the run lock: only ONE update runs at a time
#   - the step status file the launcher window and dashboard read
# ============================================================

import ctypes
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import settings

OUT = settings.OUTPUT_FOLDER
LOCK_FILE = OUT / "run.lock"            # exists while an update is running (holds its process ID)
STATUS_FILE = OUT / "run_status.json"   # which step the running update is on
STEPS_FILE = OUT / "steps.json"         # per step: last run, ok or not, last time it was good
LOG_FILE = OUT / "run.log"              # timestamped log of the last 10 runs

# Windows process flags (https://learn.microsoft.com/windows/win32/procthread/process-creation-flags)
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def now_text():
    return f"{datetime.now():%Y-%m-%d %H:%M:%S}"


# --- Processes -------------------------------------------------

def pid_alive(pid):
    """True if a process with this ID is still running."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    k = ctypes.windll.kernel32
    handle = k.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    code = ctypes.c_ulong()
    k.GetExitCodeProcess(handle, ctypes.byref(code))
    k.CloseHandle(handle)
    return code.value == 259                            # STILL_ACTIVE


def windowless_python():
    """pythonw.exe next to this Python (no console window), else python."""
    w = Path(sys.executable).with_name("pythonw.exe")
    return str(w) if w.exists() else sys.executable


def console_python():
    """python.exe (also when running under pythonw), for child steps whose output we read."""
    p = Path(sys.executable)
    c = p.with_name("python.exe")
    return str(c) if c.exists() else str(p)


def start_hidden(args, log_path, cwd, env=None):
    """Start a program with no window, detached from this one (it keeps
    running after we exit). Output goes to log_path. Returns the process ID."""
    log = open(log_path, "a", encoding="utf-8")
    kwargs = dict(cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                  close_fds=True)
    if os.name == "nt":
        base = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        try:      # also leave a Task Scheduler job, so the task ending doesn't stop it
            proc = subprocess.Popen(args, creationflags=base | CREATE_BREAKAWAY_FROM_JOB, **kwargs)
        except OSError:
            proc = subprocess.Popen(args, creationflags=base, **kwargs)
    else:
        proc = subprocess.Popen(args, start_new_session=True, **kwargs)
    log.close()
    return proc.pid


def stop_pid(pid):
    """Stop a process (and anything it started)."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                       creationflags=CREATE_NO_WINDOW)
    else:
        os.kill(int(pid), 15)


# --- Ports -----------------------------------------------------

def port_free(port):
    """True if nothing is listening on this port on this PC."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) != 0


def streamlit_answers(port, timeout=1.5):
    """True if a Streamlit app answers on this port."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/_stcore/health", timeout=timeout) as r:
            return r.status == 200 and r.read().strip() == b"ok"
    except Exception:                                    # noqa: BLE001 - refused, timeout, 404 ...
        return False


def first_free_port(start=8501, tries=100):
    for port in range(start, start + tries):
        if port_free(port):
            return port
    raise RuntimeError(f"no free port between {start} and {start + tries - 1}")


# --- The run lock (one update at a time) -----------------------

def lock_holder():
    """(pid, started) of the update that is running now, or None."""
    try:
        info = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return (info["pid"], info["started"]) if pid_alive(info.get("pid")) else None


def take_lock():
    """True if we now hold the lock; False if another update is running.
    A lock left behind by a crashed run (its process is gone) is taken over."""
    OUT.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)   # fails if it exists
        except FileExistsError:
            if lock_holder():
                return False
            try:
                LOCK_FILE.unlink()                       # stale: its process is gone
            except OSError:
                pass
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "started": now_text()}, f)
        return True
    return False


def release_lock():
    try:
        info = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
        if info.get("pid") == os.getpid():
            LOCK_FILE.unlink()
    except (OSError, ValueError):
        pass


def wait_for_unlock(poll=2.0, on_wait=None):
    """Block until no update is running (calls on_wait(status) while waiting)."""
    while lock_holder():
        if on_wait:
            on_wait(read_status())
        time.sleep(poll)


# --- Step status (for the launcher window and the dashboard) ---

def write_status(**fields):
    settings.write_text_safely(STATUS_FILE, json.dumps({**fields, "updated": now_text()}, ensure_ascii=False))


def read_status():
    try:
        return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def read_steps():
    """{script: {"last_run", "ok", "error", "last_good"}} across runs."""
    try:
        return json.loads(STEPS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def record_step(script, ok, error=None):
    steps = read_steps()
    s = steps.get(script, {})
    s.update({"last_run": now_text(), "ok": ok, "error": error})
    if ok:
        s["last_good"] = s["last_run"]
    steps[script] = s
    settings.write_text_safely(STEPS_FILE, json.dumps(steps, indent=1, ensure_ascii=False))
