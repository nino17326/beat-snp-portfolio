# ============================================================
#  lookup_server.py  -  keeps the lookup page running in the background
#
#      py lookup_server.py start    start it hidden (never twice)
#      py lookup_server.py stop     stop it
#      py lookup_server.py status   is it running, and on which port?
#
#  It runs with no window and keeps running after the script that
#  started it ends. run_all.py starts it at the end of every update.
#  In <data folder>\output\:
#    lookup_port.txt  the port it uses (8501, or a free one if 8501 is
#                     taken by something else); the dashboard reads it
#    lookup.pid       its process ID (so it can be stopped)
#    lookup.log       what it printed
# ============================================================

import os
import sys
import time
import webbrowser

import procs
import settings

DEFAULT_PORT = 8501
PORT_FILE = settings.OUTPUT_FOLDER / "lookup_port.txt"
PID_FILE = settings.OUTPUT_FOLDER / "lookup.pid"
LOG_FILE = settings.OUTPUT_FOLDER / "lookup.log"


def saved_port():
    try:
        return int(PORT_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return DEFAULT_PORT


def saved_pid():
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def running():
    """The port of OUR lookup if it's running and answering, else None."""
    port, pid = saved_port(), saved_pid()
    if pid and procs.pid_alive(pid) and procs.streamlit_answers(port):
        return port
    return None


def reserve_port():
    """Decide the lookup's port and save it, BEFORE the scanner report and
    the dashboard write their links: our running lookup's port; else the
    saved port if it's free; else 8501 if free; else the next free one."""
    port = running()
    if port is None:
        saved = saved_port() if PORT_FILE.exists() else None
        if saved and procs.port_free(saved):
            port = saved
        elif procs.port_free(DEFAULT_PORT):
            port = DEFAULT_PORT
        else:
            port = procs.first_free_port(DEFAULT_PORT + 1)
    settings.OUTPUT_FOLDER.mkdir(parents=True, exist_ok=True)
    settings.write_text_safely(PORT_FILE, str(port))
    return port


def start(open_browser=False, wait_seconds=40):
    """Start the lookup hidden, unless it's already running. Returns the port."""
    port = running()
    if port:
        print(f"Lookup already running on http://localhost:{port}")
    else:
        old = saved_pid()
        if old and procs.pid_alive(old):                 # ours, but not answering: restart it
            procs.stop_pid(old)
        port = reserve_port()                            # 8501, or a free one if 8501 is taken
        with open(LOG_FILE, "a", encoding="utf-8") as log:
            log.write(f"\n===== {procs.now_text()} starting lookup on port {port}\n")
        pid = procs.start_hidden(
            [procs.windowless_python(), "-m", "streamlit", "run", str(settings.CODE_FOLDER / "lookup.py"),
             "--server.port", str(port), "--server.headless", "true", "--browser.gatherUsageStats", "false",
             "--server.address", "127.0.0.1"],          # this PC only: the page can edit rules.toml
            LOG_FILE, cwd=settings.CODE_FOLDER,
            env={**os.environ, "BEAT_SNP_DATA_DIR": "" if settings.DEMO else str(settings.DATA_FOLDER)})
        settings.write_text_safely(PID_FILE, str(pid))
        settings.write_text_safely(PORT_FILE, str(port))
        deadline = time.time() + wait_seconds
        while time.time() < deadline and not procs.streamlit_answers(port):
            time.sleep(0.5)
        state = "running" if procs.streamlit_answers(port) else "starting (not answering yet - see lookup.log)"
        note = "" if port == DEFAULT_PORT else f" ({DEFAULT_PORT} is used by something else)"
        print(f"Lookup {state} on http://localhost:{port}{note}")
    if open_browser:
        webbrowser.open(f"http://localhost:{port}/")
    return port


def stop():
    pid = saved_pid()
    if pid and procs.pid_alive(pid):
        procs.stop_pid(pid)
        print(f"Lookup stopped (process {pid}).")
    else:
        print("Lookup wasn't running.")
    try:
        PID_FILE.unlink()
    except OSError:
        pass


def status():
    port = running()
    print(f"Lookup running on http://localhost:{port}" if port else "Lookup is not running.")


if __name__ == "__main__":
    if sys.stdout:                                       # None under pythonw (no console)
        sys.stdout.reconfigure(encoding="utf-8")
    command = sys.argv[1] if len(sys.argv) > 1 else "status"
    {"start": lambda: start(open_browser="--open" in sys.argv), "stop": stop, "status": status}.get(
        command, lambda: print("use: start | stop | status"))()
