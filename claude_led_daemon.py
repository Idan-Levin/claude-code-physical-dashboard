#!/usr/bin/env python3
"""
claude_led_daemon.py

Long-running process that owns the USB serial connection to the Arduino.
Receives LED commands over a Unix domain socket (SOCK_DGRAM) and forwards
them to serial. Keeping the port open avoids the per-invocation DTR pulse
that resets the Arduino and causes the boot-animation flicker.

Auto-spawned by claude_led_bridge.py when not running.

Singleton is enforced with fcntl.flock on ~/.claude-led/daemon.lock held
for the daemon's lifetime — prevents double-spawn races that PID files
alone cannot (stale PID reuse, two bridges both seeing "no daemon").
"""

import errno
import fcntl
import glob
import os
import signal
import socket
import stat
import sys
import time
from pathlib import Path

import serial
from serial.tools import list_ports

BAUD = 9600

STATE_DIR = Path.home() / ".claude-led"
SOCK_PATH = str(STATE_DIR / "daemon.sock")
PID_FILE = STATE_DIR / "daemon.pid"
LOCK_FILE = STATE_DIR / "daemon.lock"
LOG_FILE = STATE_DIR / "daemon.log"


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
        if p.vid in arduino_vids and p.device:
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


def _ensure_state_dir():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(STATE_DIR, 0o700)
    except OSError:
        pass


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
        # Best effort: if disk is full or perms are wrong, fall back to stderr.
        try:
            sys.stderr.write(f"daemon log failed: {msg}\n")
        except Exception:
            pass


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


def acquire_singleton():
    """Return an open lock fd on success, or None if another daemon is running."""
    _ensure_state_dir()
    fd = os.open(str(LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    try:
        os.fchmod(fd, 0o600)
    except OSError:
        pass
    # Write our PID into the lock file for debugging; held fd keeps the lock.
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
    except OSError:
        pass
    return fd


def _is_our_socket(path):
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return False
    return stat.S_ISSOCK(st.st_mode)


def _safe_unlink_socket(path):
    """Only unlink SOCK_PATH if it's a real socket (not a symlink or regular file)."""
    if _is_our_socket(path):
        try: os.unlink(path)
        except OSError: pass


def cleanup(sock=None, ser=None):
    if sock is not None:
        try: sock.close()
        except Exception: pass
    _safe_unlink_socket(SOCK_PATH)
    if ser is not None:
        try: ser.close()
        except Exception: pass
    try: PID_FILE.unlink()
    except OSError: pass


def main():
    _ensure_state_dir()

    lock_fd = acquire_singleton()
    if lock_fd is None:
        # Another daemon already holds the lock; nothing to do.
        sys.exit(0)

    # Write PID for debugging/observability only (not used for aliveness).
    try:
        PID_FILE.write_text(str(os.getpid()))
        os.chmod(PID_FILE, 0o600)
    except OSError:
        pass

    # We hold the lock, so any stale socket at SOCK_PATH is ours to clean up.
    _safe_unlink_socket(SOCK_PATH)

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        sock.bind(SOCK_PATH)
    except OSError as e:
        log(f"socket bind failed on {SOCK_PATH}: {e}")
        sock.close()
        try: PID_FILE.unlink()
        except OSError: pass
        sys.exit(1)
    try:
        os.chmod(SOCK_PATH, 0o600)
    except OSError:
        pass

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
        # Releasing the flock happens implicitly on process exit when lock_fd closes.
        try: os.close(lock_fd)
        except Exception: pass


if __name__ == "__main__":
    main()
