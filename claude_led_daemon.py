#!/usr/bin/env python3
"""
claude_led_daemon.py

Long-running process that owns the USB serial connection to the Arduino.
Receives LED commands over a Unix domain socket (SOCK_DGRAM) and forwards
them to serial. Keeping the port open avoids the per-invocation DTR pulse
that resets the Arduino and causes the boot-animation flicker.

Auto-spawned by claude_led_bridge.py when not running.
"""

import os
import signal
import socket
import sys
import time
from pathlib import Path

import serial

SERIAL_PORT = os.environ.get("CLAUDE_LED_PORT", "/dev/tty.usbmodem11301")
BAUD = 9600
STATE_DIR = Path.home() / ".claude-led"
SOCK_PATH = str(STATE_DIR / "daemon.sock")
PID_FILE = STATE_DIR / "daemon.pid"
LOG_FILE = STATE_DIR / "daemon.log"


def log(msg):
    with LOG_FILE.open("a") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")


def already_running():
    if not PID_FILE.exists():
        return False
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, ValueError, PermissionError, OSError):
        return False


def cleanup(sock=None, ser=None):
    if sock is not None:
        try: sock.close()
        except Exception: pass
    try: os.unlink(SOCK_PATH)
    except OSError: pass
    if ser is not None:
        try: ser.close()
        except Exception: pass
    try: PID_FILE.unlink()
    except OSError: pass


def main():
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    if already_running():
        sys.exit(0)

    PID_FILE.write_text(str(os.getpid()))

    # Fresh socket
    if os.path.exists(SOCK_PATH):
        try: os.unlink(SOCK_PATH)
        except OSError: pass

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(SOCK_PATH)
    os.chmod(SOCK_PATH, 0o600)

    # Open serial (Arduino resets here — one time only)
    try:
        ser = serial.Serial(SERIAL_PORT, BAUD, timeout=1)
    except Exception as e:
        log(f"serial open failed: {e}")
        cleanup(sock=sock)
        sys.exit(1)

    time.sleep(2.5)  # wait for Arduino boot + Serial.begin settle
    banner = ser.read(200)
    log(f"started pid={os.getpid()} port={SERIAL_PORT} banner={banner!r}")

    # Graceful shutdown on SIGTERM/SIGINT
    stop = {"flag": False}
    def _sig(_n, _f): stop["flag"] = True
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    sock.settimeout(1.0)
    try:
        while not stop["flag"]:
            try:
                data, _ = sock.recvfrom(256)
            except socket.timeout:
                continue
            cmd = data.decode(errors="ignore").strip()
            if not cmd:
                continue
            if cmd == "_quit":
                log("quit requested")
                break
            try:
                ser.write((cmd + "\n").encode())
                ser.flush()
                log(f"-> {cmd}")
            except Exception as e:
                log(f"write error: {e}")
    finally:
        cleanup(sock=sock, ser=ser)


if __name__ == "__main__":
    main()
