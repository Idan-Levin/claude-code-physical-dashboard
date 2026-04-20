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

State parseState(const String& s) {
  if (s == "off")     return OFF;
  if (s == "idle")    return IDLE;
  if (s == "working") return WORKING;
  if (s == "waiting") return WAITING;
  if (s == "alert")   return ALERT;
  return OFF;
}

void handleCommand(String cmd) {
  cmd.trim();
  if (cmd.length() == 0) return;

  if (cmd == "ping") {
    Serial.println("pong");
    return;
  }

  if (cmd == "reset") {
    for (int i = 0; i < NUM_SLOTS; i++) slotState[i] = OFF;
    renderAll();
    Serial.println("ok reset");
    return;
  }

  if (cmd == "test") {
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

  int colon = cmd.indexOf(':');
  if (colon < 1) {
    Serial.print("err bad cmd: ");
    Serial.println(cmd);
    return;
  }

  int slot = cmd.substring(0, colon).toInt();
  String stateStr = cmd.substring(colon + 1);

  if (slot < 1 || slot > NUM_SLOTS) {
    Serial.print("err bad slot: ");
    Serial.println(slot);
    return;
  }

  slotState[slot - 1] = parseState(stateStr);
  renderSlot(slot - 1);
  Serial.print("ok ");
  Serial.println(cmd);
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

  static String buf;
  while (Serial.available() > 0) {
    char c = Serial.read();
    if (c == '\n') {
      handleCommand(buf);
      buf = "";
    } else if (c != '\r') {
      buf += c;
      if (buf.length() > 64) buf = "";
    }
  }
}
