// ECOReward Bin — ESP32 firmware for the kiosk protocol (see README "ESP32 protocol").
//
// STARTING POINT, NOT TESTED ON THE REAL BIN. Every pin, angle and calibration value below is an
// assumption: check each one against your wiring (section 1 of firmware/README.md) before powering servos.
//
// Talks to app.py over USB serial, 115200 baud, one ASCII line per message:
//   app -> ESP32   "1" / "2" / "3"   sort into Biodegradable / Recyclable / Residual
//                  "T"               tare the scale          "P"  ping (answer PONG)
//   ESP32 -> app   READY, W:<grams> every 200 ms, OK:<n>, DONE:<n>:<grams>, JAM:<n>, FILL:<b>,<r>,<s>, PONG, ERR:<text>
// Staff-only (Arduino Serial Monitor, kiosk closed): "CAL <grams>" with a known weight on the tray prints the
// calibration factor; "RAW" prints the raw HX711 value.
// Test-only, while HAS_SCALE is false: "ITEM <grams>" pretends an item lies on the tray (kiosk staff key I).
//
// Libraries: ESP32Servo (Library Manager). The HX711 is read directly, no library needed.

#include <ESP32Servo.h>

// ── 0. what is connected (start with only the tray servo, add parts one by one) ──
const bool HAS_SCALE        = false;  // true once the HX711 load cell is wired; false = fake weight via "ITEM <g>"
const bool HAS_FILL_SENSORS = false;  // true once the three HC-SR04 are wired; false = FILL:0,0,0
const unsigned long FAKE_DROP_MS = 800; // without a scale: how long the tray stays tipped before "it fell"

// ── 1. pins (ASSUMED — match your wiring) ────────────────────────────────────
const int PIN_CHUTE_SERVO = 19;     // MG996R, rotates the 45° chute over the 3 bins
const int PIN_TRAY_SERVO  = 18;     // small servo that tips the tray (your microservo.ino test used 18)
const int PIN_HX711_DOUT  = 16;
const int PIN_HX711_SCK   = 4;
const int PIN_TRIG[3]     = {25, 26, 27};   // HC-SR04 trigger, bins 1 2 3
const int PIN_ECHO[3]     = {34, 35, 32};   // HC-SR04 echo through a 5 V -> 3.3 V divider, bins 1 2 3

// ── 2. angles and timing (ASSUMED — tune on the bin) ─────────────────────────
const int CHUTE_HOME      = 90;
const int CHUTE_ANGLE[3]  = {30, 90, 150};  // bin 1 Biodegradable, 2 Recyclable, 3 Residual
const int TRAY_LEVEL      = 0;              // 0° = flap closed in your servo test
const int TRAY_TIP        = 90;             // 90° = open
const unsigned long CHUTE_SETTLE_MS = 500;  // spec: move, then wait 500 ms, then OK:n
const unsigned long EMPTY_TIMEOUT_MS = 3000;// spec: weight must fall below 2 g within 3 s, else JAM:n
const unsigned long TRAY_RETURN_MS  = 400;
const float EMPTY_BELOW_G  = 2.0;

// ── 3. scale and fill sensors (CALIBRATE) ────────────────────────────────────
float calibration = 420.0;          // raw HX711 units per gram — replace with your "CAL" result
const float BIN_EMPTY_CM = 40.0;    // sensor -> bottom of an empty bin
const float BIN_FULL_CM  = 5.0;     // sensor -> top of the waste when the bin counts as full

// ─────────────────────────────────────────────────────────────────────────────
Servo chute, tray;
long tareOffset = 0;
float grams = 0, lastSample = 0;
float testItemGrams = 0;            // HAS_SCALE == false: the pretend item on the tray
int fillPct[3] = {0, 0, 0};

enum Step { IDLE, MOVING, TIPPING, RETURNING };
Step step = IDLE;
int binN = 0;                       // 1..3 while sorting
float weightBefore = 0;
unsigned long stepStart = 0, lastW = 0;
String line;

// ── HX711 (channel A, gain 128) ───────────────────────────────────────────────
bool hxReady() { return digitalRead(PIN_HX711_DOUT) == LOW; }

long hxReadRaw() {
  long v = 0;
  for (int i = 0; i < 24; i++) {
    digitalWrite(PIN_HX711_SCK, HIGH);
    delayMicroseconds(1);
    v = (v << 1) | digitalRead(PIN_HX711_DOUT);
    digitalWrite(PIN_HX711_SCK, LOW);
    delayMicroseconds(1);
  }
  digitalWrite(PIN_HX711_SCK, HIGH);   // 25th pulse: next reading channel A, gain 128
  delayMicroseconds(1);
  digitalWrite(PIN_HX711_SCK, LOW);
  if (v & 0x800000) v |= ~0xFFFFFFL;   // sign-extend 24 bits
  return v;
}

long hxAverage(int n) {
  long sum = 0;
  for (int i = 0; i < n; i++) {
    unsigned long t0 = millis();
    while (!hxReady() && millis() - t0 < 200) delay(1);
    sum += hxReadRaw();
  }
  return sum / n;
}

void updateWeight() {                  // non-blocking: only reads when the HX711 has a sample
  if (!HAS_SCALE) { grams = testItemGrams; return; }
  if (!hxReady()) return;
  float g = (hxReadRaw() - tareOffset) / calibration;
  grams = (g + lastSample) / 2.0;      // light smoothing
  lastSample = g;
}

