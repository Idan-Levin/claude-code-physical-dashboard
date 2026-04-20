#!/usr/bin/env python3
"""
claude_led_bridge.py

Reads a Claude Code hook event from stdin and sends the right command
to the Arduino over USB serial. Configured via .claude/settings.json hooks.

Maintains a tiny state file at ~/.claude-led/state.json that maps
session_id -> slot number (1..NUM_SLOTS), on a first-come-first-served
basis. Sessions that arrive when all slots are full are ignored.
"""

import json
import os
import sys
import time
from pathlib import Path

import serial  # pip install pyserial

# --- Config ---------------------------------------------------------------

NUM_SLOTS = 4
SERIAL_PORT = os.environ.get("CLAUDE_LED_PORT", "/dev/tty.usbmodem1101")
BAUD = 9600
STATE_DIR = Path.home() / ".claude-led"
STATE_FILE = STATE_DIR / "state.json"
LOG_FILE = STATE_DIR / "bridge.log"

# --- State helpers --------------------------------------------------------

def load_state():
    if not STATE_FILE.exists():
        return {"slots": [None] * NUM_SLOTS, "last_seen": {}}
    try:
        with STATE_FILE.open() as f:
            s = json.load(f)
        # Backfill if length changed
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
    """Return 1..NUM_SLOTS for this session, or None if full and not assigning."""
    if session_id in state["slots"]:
        return state["slots"].index(session_id) + 1
    if not assign:
        return None
    # Reap stale slots: any session not seen in 6 hours gets evicted
    now = time.time()
    for i, sid in enumerate(state["slots"]):
        if sid and now - state["last_seen"].get(sid, 0) > 6 * 3600:
            state["slots"][i] = None
    # Assign first free slot
    for i, sid in enumerate(state["slots"]):
        if sid is None:
            state["slots"][i] = session_id
            return i + 1
    return None

# --- Serial ---------------------------------------------------------------

def send(slot, state_name):
    try:
        with serial.Serial(SERIAL_PORT, BAUD, timeout=1) as ser:
            # Arduino resets on connect; give it a moment
            time.sleep(2)
            ser.write(f"{slot}:{state_name}\n".encode())
            ser.flush()
    except Exception as e:
        log(f"serial error: {e}")

# --- Main -----------------------------------------------------------------

def main():
    try:
        payload = json.load(sys.stdin)
    except Exception as e:
        log(f"bad stdin: {e}")
        sys.exit(0)  # never block Claude

    event = payload.get("hook_event_name") or os.environ.get("CLAUDE_HOOK_EVENT", "")
    session_id = payload.get("session_id", "unknown")

    state = load_state()
    state["last_seen"][session_id] = time.time()

    # Figure out which LED state this event maps to
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

    assign = led_state != "off"
    slot = slot_for(session_id, state, assign=assign)
    if slot is None:
        log(f"no slot available for session {session_id[:8]} ({event})")
        save_state(state)
        sys.exit(0)

    log(f"slot {slot} <- {led_state} ({event}, session {session_id[:8]})")
    send(slot, led_state)

    # On SessionEnd, free the slot AFTER sending "off" so the Arduino actually turns it off
    if event == "SessionEnd" and session_id in state["slots"]:
        i = state["slots"].index(session_id)
        state["slots"][i] = None

    save_state(state)
    sys.exit(0)

if __name__ == "__main__":
    main()
