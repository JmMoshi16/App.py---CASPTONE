// ECOReward Bin — ESP32 firmware for the kiosk protocol (README "ESP32 protocol", firmware/README.md).
//
// app.py -> ESP32:  P ping · T tare · 1/2/3 sort into Biodegradable / Recyclable / Residual
// ESP32 -> app.py:  READY · PONG · W:<g> every 200 ms · OK:<n> · DONE:<n>:<g> · JAM:<n> · FILL:<b>,<r>,<s> · ERR:<text>
// Libraries: "HX711 Arduino Library" (bogde) and "ESP32Servo". Board: ESP32 Dev Module, 115200 baud.

#include <ESP32Servo.h>
#include "HX711.h"

// ── hardware (adjust to your build) ──────────────────────────────────────────
const int   LOADCELL_DOUT_PIN  = 16;
const int   LOADCELL_SCK_PIN   = 4;
const float CALIBRATION_FACTOR = 2280.0;

const int SORTER_PIN       = 13;
const int HOME_ANGLE       = 90;
const int BIN_ANGLE[3]     = {0, 90, 180};       // bins 1 Biodegradable, 2 Recyclable, 3 Residual

const bool HAS_FILL_SENSORS = false;              // true once the 3 analog sensors are wired
const int  FILL_PIN[3]      = {34, 35, 32};       // analog 0-4095 -> 0-100 %
const int  FILL_STEP        = 3;                  // without sensors: estimated % added per drop

// ── timing ───────────────────────────────────────────────────────────────────
const unsigned long WEIGHT_EVERY_MS = 200;
const unsigned long SETTLE_MS       = 500;       // servo travel time before OK:<n>
const unsigned long JAM_TIMEOUT_MS  = 4000;      // weight must drop within 4 s of OK
const float         EMPTY_BELOW_G   = 2.0;

HX711 scale;
Servo sorter;
bool scaleOk = false;
float grams = 0;
int fill[3] = {0, 0, 0};

enum Step { IDLE, MOVING, WAITING_DROP };
Step step = IDLE;
int bin = 0;                                      // 1..3 while sorting
float gramsBefore = 0;
unsigned long stepStart = 0, lastWeight = 0;
String line;

void sendFill() {
  Serial.printf("FILL:%d,%d,%d\n", fill[0], fill[1], fill[2]);
}

void readFillSensors() {
  if (!HAS_FILL_SENSORS) return;
  for (int i = 0; i < 3; i++) fill[i] = map(analogRead(FILL_PIN[i]), 0, 4095, 0, 100);
}

void startSort(int n) {
  if (step != IDLE) {                             // one sort at a time
    Serial.println("ERR:busy");
    return;
  }
  bin = n;
  gramsBefore = grams;
  sorter.write(BIN_ANGLE[n - 1]);
  step = MOVING;
  stepStart = millis();
}

void handle(String cmd) {
  cmd.trim();
  if (cmd.length() == 0) return;
  if (cmd == "P") Serial.println("PONG");
  else if (cmd == "T") { if (scaleOk) scale.tare(5); }
  else if (cmd == "1" || cmd == "2" || cmd == "3") startSort(cmd.toInt());
  else Serial.println("ERR:unknown command");
}

void readCommands() {                             // non-blocking line reader
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      handle(line);
      line = "";
    } else if (line.length() < 32) {
      line += c;
    }
  }
}

void updateWeight() {                             // only reads when the HX711 has a new sample
  if (scaleOk && scale.is_ready()) grams = scale.get_units(1);
}

void runSort() {
  unsigned long t = millis() - stepStart;
  if (step == MOVING && t >= SETTLE_MS) {
    Serial.printf("OK:%d\n", bin);
    step = WAITING_DROP;
    stepStart = millis();
  } else if (step == WAITING_DROP) {
    if (grams < EMPTY_BELOW_G) {
      Serial.printf("DONE:%d:%.1f\n", bin, gramsBefore);
      if (HAS_FILL_SENSORS) readFillSensors();
      else fill[bin - 1] = min(100, fill[bin - 1] + FILL_STEP);
      sendFill();
      sorter.write(HOME_ANGLE);
      step = IDLE;
    } else if (t >= JAM_TIMEOUT_MS) {
      Serial.printf("JAM:%d\n", bin);
      sorter.write(HOME_ANGLE);
      step = IDLE;
    }
  }
}

void setup() {
  Serial.begin(115200);
  ESP32PWM::allocateTimer(0);
  sorter.setPeriodHertz(50);
  sorter.attach(SORTER_PIN, 500, 2400);
  sorter.write(HOME_ANGLE);

  scale.begin(LOADCELL_DOUT_PIN, LOADCELL_SCK_PIN);
  scaleOk = scale.wait_ready_timeout(1000);
  if (scaleOk) {
    scale.set_scale(CALIBRATION_FACTOR);
    scale.tare(10);                               // the tray must be empty at power-up
  }
  readFillSensors();

  Serial.println("READY");
  if (!scaleOk) Serial.println("ERR:HX711 not found");   // keep answering PONG so the kiosk can show it
  sendFill();
}

void loop() {
  readCommands();
  updateWeight();
  runSort();
  if (millis() - lastWeight >= WEIGHT_EVERY_MS) {
    lastWeight = millis();
    Serial.printf("W:%.2f\n", grams);
  }
}