// ── HC-SR04 ──────────────────────────────────────────────────────────────────
int measureFill(int i) {
  if (!HAS_FILL_SENSORS) return 0;
  digitalWrite(PIN_TRIG[i], LOW);
  delayMicroseconds(2);
  digitalWrite(PIN_TRIG[i], HIGH);
  delayMicroseconds(10);
  digitalWrite(PIN_TRIG[i], LOW);
  unsigned long us = pulseIn(PIN_ECHO[i], HIGH, 30000);   // ~5 m max
  if (us == 0) return fillPct[i];                          // no echo: keep the last value
  float cm = us * 0.0343 / 2.0;
  float pct = (BIN_EMPTY_CM - cm) / (BIN_EMPTY_CM - BIN_FULL_CM) * 100.0;
  return constrain((int)(pct + 0.5), 0, 100);
}

void sendFill() {
  Serial.printf("FILL:%d,%d,%d\n", fillPct[0], fillPct[1], fillPct[2]);
}

// ── commands ─────────────────────────────────────────────────────────────────
void handle(String c) {
  c.trim();
  if (c == "P") { Serial.println("PONG"); return; }
  if (c == "T") { if (HAS_SCALE) tareOffset = hxAverage(10); return; }
  if (c.startsWith("ITEM")) {            // test only: a pretend item while no load cell is wired
    if (HAS_SCALE) { Serial.println("ERR:ITEM is only for HAS_SCALE = false"); return; }
    float g = c.length() > 5 ? c.substring(5).toFloat() : 20.0;
    if (step == IDLE) testItemGrams = g > 0 ? g : 20.0;
    return;
  }
  if (c == "1" || c == "2" || c == "3") {
    if (step != IDLE) return;          // one sort at a time; the kiosk never sends two
    binN = c.toInt();
    weightBefore = grams;
    chute.write(CHUTE_ANGLE[binN - 1]);
    step = MOVING;
    stepStart = millis();
    return;
  }
  if (c == "RAW") { Serial.printf("RAW %ld (tare %ld)\n", hxAverage(10), tareOffset); return; }
  if (c.startsWith("CAL ")) {          // staff: "CAL 100" with 100 g on the tray
    float known = c.substring(4).toFloat();
    if (known > 0) {
      calibration = (hxAverage(20) - tareOffset) / known;
      Serial.printf("CAL factor=%.2f  (put this in 'calibration' and upload again)\n", calibration);
    }
    return;
  }
  if (c.length()) Serial.printf("ERR:unknown command %s\n", c.c_str());
}

void runSort() {
  unsigned long t = millis() - stepStart;
  switch (step) {
    case MOVING:
      if (t >= CHUTE_SETTLE_MS) {
        Serial.printf("OK:%d\n", binN);
        tray.write(TRAY_TIP);
        step = TIPPING;
        stepStart = millis();
      }
      break;
    case TIPPING:
      if (!HAS_SCALE && t >= FAKE_DROP_MS) testItemGrams = 0;   // the pretend item slid off
      if (grams < EMPTY_BELOW_G && t >= 300) {
        tray.write(TRAY_LEVEL);
        step = RETURNING;
        stepStart = millis();
      } else if (t >= EMPTY_TIMEOUT_MS) {
        Serial.printf("JAM:%d\n", binN);
        tray.write(TRAY_LEVEL);        // level the tray so staff can reach the item
        chute.write(CHUTE_HOME);
        step = IDLE;
      }
      break;
    case RETURNING:
      if (t >= TRAY_RETURN_MS) {
        Serial.printf("DONE:%d:%.1f\n", binN, weightBefore);
        fillPct[binN - 1] = measureFill(binN - 1);
        sendFill();
        chute.write(CHUTE_HOME);
        step = IDLE;
      }
      break;
    default:
      break;
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(PIN_HX711_DOUT, INPUT);
  pinMode(PIN_HX711_SCK, OUTPUT);
  digitalWrite(PIN_HX711_SCK, LOW);
  for (int i = 0; i < 3; i++) {
    pinMode(PIN_TRIG[i], OUTPUT);
    pinMode(PIN_ECHO[i], INPUT);
  }
  ESP32PWM::allocateTimer(0);
  ESP32PWM::allocateTimer(1);
  chute.setPeriodHertz(50);
  tray.setPeriodHertz(50);
  chute.attach(PIN_CHUTE_SERVO, 500, 2400);
  tray.attach(PIN_TRAY_SERVO, 500, 2400);
  chute.write(CHUTE_HOME);
  tray.write(TRAY_LEVEL);
  delay(800);
  if (HAS_SCALE) tareOffset = hxAverage(10);   // the tray must be empty at power-up
  for (int i = 0; i < 3; i++) fillPct[i] = measureFill(i);
  Serial.println("READY");
  sendFill();
}

void loop() {
  while (Serial.available()) {
    char ch = Serial.read();
    if (ch == '\n' || ch == '\r') {
      if (line.length()) handle(line);
      line = "";
    } else if (line.length() < 40) {
      line += ch;
    }
  }
  updateWeight();
  runSort();
  if (millis() - lastW >= 200) {       // stream the weight, also while sorting
    lastW = millis();
    Serial.printf("W:%.1f\n", grams);
  }
}
