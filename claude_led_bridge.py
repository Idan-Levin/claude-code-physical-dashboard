#!/usr/bin/env python3
"""
claude_led_bridge.py

Reads a Claude Code hook event from stdin and forwards the right command
to the LED daemon over a Unix domain socket. Configured via Claude Code
hooks in settings.json.

The daemon (claude_led_daemon.py) owns the USB serial connection so we
don't reset the Arduino on every hook invocation. This script auto-spawns
the daemon if it isn't already running.

Maintains a tiny state file at ~/.claude-led/state.json that maps
session_id -> slot number (1..NUM_SLOTS), first-come-first-served.
Sessions that arrive when all slots are full are ignored.
"""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

# --- Config ---------------------------------------------------------------

NUM_SLOTS = 4
STATE_DIR = Path.home() / ".claude-led"
STATE_FILE = STATE_DIR / "state.json"
LOG_FILE = STATE_DIR / "bridge.log"
SOCK_PATH = str(STATE_DIR / "daemon.sock")
PID_FILE = STATE_DIR / "daemon.pid"
DAEMON_SCRIPT = Path(__file__).resolve().parent / "claude_led_daemon.py"

# --- State helpers --------------------------------------------------------

def load_state():
    if not STATE_FILE.exists():
        return {"slots": [None] * NUM_SLOTS, "last_seen": {}}
    try:
        with STATE_FILE.open() as f:
            s = json.load(f)
        while len(s.get("slots", [])) < NUM_SLOTS:
            s["slots"].append(None)
        return s
    except Exception:
        return {"slots": [None] * NUM_SLOTS, "last_seen": {}}

def save_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with STATE_FILE.open("w") as f:
        json.dump(state, f, indent=2)

def log(msg):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")

def slot_for(session_id, state, assign=True):
    if session_id in state["slots"]:
        return state["slots"].index(session_id) + 1
    if not assign:
        return None
    now = time.time()
    for i, sid in enumerate(state["slots"]):
        if sid and now - state["last_seen"].get(sid, 0) > 30 * 60:
            log(f"evicting stale session {sid[:8]} from slot {i + 1}")
            send(i + 1, "off")
            state["slots"][i] = None
    for i, sid in enumerate(state["slots"]):
        if sid is None:
            state["slots"][i] = session_id
            return i + 1
    return None

# --- Daemon comms ---------------------------------------------------------

def daemon_alive():
    if not PID_FILE.exists():
        return False
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return os.path.exists(SOCK_PATH)
    except Exception:
        return False

def spawn_daemon():
    try:
        subprocess.Popen(
            [sys.executable, str(DAEMON_SCRIPT)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=os.environ.copy(),
        )
    except Exception as e:
        log(f"spawn error: {e}")
        return False
    # Daemon binds socket before the 2.5s Arduino boot wait,
    # so the socket should appear quickly (under ~200ms).
    for _ in range(50):
        if os.path.exists(SOCK_PATH):
            return True
        time.sleep(0.1)
    return os.path.exists(SOCK_PATH)

def send(slot, state_name):
    if not daemon_alive():
        if not spawn_daemon():
            log("daemon failed to start")
            return
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.sendto(f"{slot}:{state_name}".encode(), SOCK_PATH)
    except Exception as e:
        log(f"sendto error: {e}")

# --- Main -----------------------------------------------------------------

def main():
    try:
        payload = json.load(sys.stdin)
    except Exception as e:
        log(f"bad stdin: {e}")
        sys.exit(0)

    event = payload.get("hook_event_name") or os.environ.get("CLAUDE_HOOK_EVENT", "")
    session_id = payload.get("session_id", "unknown")

    state = load_state()
    state["last_seen"][session_id] = time.time()

    led_state = None
    if event == "SessionStart":
        led_state = "idle"
    elif event == "SessionEnd":
        led_state = "off"
    elif event == "UserPromptSubmit":
        led_state = "working"
    elif event == "PreToolUse" or event == "PostToolUse":
        led_state = "working"
    elif event == "Stop":
        led_state = "waiting"
    elif event == "Notification":
        led_state = "alert"
    else:
        log(f"ignoring event: {event}")
        save_state(state)
        sys.exit(0)

    # Only SessionStart is allowed to claim a new slot. Any other event for
    # a session that doesn't already own a slot is stale (e.g. a late
    # Notification firing after SessionEnd) and must not re-animate the LED.
    assign = event == "SessionStart"
    slot = slot_for(session_id, state, assign=assign)
    if slot is None:
        log(f"no slot for session {session_id[:8]} ({event}) — ignoring")
        save_state(state)
        sys.exit(0)

    log(f"slot {slot} <- {led_state} ({event}, session {session_id[:8]})")
    send(slot, led_state)

    if event == "SessionEnd" and session_id in state["slots"]:
        i = state["slots"].index(session_id)
        state["slots"][i] = None

    save_state(state)
    sys.exit(0)

if __name__ == "__main__":
    main()
