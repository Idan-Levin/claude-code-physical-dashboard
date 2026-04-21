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

State access is serialized via fcntl.flock and writes are atomic
(tmp file + os.replace) so concurrent async hook invocations cannot
corrupt state.json.
"""

import contextlib
import errno
import fcntl
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# --- Config ---------------------------------------------------------------

NUM_SLOTS = 4
STATE_DIR = Path.home() / ".claude-led"
STATE_FILE = STATE_DIR / "state.json"
STATE_LOCK = STATE_DIR / "state.lock"
DAEMON_LOCK = STATE_DIR / "daemon.lock"
LOG_FILE = STATE_DIR / "bridge.log"
SOCK_PATH = str(STATE_DIR / "daemon.sock")
DAEMON_SCRIPT = Path(__file__).resolve().parent / "claude_led_daemon.py"

EVICT_AFTER_SEC = 30 * 60           # evict a slot owner quiet for 30 min
PRUNE_LAST_SEEN_AFTER_SEC = 24 * 3600
LATE_EVENT_GRACE_SEC = 0.3          # retry window for non-SessionStart events

# --- Filesystem setup -----------------------------------------------------

def _ensure_state_dir():
    """Create STATE_DIR with 0o700; chmod existing dirs in case perms drifted."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(STATE_DIR, 0o700)
    except OSError:
        pass


def _secure_write(path: Path, data: str):
    """Atomic write with 0o600 perms via tmp file in the same directory."""
    _ensure_state_dir()
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        os.write(fd, data.encode())
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def log(msg):
    try:
        _ensure_state_dir()
        with LOG_FILE.open("a") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
        try:
            os.chmod(LOG_FILE, 0o600)
        except OSError:
            pass
    except Exception:
        # Logging must never break the hook.
        pass

# --- State helpers --------------------------------------------------------

@contextlib.contextmanager
def state_lock():
    """Inter-process exclusive lock around load→mutate→save of state.json."""
    _ensure_state_dir()
    fd = os.open(str(STATE_LOCK), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
        yield
    finally:
        try: fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception: pass
        os.close(fd)


def _blank_state():
    return {"slots": [None] * NUM_SLOTS, "last_seen": {}}


def load_state():
    if not STATE_FILE.exists():
        return _blank_state()
    try:
        with STATE_FILE.open() as f:
            s = json.load(f)
        slots = s.get("slots", [])
        while len(slots) < NUM_SLOTS:
            slots.append(None)
        s["slots"] = slots[:NUM_SLOTS]
        if not isinstance(s.get("last_seen"), dict):
            s["last_seen"] = {}
        return s
    except Exception:
        return _blank_state()


def save_state(state):
    _prune_last_seen(state)
    _secure_write(STATE_FILE, json.dumps(state, indent=2))


def _prune_last_seen(state):
    """Drop last_seen entries not in slots and older than the cutoff."""
    cutoff = time.time() - PRUNE_LAST_SEEN_AFTER_SEC
    in_slots = {sid for sid in state["slots"] if sid}
    state["last_seen"] = {
        sid: ts for sid, ts in state["last_seen"].items()
        if sid in in_slots or ts >= cutoff
    }


def slot_for(session_id, state, assign=True):
    if session_id in state["slots"]:
        return state["slots"].index(session_id) + 1
    if not assign:
        return None
    now = time.time()
    # Missing last_seen → treat as recent so we don't wrongly evict.
    for i, sid in enumerate(state["slots"]):
        if sid and now - state["last_seen"].get(sid, now) > EVICT_AFTER_SEC:
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
    """Daemon is alive iff it holds DAEMON_LOCK exclusively."""
    _ensure_state_dir()
    try:
        fd = os.open(str(DAEMON_LOCK), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # We got the lock → no daemon owns it. Release and report absent.
        try: fcntl.flock(fd, fcntl.LOCK_UN)
        except Exception: pass
        return False
    except BlockingIOError:
        # Someone else (the daemon) holds it.
        return True
    finally:
        os.close(fd)


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
        if os.path.exists(SOCK_PATH) and daemon_alive():
            return True
        time.sleep(0.1)
    return os.path.exists(SOCK_PATH)


def send(slot, state_name):
    """Send one LED command. Retries once with a respawn on socket failure."""
    payload = f"{slot}:{state_name}".encode()
    for attempt in (1, 2):
        if not daemon_alive() or not os.path.exists(SOCK_PATH):
            if not spawn_daemon():
                log("daemon failed to start")
                return
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
                s.sendto(payload, SOCK_PATH)
            return
        except (FileNotFoundError, ConnectionRefusedError, OSError) as e:
            if attempt == 2:
                log(f"sendto error (final): {e}")
                return
            log(f"sendto error (retrying): {e}")
            # Fall through to respawn attempt.

# --- Main -----------------------------------------------------------------

EVENT_TO_STATE = {
    "SessionStart": "idle",
    "SessionEnd": "off",
    "UserPromptSubmit": "working",
    "PreToolUse": "working",
    "PostToolUse": "working",
    "Stop": "waiting",
    "Notification": "alert",
}


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception as e:
        log(f"bad stdin: {e}")
        sys.exit(0)

    event = payload.get("hook_event_name") or os.environ.get("CLAUDE_HOOK_EVENT", "")
    session_id = payload.get("session_id", "unknown")

    led_state = EVENT_TO_STATE.get(event)
    if led_state is None:
        log(f"ignoring event: {event}")
        sys.exit(0)

    # Only SessionStart is allowed to claim a new slot. Any other event for
    # a session that doesn't already own a slot is stale (e.g. a late
    # Notification firing after SessionEnd) and must not re-animate the LED.
    # Under async hooks, though, non-SessionStart events can *race ahead* of
    # SessionStart, so we give them a short grace window to let SessionStart
    # land before giving up.
    assign = event == "SessionStart"
    deadline = time.time() + (0 if assign else LATE_EVENT_GRACE_SEC)

    while True:
        with state_lock():
            state = load_state()
            state["last_seen"][session_id] = time.time()
            slot = slot_for(session_id, state, assign=assign)
            if slot is not None:
                log(f"slot {slot} <- {led_state} ({event}, session {session_id[:8]})")
                if event == "SessionEnd" and session_id in state["slots"]:
                    i = state["slots"].index(session_id)
                    state["slots"][i] = None
                save_state(state)
                break
            # No slot. If this is a non-SessionStart event that might just be
            # racing ahead of SessionStart, persist last_seen and retry briefly.
            save_state(state)
        if time.time() >= deadline:
            log(f"no slot for session {session_id[:8]} ({event}) — ignoring")
            sys.exit(0)
        time.sleep(0.1)

    # Socket IO happens outside the state lock to avoid stalling other hooks.
    send(slot, led_state)
    sys.exit(0)


if __name__ == "__main__":
    main()
