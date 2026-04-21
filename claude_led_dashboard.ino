/*
 * Claude Code LED Dashboard (pins 22-29 version)
 *
 * Listens on USB serial for commands, drives 4 session slots,
 * each with a red + green LED.
 *
 * Command format (newline-terminated):
 *   <slot>:<state>\n
 *
 * Slot:  1..4
 * State: off | idle | working | waiting | alert
 *
 *   off      -> both LEDs off              (no session)
 *   idle     -> both LEDs blinking slowly  (session open, nothing happening)
 *   working  -> green on                   (Claude is thinking/using tools)
 *   waiting  -> red on                     (turn ended, waiting on you)
 *   alert    -> red blinking fast          (notification: needs input NOW)
 */

const int NUM_SLOTS = 4;

// Pins: {greenPin, redPin} per slot.
// Pins 22-29 on the Mega are digital-only (no PWM).
const int pins[NUM_SLOTS][2] = {
  {22, 23},  // slot 1
  {24, 25},  // slot 2
  {26, 27},  // slot 3
  {28, 29}   // slot 4
};

enum State { OFF, IDLE, WORKING, WAITING, ALERT };
State slotState[NUM_SLOTS];

unsigned long lastFastBlink = 0;
unsigned long lastSlowBlink = 0;
bool fastBlinkOn = false;
bool slowBlinkOn = false;
const unsigned long FAST_BLINK_MS = 400;
const unsigned long SLOW_BLINK_MS = 1500;

void setup() {
  Serial.begin(9600);
  for (int i = 0; i < NUM_SLOTS; i++) {
    pinMode(pins[i][0], OUTPUT);
    pinMode(pins[i][1], OUTPUT);
    slotState[i] = OFF;
  }
  // Boot animation
  for (int i = 0; i < NUM_SLOTS; i++) {
    digitalWrite(pins[i][0], HIGH);
    delay(80);
    digitalWrite(pins[i][0], LOW);
    digitalWrite(pins[i][1], HIGH);
    delay(80);
    digitalWrite(pins[i][1], LOW);
  }
  Serial.println("ready");
}

void renderSlot(int i) {
  int greenPin = pins[i][0];
  int redPin   = pins[i][1];

  switch (slotState[i]) {
    case OFF:
      digitalWrite(greenPin, LOW);
      digitalWrite(redPin, LOW);
      break;
    case IDLE:
      digitalWrite(greenPin, slowBlinkOn ? HIGH : LOW);
      digitalWrite(redPin, slowBlinkOn ? HIGH : LOW);
      break;
    case WORKING:
      digitalWrite(redPin, LOW);
      digitalWrite(greenPin, HIGH);
      break;
    case WAITING:
      digitalWrite(greenPin, LOW);
      digitalWrite(redPin, HIGH);
      break;
    case ALERT:
      digitalWrite(greenPin, LOW);
      digitalWrite(redPin, fastBlinkOn ? HIGH : LOW);
      break;
  }
}

void renderAll() {
  for (int i = 0; i < NUM_SLOTS; i++) renderSlot(i);
}

// parseState returns true on success; writes result to *out. Unknown state
// is rejected (returns false) rather than silently mapped to OFF, so a
// malformed command doesn't quietly dark a live slot.
bool parseState(const char* s, State* out) {
  if (strcmp(s, "off") == 0)     { *out = OFF;     return true; }
  if (strcmp(s, "idle") == 0)    { *out = IDLE;    return true; }
  if (strcmp(s, "working") == 0) { *out = WORKING; return true; }
  if (strcmp(s, "waiting") == 0) { *out = WAITING; return true; }
  if (strcmp(s, "alert") == 0)   { *out = ALERT;   return true; }
  return false;
}

// trim leading/trailing whitespace in-place. Returns pointer to trimmed start.
char* trimInPlace(char* s) {
  while (*s == ' ' || *s == '\t') s++;
  size_t len = strlen(s);
  while (len > 0 && (s[len - 1] == ' ' || s[len - 1] == '\t')) {
    s[--len] = '\0';
  }
  return s;
}

void handleCommand(char* cmd) {
  cmd = trimInPlace(cmd);
  if (*cmd == '\0') return;

  if (strcmp(cmd, "ping") == 0) {
    Serial.println("pong");
    return;
  }

  if (strcmp(cmd, "reset") == 0) {
    for (int i = 0; i < NUM_SLOTS; i++) slotState[i] = OFF;
    renderAll();
    Serial.println("ok reset");
    return;
  }

  if (strcmp(cmd, "test") == 0) {
    for (int i = 0; i < NUM_SLOTS; i++) slotState[i] = OFF;
    renderAll();
    for (int i = 0; i < NUM_SLOTS; i++) {
      digitalWrite(pins[i][0], HIGH);
      delay(600);
      digitalWrite(pins[i][0], LOW);
      digitalWrite(pins[i][1], HIGH);
      delay(600);
      digitalWrite(pins[i][1], LOW);
      delay(300);
    }
    Serial.println("ok test");
    return;
  }

  char* colon = strchr(cmd, ':');
  if (colon == NULL || colon == cmd) {
    Serial.print("err bad cmd: ");
    Serial.println(cmd);
    return;
  }

  // Validate the slot field is all digits before atoi (so "1abc" doesn't
  // silently parse as 1).
  for (char* p = cmd; p < colon; p++) {
    if (*p < '0' || *p > '9') {
      Serial.print("err bad slot: ");
      Serial.println(cmd);
      return;
    }
  }

  *colon = '\0';
  int slot = atoi(cmd);
  const char* stateStr = colon + 1;

  if (slot < 1 || slot > NUM_SLOTS) {
    Serial.print("err bad slot: ");
    Serial.println(slot);
    return;
  }

  State parsed;
  if (!parseState(stateStr, &parsed)) {
    Serial.print("err bad state: ");
    Serial.println(stateStr);
    return;
  }

  slotState[slot - 1] = parsed;
  renderSlot(slot - 1);
  Serial.print("ok ");
  Serial.print(slot);
  Serial.print(":");
  Serial.println(stateStr);
}

void loop() {
  unsigned long now = millis();

  if (now - lastFastBlink >= FAST_BLINK_MS) {
    lastFastBlink = now;
    fastBlinkOn = !fastBlinkOn;
    for (int i = 0; i < NUM_SLOTS; i++) {
      if (slotState[i] == ALERT) renderSlot(i);
    }
  }

  if (now - lastSlowBlink >= SLOW_BLINK_MS) {
    lastSlowBlink = now;
    slowBlinkOn = !slowBlinkOn;
    for (int i = 0; i < NUM_SLOTS; i++) {
      if (slotState[i] == IDLE) renderSlot(i);
    }
  }

  // Fixed char buffer instead of dynamic String to avoid AVR heap fragmentation.
  // Overflow (line > 64 bytes) latches until the next newline, so we don't
  // silently act on a truncated command.
  static char buf[65];
  static uint8_t buflen = 0;
  static bool overflowed = false;

  while (Serial.available() > 0) {
    char c = Serial.read();
    if (c == '\n') {
      if (overflowed) {
        Serial.println("err overflow");
      } else {
        buf[buflen] = '\0';
        handleCommand(buf);
      }
      buflen = 0;
      overflowed = false;
    } else if (c != '\r') {
      if (buflen < sizeof(buf) - 1) {
        buf[buflen++] = c;
      } else {
        overflowed = true;
      }
    }
  }
}
