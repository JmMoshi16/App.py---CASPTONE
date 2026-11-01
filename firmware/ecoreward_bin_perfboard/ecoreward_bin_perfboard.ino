// ECOReward Bin — ESP32 firmware for app.py (kiosk protocol) + built-in setup commands
//
// app.py -> ESP32:  P ping · T tare · 1/2/3 sort into Biodegradable / Recyclable / Residual
// ESP32 -> app.py:  READY · PONG · W:<g> every 200 ms · OK:<n> · DONE:<n>:<g> · JAM:<n> · FILL:<b>,<r>,<s> · ERR:<text>
//
// SETUP (type these in the Arduino Serial Monitor, 115200, "New Line"; app.py never sends them):
//   QUIET          stop/start the W: weight lines so you can read the screen
//   INFO           show all settings
//   C90            move the chute to 90 deg (test only)
//   Y30            move the tray to 30 deg (test only)
//   SET HOME 90    chute home angle          SET BIO 0 / SET REC 90 / SET RES 180   chute angle per bin
//   SET FLAT 90    tray flat angle           SET TIP 30                              tray tip angle
//   K100           calibrate the scale with a known 100 g item on the tray (tray zeroed first with T)
//   SAVE           store everything in the ESP32's memory (kept after power off; no re-upload needed)
//   RESET          go back to the default values
// Setup replies start with "#", which app.py ignores.
//
// Libraries: "HX711 Arduino Library" (bogde) and "ESP32Servo". Board: ESP32 Dev Module, 115200 baud.
// PERFBOARD pins: J7 pin 1 -> G18 chute · pin 2 -> G19 tray · pins 3-5 -> G25/26/27 TRIG · pins 6-8 -> G32/33/34 ECHO
//                 HX711 DT G16, SCK G4, VCC 3V3

#include <ESP32Servo.h>
#include <Preferences.h>
#include "HX711.h"

// ── pins ─────────────────────────────────────────────────────────────────────
const int LOADCELL_DOUT_PIN = 16;
const int LOADCELL_SCK_PIN  = 4;
const int SORTER_PIN        = 18;   // chute servo
const int TRAY_PIN          = 19;   // tray servo

// ── fill sensors: HC-SR04 with a resistor divider on each ECHO (5 V -> 3.3 V); never wire ECHO directly ──
const bool HAS_FILL_SENSORS = true;
const int  TRIG_PIN[3]      = {25, 26, 27};
const int  ECHO_PIN[3]      = {32, 33, 34};
const float BIN_EMPTY_CM    = 30.0;
const float BIN_FULL_CM     = 5.0;

// ── default settings (changed with SET / K and kept with SAVE) ───────────────
struct Settings {
  float factor   = 2280.0;
  int   home     = 90;
  int   bin[3]   = {0, 90, 180};    // Bio, Rec, Res
  int   flat     = 90;
  int   tip      = 30;
} cfg;

// ── timing ───────────────────────────────────────────────────────────────────
const unsigned long WEIGHT_EVERY_MS = 200;
const unsigned long SETTLE_MS       = 600;    // servo travel time
const unsigned long JAM_TIMEOUT_MS  = 4000;   // item must leave the tray within 4 s
const float         EMPTY_BELOW_G   = 2.0;
const unsigned long FILL_EVERY_MS   = 30000;  // fill levels while idle, so "bin full" shows without a drop

HX711 scale;
Servo sorter, tray;
Preferences prefs;
bool scaleOk = false, quiet = false;
float grams = 0;
int fill[3] = {-1, -1, -1};   // -1 = no reading yet (the kiosk shows it as unknown, never as empty)

enum Step { IDLE, MOVING, WAITING_DROP, RETURNING };
Step step = IDLE;
int bin = 0;
float gramsBefore = 0;
unsigned long stepStart = 0, lastWeight = 0, lastFill = 0;
bool retried = false;   // one automatic re-tip before reporting JAM
String line;

// ── settings storage ─────────────────────────────────────────────────────────
void loadSettings() {
  prefs.begin("ecoreward", true);
  cfg.factor = prefs.getFloat("factor", cfg.factor);
  cfg.home   = prefs.getInt("home", cfg.home);
  cfg.bin[0] = prefs.getInt("bio", cfg.bin[0]);
  cfg.bin[1] = prefs.getInt("rec", cfg.bin[1]);
  cfg.bin[2] = prefs.getInt("res", cfg.bin[2]);
  cfg.flat   = prefs.getInt("flat", cfg.flat);
  cfg.tip    = prefs.getInt("tip", cfg.tip);
  prefs.end();
}

