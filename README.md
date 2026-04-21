# Claude Code LED Dashboard

A physical status board for [Claude Code](https://claude.com/claude-code) built on an Arduino Mega. Each of 4 "slots" has a red + green LED pair that reflects the live state of a Claude Code session: idle, working, waiting for you, or needing your attention.

```
┌─────────────────────────────────────────┐
│  [G][R]  [G][R]  [G][R]  [G][R]         │
│  slot 1  slot 2  slot 3  slot 4         │
└─────────────────────────────────────────┘
```

## How it works

1. You run `claude` in a terminal.
2. Claude Code fires hook events (`SessionStart`, `PreToolUse`, `Stop`, `Notification`, `SessionEnd`, ...) — configured in `~/.claude/settings.json`.
3. Each hook pipes its JSON payload into `claude_led_bridge.py`.
4. The bridge maps the event to an LED state, assigns the session a slot (1–4, first-come-first-served), and sends `<slot>:<state>\n` over USB serial to the Arduino.
5. The Arduino sketch renders the state on the LEDs.

Up to 4 simultaneous Claude Code sessions can be tracked at once.

## LED states

| State     | LEDs                  | Meaning                                   | Triggered by        |
|-----------|-----------------------|-------------------------------------------|---------------------|
| `off`     | both off              | no session                                | `SessionEnd`        |
| `idle`    | both blink slowly     | session open, nothing happening           | `SessionStart`      |
| `working` | green solid           | Claude is thinking / using tools          | `UserPromptSubmit`, `PreToolUse`, `PostToolUse` |
| `waiting` | red solid             | turn ended, waiting on your reply         | `Stop`              |
| `alert`   | red blinks fast       | notification — needs input NOW            | `Notification`      |

## Hardware

- **Board**: Arduino Mega 2560 (pins 22–29 are digital-only, no PWM needed).
- **LEDs**: 4 × red, 4 × green (8 total), plus current-limiting resistors (≈220 Ω).
- **Pin mapping** (set in the sketch):

  | Slot | Green pin | Red pin |
  |------|-----------|---------|
  | 1    | 22        | 23      |
  | 2    | 24        | 25      |
  | 3    | 26        | 27      |
  | 4    | 28        | 29      |

Wiring per LED: `pin → 220Ω → LED anode → LED cathode → GND`.

## Files

| Path                          | What it is                                                    |
|-------------------------------|---------------------------------------------------------------|
| `claude_led_dashboard.ino`    | Arduino sketch — listens on serial, drives the LEDs           |
| `claude_led_bridge.py`        | Python hook script — stdin JSON → serial command              |
| `settings.json`               | Reference Claude Code hooks config (merge into `~/.claude/settings.json`) |
| `.venv/`                      | Local venv holding `pyserial` (not committed)                 |

Runtime state lives outside the repo:

- `~/.claude-led/state.json` — session-ID → slot mapping
- `~/.claude-led/bridge.log` — event log (useful for debugging)

## Setup

### 1. Flash the sketch

Open `claude_led_dashboard.ino` in the Arduino IDE, select your Mega and port, and upload. On boot, each LED flashes briefly.

### 2. Find the serial port

```bash
# macOS: prefer /dev/cu.* (non-blocking opens; /dev/tty.* blocks on DCD)
ls /dev/cu.usbmodem* /dev/cu.usbserial* 2>/dev/null
# Linux:
ls /dev/ttyACM* /dev/ttyUSB* 2>/dev/null
```

Copy the path (e.g. `/dev/cu.usbmodem11301` on macOS, `/dev/ttyACM0` on Linux).
If `$CLAUDE_LED_PORT` isn't set, the daemon auto-detects via pyserial VIDs
and falls back to globbing these paths.

### 3. Install the bridge

```bash
git clone <this-repo> ~/claude-led-dashboard
cd ~/claude-led-dashboard

python3 -m venv .venv
.venv/bin/pip install pyserial
```

> On macOS with Homebrew Python you **must** use a venv — Homebrew's Python blocks system-wide `pip install` (PEP 668).

### 4. Configure the port

```bash
echo 'export CLAUDE_LED_PORT=/dev/tty.usbmodem11301' >> ~/.zshrc
source ~/.zshrc
```

Replace the path with yours from step 2.

### 5. Close the Arduino Serial Monitor

Only one process can hold the serial port. If the Serial Monitor is open, the bridge can't talk to the Arduino.

### 6. Test the hardware

```bash
echo '{"hook_event_name":"SessionStart","session_id":"test-1"}' \
  | ~/claude-led-dashboard/.venv/bin/python ~/claude-led-dashboard/claude_led_bridge.py
# → slot 1 starts idle-blinking

echo '{"hook_event_name":"PreToolUse","session_id":"test-1"}' \
  | ~/claude-led-dashboard/.venv/bin/python ~/claude-led-dashboard/claude_led_bridge.py
# → slot 1 solid green

echo '{"hook_event_name":"Stop","session_id":"test-1"}' \
  | ~/claude-led-dashboard/.venv/bin/python ~/claude-led-dashboard/claude_led_bridge.py
# → slot 1 solid red

echo '{"hook_event_name":"Notification","session_id":"test-1"}' \
  | ~/claude-led-dashboard/.venv/bin/python ~/claude-led-dashboard/claude_led_bridge.py
# → slot 1 blinking red fast

echo '{"hook_event_name":"SessionEnd","session_id":"test-1"}' \
  | ~/claude-led-dashboard/.venv/bin/python ~/claude-led-dashboard/claude_led_bridge.py
# → slot 1 off
```

### 7. Wire it into Claude Code

Merge the contents of `settings.json` into `~/.claude/settings.json`. If you don't have one yet:

```bash
mkdir -p ~/.claude
cp settings.json ~/.claude/settings.json
```

If you already have `~/.claude/settings.json`, add the `hooks` block manually.

### 8. Reset slot state and go

```bash
rm -f ~/.claude-led/state.json
claude   # in any directory — slot 1 lights up
```

Open up to 4 terminals; each new `claude` session claims the next free slot.

## Troubleshooting

Read the log:

```bash
tail -f ~/.claude-led/bridge.log
```

Common failure modes:

| Symptom                                      | Fix                                                                 |
|----------------------------------------------|---------------------------------------------------------------------|
| `serial error: could not open port ...`      | Wrong `CLAUDE_LED_PORT`, or Serial Monitor still open               |
| `serial error: Resource busy`                | Another process (another Claude session, or Serial Monitor) is holding the port |
| Log is empty                                 | `pyserial` not installed in the venv, or venv path wrong in `settings.json` |
| Hook runs but LEDs don't move                | Arduino may have frozen — unplug / replug USB                       |
| Wrong port reported in log (e.g. `usbmodem1101`) | Claude was launched before `CLAUDE_LED_PORT` was exported. Quit and relaunch |

Reset everything:

```bash
rm -f ~/.claude-led/state.json
```

That just clears slot assignments; it doesn't touch the Arduino or the config.

## Command reference

The Arduino accepts these over serial (9600 baud, newline-terminated):

| Command          | Effect                                           |
|------------------|--------------------------------------------------|
| `<slot>:off`     | Slot goes dark                                   |
| `<slot>:idle`    | Slot both-LEDs slow blink                        |
| `<slot>:working` | Slot solid green                                 |
| `<slot>:waiting` | Slot solid red                                   |
| `<slot>:alert`   | Slot red fast blink                              |
| `ping`           | Arduino replies `pong`                           |
| `reset`          | All 4 slots → off                                |

Where `<slot>` is `1`–`4`.

## License

MIT.
