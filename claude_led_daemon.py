#!/usr/bin/env python3
"""
claude_led_daemon.py

Long-running process that owns the USB serial connection to the Arduino.
Receives LED commands over a Unix domain socket (SOCK_DGRAM) and forwards
them to serial. Keeping the port open avoids the per-invocation DTR pulse
that resets the Arduino and causes the boot-animation flicker.

Auto-spawned by claude_led_bridge.py when not running.
"""

import glob
import os
import signal
import socket
import sys
import time
from pathlib import Path

import serial
from serial.tools import list_ports

BAUD = 9600


def find_serial_port():
    """Locate the Arduino serial device.

    Priority:
      1. $CLAUDE_LED_PORT if set and the device exists.
      2. pyserial's list_ports, preferring Arduino VIDs (2341, 2A03, 1A86, 0403).
      3. Glob of common macOS/Linux Arduino device paths.
    """
    env_port = os.environ.get("CLAUDE_LED_PORT")
    if env_port and os.path.exists(env_port):
        return env_port

    arduino_vids = {0x2341, 0x2A03, 0x1A86, 0x0403, 0x10C4}
    candidates = list(list_ports.comports())
    for p in candidates:
        if p.vid in arduino_vids:
            return p.device
    for p in candidates:
        dev = p.device or ""
        if "usbmodem" in dev or "usbserial" in dev or "ttyACM" in dev or "ttyUSB" in dev:
            return dev

    # /dev/cu.* first on macOS: /dev/tty.* opens block on DCD carrier detect.
    for pattern in ("/dev/cu.usbmodem*", "/dev/cu.usbserial*",
                    "/dev/tty.usbmodem*", "/dev/tty.usbserial*",
                    "/dev/ttyACM*", "/dev/ttyUSB*"):
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[0]

    return None
STATE_DIR = Path.home() / ".claude-led"
SOCK_PATH = str(STATE_DIR / "daemon.sock")
PID_FILE = STATE_DIR / "daemon.pid"
LOG_FILE = STATE_DIR / "daemon.log"


def log(msg):
    with LOG_FILE.open("a") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")


def open_serial():
    """Locate and open the Arduino serial port. Returns (ser, port) or (None, None)."""
    port = find_serial_port()
    if port is None:
        return None, None
    try:
        s = serial.Serial(port, BAUD, timeout=1)
    except Exception as e:
        log(f"serial open failed on {port}: {e}")
        return None, None
    try:
        time.sleep(2.5)  # Arduino resets on DTR pulse; wait for boot + Serial.begin
        banner = s.read(200)
    except (serial.SerialException, OSError) as e:
        log(f"serial banner read failed on {port}: {e}")
        try: s.close()
        except Exception: pass
        return None, None
    log(f"serial opened port={port} banner={banner!r}")
    return s, port


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
    ser, serial_port = open_serial()
    if ser is None:
        log("no Arduino serial device found at startup (checked $CLAUDE_LED_PORT, pyserial, /dev globs)")
        cleanup(sock=sock)
        sys.exit(1)
    log(f"started pid={os.getpid()} port={serial_port}")

    # Graceful shutdown on SIGTERM/SIGINT
    stop = {"flag": False}
    def _sig(_n, _f): stop["flag"] = True
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    last_reopen_attempt = 0.0
    REOPEN_COOLDOWN = 2.0

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

            if ser is None:
                now = time.time()
                if now - last_reopen_attempt < REOPEN_COOLDOWN:
                    log(f"dropped {cmd!r}: serial closed, reopen cooldown")
                    continue
                last_reopen_attempt = now
                ser, serial_port = open_serial()
                if ser is None:
                    log(f"dropped {cmd!r}: reopen failed")
                    continue

            try:
                ser.write((cmd + "\n").encode())
                ser.flush()
                log(f"-> {cmd}")
            except (serial.SerialException, OSError) as e:
                # Stale FD after USB replug — drop it, next command triggers reopen.
                log(f"write error: {e}; closing port, will reopen")
                try: ser.close()
                except Exception: pass
                ser = None
                last_reopen_attempt = 0.0
    finally:
        cleanup(sock=sock, ser=ser)


if __name__ == "__main__":
    main()