void saveSettings() {
  prefs.begin("ecoreward", false);
  prefs.putFloat("factor", cfg.factor);
  prefs.putInt("home", cfg.home);
  prefs.putInt("bio", cfg.bin[0]);
  prefs.putInt("rec", cfg.bin[1]);
  prefs.putInt("res", cfg.bin[2]);
  prefs.putInt("flat", cfg.flat);
  prefs.putInt("tip", cfg.tip);
  prefs.end();
}

void printInfo() {
  Serial.printf("# factor=%.1f  home=%d  bio=%d  rec=%d  res=%d  flat=%d  tip=%d  scale=%s\n",
                cfg.factor, cfg.home, cfg.bin[0], cfg.bin[1], cfg.bin[2], cfg.flat, cfg.tip,
                scaleOk ? "ok" : "NOT FOUND");
}

// ── fill sensors ─────────────────────────────────────────────────────────────
void sendFill() { Serial.printf("FILL:%d,%d,%d\n", fill[0], fill[1], fill[2]); }

float readCM(int i) {
  digitalWrite(TRIG_PIN[i], HIGH); delayMicroseconds(20); digitalWrite(TRIG_PIN[i], LOW);
  long us = pulseIn(ECHO_PIN[i], HIGH, 30000);
  return us == 0 ? -1 : us * 0.0343 / 2.0;
}

void readFillSensors() {
  if (!HAS_FILL_SENSORS) return;          // no sensors: fill stays 0 so the kiosk never pauses as "full"
  for (int i = 0; i < 3; i++) {
    float d = readCM(i);
    delay(50);
    if (d > 0) {
      d = constrain(d, BIN_FULL_CM, BIN_EMPTY_CM);
      fill[i] = (int)map(d * 10, BIN_EMPTY_CM * 10, BIN_FULL_CM * 10, 0, 100);
    }
  }
}

// ── sorting (driven by app.py) ───────────────────────────────────────────────
void startSort(int n) {
  if (step != IDLE) { Serial.println("ERR:busy"); return; }
  // Without the scale the tray can't tell that the item fell, so every sort would look done.
  if (!scaleOk) { Serial.println("ERR:HX711 not found"); return; }
  bin = n;
  retried = false;
  gramsBefore = grams;
  sorter.write(cfg.bin[n - 1]);
  step = MOVING;
  stepStart = millis();
}

void runSort() {
  unsigned long t = millis() - stepStart;
  if (step == MOVING && t >= SETTLE_MS) {
    Serial.printf("OK:%d\n", bin);
    tray.write(cfg.tip);                       // chute in place: tip the tray
    step = WAITING_DROP;
    stepStart = millis();
  } else if (step == WAITING_DROP) {
    if (grams < EMPTY_BELOW_G) {
      tray.write(cfg.flat);                    // item gone: level the tray
      step = RETURNING;
      stepStart = millis();
    } else if (t >= JAM_TIMEOUT_MS && !retried) {
      retried = true;                          // stuck: level the tray, tip it again once
      tray.write(cfg.flat);
      delay(SETTLE_MS);
      tray.write(cfg.tip);
      stepStart = millis();
    } else if (t >= JAM_TIMEOUT_MS) {
      Serial.printf("JAM:%d\n", bin);
      tray.write(cfg.flat);
      sorter.write(cfg.home);
      step = IDLE;
    }
  } else if (step == RETURNING && t >= SETTLE_MS) {
    Serial.printf("DONE:%d:%.1f\n", bin, gramsBefore);
    readFillSensors();
    sendFill();
    sorter.write(cfg.home);
    step = IDLE;
  }
}

// ── commands ─────────────────────────────────────────────────────────────────
bool setAngle(const String& name, int v) {
  v = constrain(v, 0, 180);
  if      (name == "HOME") { cfg.home = v;   sorter.write(v); }
  else if (name == "BIO")  { cfg.bin[0] = v; sorter.write(v); }
  else if (name == "REC")  { cfg.bin[1] = v; sorter.write(v); }
  else if (name == "RES")  { cfg.bin[2] = v; sorter.write(v); }
  else if (name == "FLAT") { cfg.flat = v;   tray.write(v); }
  else if (name == "TIP")  { cfg.tip = v;    tray.write(v); }
  else return false;
  Serial.printf("# %s = %d (type SAVE to keep it)\n", name.c_str(), v);
  return true;
}

void handle(String cmd) {
  cmd.trim();
  cmd.toUpperCase();
  if (cmd.length() == 0) return;

  // kiosk protocol
  if (cmd == "P") { Serial.println("PONG"); return; }
  if (cmd == "T") { if (scaleOk) scale.tare(10); return; }
  if (cmd == "1" || cmd == "2" || cmd == "3") { startSort(cmd.toInt()); return; }

  // setup commands (only accepted while no sort is running)
  if (step != IDLE) { Serial.println("# busy sorting, try again"); return; }

  if (cmd == "QUIET") { quiet = !quiet; Serial.printf("# weight lines %s\n", quiet ? "OFF" : "ON"); }
  else if (cmd == "INFO") printInfo();
  else if (cmd == "SAVE") { saveSettings(); Serial.println("# saved"); printInfo(); }
  else if (cmd == "RESET") { cfg = Settings(); saveSettings(); sorter.write(cfg.home); tray.write(cfg.flat);
                             if (scaleOk) scale.set_scale(cfg.factor); Serial.println("# defaults restored"); printInfo(); }
  else if (cmd.startsWith("C") && cmd.length() > 1 && isDigit(cmd[1])) {
    int v = constrain(cmd.substring(1).toInt(), 0, 180); sorter.write(v); Serial.printf("# chute -> %d\n", v);
  }
  else if (cmd.startsWith("Y") && cmd.length() > 1 && isDigit(cmd[1])) {
    int v = constrain(cmd.substring(1).toInt(), 0, 180); tray.write(v); Serial.printf("# tray -> %d\n", v);
  }
  else if (cmd.startsWith("K") && cmd.length() > 1) {
    float known = cmd.substring(1).toFloat();
    if (!scaleOk || known <= 0) { Serial.println("# K needs the scale and a weight, e.g. K100"); return; }
    long raw = scale.get_value(20);
    if (raw == 0) { Serial.println("# no reading change: is the item on the tray?"); return; }
    cfg.factor = raw / known;
    scale.set_scale(cfg.factor);
    Serial.printf("# factor = %.1f (type SAVE to keep it)\n", cfg.factor);
  }
  else if (cmd.startsWith("SET ")) {
    int sp = cmd.indexOf(' ', 4);
    if (sp < 0 || !setAngle(cmd.substring(4, sp), cmd.substring(sp + 1).toInt()))
      Serial.println("# use: SET HOME|BIO|REC|RES|FLAT|TIP <0-180>");
  }
  else Serial.println("ERR:unknown command");
}

void readCommands() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') { handle(line); line = ""; }
    else if (line.length() < 32) line += c;
  }
}

void updateWeight() {
  if (scaleOk && scale.is_ready()) grams = scale.get_units(1);
}

void setup() {
  Serial.begin(115200);
  loadSettings();

  ESP32PWM::allocateTimer(0);
  ESP32PWM::allocateTimer(1);
  sorter.setPeriodHertz(50);
  sorter.attach(SORTER_PIN, 500, 2400);
  sorter.write(cfg.home);
  tray.setPeriodHertz(50);
  tray.attach(TRAY_PIN, 500, 2400);
  tray.write(cfg.flat);

  if (HAS_FILL_SENSORS) {
    for (int i = 0; i < 3; i++) { pinMode(TRIG_PIN[i], OUTPUT); digitalWrite(TRIG_PIN[i], LOW); pinMode(ECHO_PIN[i], INPUT); }
  }

  scale.begin(LOADCELL_DOUT_PIN, LOADCELL_SCK_PIN);
  scaleOk = scale.wait_ready_timeout(1000);
  if (scaleOk) {
    scale.set_scale(cfg.factor);
    scale.tare(10);                          // tray must be empty at power-up
  }
  readFillSensors();

  Serial.println("READY");
  if (!scaleOk) Serial.println("ERR:HX711 not found");
  sendFill();
}

void loop() {
  readCommands();
  updateWeight();
  runSort();
  if (step == IDLE && millis() - lastFill >= FILL_EVERY_MS) {
    lastFill = millis();
    readFillSensors();
    sendFill();
  }
  if (!quiet && millis() - lastWeight >= WEIGHT_EVERY_MS) {
    lastWeight = millis();
    Serial.printf("W:%.2f\n", grams);
  }
}
