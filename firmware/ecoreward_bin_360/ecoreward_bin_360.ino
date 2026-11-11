// ECOReward Bin — ESP32 firmware for app.py (hold mode)
// CHUTE: 360° continuous servo + 3 end switches (one per bin)
// NO TRAY (HAS_TRAY = false): the customer holds the item in the bin opening under the camera, lets go when the
// kiosk says so, and the item falls onto a small DROP PLATE on the load cell, then slides into the chute.
// A weight bump on the plate is the proof that the item really went in.
//
// HOW A DEPOSIT WORKS
//   1. app.py sends 1, 2 or 3. The chute ALWAYS spins FORWARD until that bin's switch clicks (other switches it
//      passes are ignored). No click within 12 s -> JAM:<n>.
//   2. Chute stopped -> OK:<n>. The plate is zeroed (tare), then we wait up to 15 s for a bump of 2 g or more.
//      Bump -> DONE:<n>:<peak grams>.   No bump -> NODROP:<n>.
//   3. If the item stays on the plate more than 3 s after landing -> JAM:<n>. When the plate is empty again,
//      small drift is zeroed and FILL is sent.
//
// CHUTE SAFETY (protects the end switches and the parts inside)
//   - There is NO code path that spins backward: the only spin function is chuteForward(), which always sends a
//     pulse on the forward side of the stop pulse. SPIN- is refused.
//   - The chosen bin is reached by turning forward, even if it is "behind": the chute goes all the way round and
//     ignores every other switch it passes. It stops only on the chosen bin's switch (10 ms debounce).
//   - Already at the chosen bin -> no movement at all.
//   - SPEED can't go below 60 µs from STOP, so a slightly wrong STOP can never turn "forward" into "backward".
//   - After stopping, the servo is detached: no creeping either way. Nothing spins at power-up.
//   - Every spin (deposit, GO1-3, SPIN+) stops by itself after 12 s if the switch never clicks.
//   - FLIP only changes which way counts as forward (use it once, if the chute turns the wrong way on SPIN+).
//
// app.py -> ESP32:  P ping · T tare · 1/2/3 go to the Biodegradable / Recyclable / Residual bin
// ESP32 -> app.py:  READY · PONG · W:<g> every 200 ms · OK:<n> · DONE:<n>:<g> · NODROP:<n> · JAM:<n> ·
//                   FILL:<b>,<r>,<s> · ERR:<text>
//
// SETUP COMMANDS (Arduino Serial Monitor, 115200, "New Line"; replies start with "#", app.py ignores them)
//   QUIET           hide/show the W: weight lines (easier to read the # progress lines)
//   LOG             turn the # progress lines off/on
//   INFO            show all settings          SW     show the 3 end switches
//   STATUS          one line: chute, switches, weight
//   US              read the 3 ultrasonic fill sensors now (distance in cm and fill %)
//   TEST1 TEST2 TEST3  full deposit test without app.py: chute -> OK -> drop something on the plate -> DONE
//   SPIN+           spin the chute forward (test, stops by itself after 12 s)   SPIN0  stop it   (SPIN- is refused)
//   GO1 GO2 GO3     move the chute to a bin only (no drop check)
//   FLIP            reverse the chute's "forward" direction
//   SET STOP 1500   chute stop pulse (µs) – raise/lower until the chute stands still at SPIN0
//   SET SPEED 350   chute speed (µs away from STOP; smaller = slower)
//   SET CREEP 0     ms to keep turning after the switch clicks (centres the opening)
//   K100            calibrate the scale with a known 100 g item on the plate (zero first with T)
//   SAVE / RESET    keep settings in the ESP32 / back to defaults
//
// WIRING
//   Chute 360° servo signal -> P18 (J2)          (power from the perfboard; no tray servo)
//   End switches: black C -> G, white NO -> S pin of  Bio = P13 · Rec = P17 (TX2) · Res = P21   (NC unused)
//   HX711 (under the drop plate): VCC 3V3, GND G, DT P16, SCK P4
//   HC-SR04 fill sensors (one above each bin, looking down): TRIG P25 / P26 / P27, ECHO P32 / P33 / P34
//                         ECHO is 5 V: through the perfboard dividers (1 kΩ / 2 kΩ) to 3.3 V. VCC 5 V, GND G.
//                         No echo -> -1 for that bin (the kiosk shows "no reading", never "empty").
// Libraries: "ESP32Servo", "HX711 Arduino Library" (bogde). Board: ESP32 Dev Module.

#include <stdarg.h>
#include <ESP32Servo.h>
#include <Preferences.h>
#include "HX711.h"

// ── hardware present ─────────────────────────────────────────────────────────
const bool HAS_TRAY = false;                  // the 180° tray servo is gone: hold mode
const bool HAS_FILL_SENSORS = true;           // 3 x HC-SR04 above the bins

// ── pins ─────────────────────────────────────────────────────────────────────
const int CHUTE_PIN = 18;
const int DT_PIN = 16, SCK_PIN = 4;
const int SW_PIN[3] = {13, 17, 21};          // Bio, Rec, Res
const int  TRIG_PIN[3] = {25, 26, 27};
const int  ECHO_PIN[3] = {32, 33, 34};
const float BIN_EMPTY_CM = 30.0, BIN_FULL_CM = 5.0;

// ── settings (changed with SET / K, kept with SAVE) ──────────────────────────
struct Settings {
  float factor  = 2280.0;
  int   stopUs  = 1500;    // 360° servo stop pulse
  int   speedUs = 350;     // how far from stop = speed. Overshoots? SET SPEED 250
  int   dir     = 1;       // 1 or -1 (FLIP)
  int   creepMs = 0;
} cfg;

// ── timing and thresholds ────────────────────────────────────────────────────
const unsigned long WEIGHT_EVERY_MS  = 200;
const unsigned long FILL_EVERY_MS    = 10000;  // send FILL every 10 s while idle (and after every deposit)
const int           MIN_SPEED_US     = 60;     // forward pulse is always at least this far from STOP
const unsigned long CHUTE_TIMEOUT_MS = 12000;  // target switch must click within 12 s (forward only)
const unsigned long DROP_WAIT_MS     = 15000;  // after OK: the customer has 15 s to let go
const unsigned long PEAK_MS          = 150;    // after the bump: keep the highest reading this long
const unsigned long STUCK_MS         = 3000;   // item still on the plate this long after landing -> JAM
const unsigned long EMPTY_STEADY_MS  = 300;    // plate must read empty this long
const float         DROP_MIN_G       = 2.0;    // a bump this big = the item landed

HX711 scale;
Servo chute;
Preferences prefs;
bool scaleOk = false, quiet = false, logOn = true;
float grams = 0;
int fill[3] = {0, 0, 0};
const char* BIN_NAME[3] = {"Bio", "Rec", "Res"};
bool lastSw[3] = {false, false, false};

int current = -1;          // bin the chute is at (0..2), -1 = unknown

// progress lines for the Serial Monitor; app.py ignores lines that start with "#"
void note(const char* fmt, ...) {
  if (!logOn) return;
  char buf[160];
  va_list ap; va_start(ap, fmt); vsnprintf(buf, sizeof(buf), fmt, ap); va_end(ap);
  Serial.print("# "); Serial.println(buf);
}

enum Step { IDLE, CHUTE_MOVING, CHUTE_CREEP, WAIT_DROP, PEAK, ON_PLATE, SPIN_TEST };
Step step = IDLE;
int target = 0;            // 0..2
bool sorting = false;
float peak = 0;
unsigned long stepStart = 0, landedAt = 0, emptySince = 0, seenSince = 0, lastWeight = 0, lastFill = 0;
String line;

void chuteArrived();

// ── settings storage ─────────────────────────────────────────────────────────
void loadSettings() {
  prefs.begin("eco360", true);
  cfg.factor  = prefs.getFloat("factor", cfg.factor);
  cfg.stopUs  = prefs.getInt("stop", cfg.stopUs);
  cfg.speedUs = constrain(prefs.getInt("speed", cfg.speedUs), MIN_SPEED_US, 500);
  cfg.dir     = prefs.getInt("dir", cfg.dir) < 0 ? -1 : 1;
  cfg.creepMs = prefs.getInt("creep", cfg.creepMs);
  prefs.end();
}
void saveSettings() {
  prefs.begin("eco360", false);
  prefs.putFloat("factor", cfg.factor);
  prefs.putInt("stop", cfg.stopUs);   prefs.putInt("speed", cfg.speedUs);
  prefs.putInt("dir", cfg.dir);       prefs.putInt("creep", cfg.creepMs);
  prefs.end();
}

bool pressed(int i) { return digitalRead(SW_PIN[i]) == LOW; }
int  pressedBin()   { for (int i = 0; i < 3; i++) if (pressed(i)) return i; return -1; }

void printInfo() {
  Serial.printf("# factor=%.1f stop=%d speed=%d dir=%d creep=%d chute_at=%d scale=%s tray=%s\n",
                cfg.factor, cfg.stopUs, cfg.speedUs, cfg.dir, cfg.creepMs, current + 1,
                scaleOk ? "ok" : "NOT FOUND", HAS_TRAY ? "yes" : "none (hold mode)");
}

// ── chute (360° servo) ───────────────────────────────────────────────────────
int forwardUs() {                             // the ONLY spin pulse there is: always on the forward side of STOP
  return cfg.stopUs + cfg.dir * max(cfg.speedUs, MIN_SPEED_US);
}
void chuteForward() {
  if (!chute.attached()) { chute.setPeriodHertz(50); chute.attach(CHUTE_PIN, 500, 2500); }
  chute.writeMicroseconds(forwardUs());
}
void chuteStop() {
  if (chute.attached()) { chute.writeMicroseconds(cfg.stopUs); delay(80); chute.detach(); }   // detach = no creeping
}

void startChute(int bin, bool isSort) {
  target = bin; sorting = isSort;
  if (isSort) note("DEPOSIT -> %s (bin %d)", BIN_NAME[bin], bin + 1);
  if (pressed(bin)) { note("chute already at %s", BIN_NAME[bin]); current = bin; stepStart = millis(); chuteArrived(); return; }
  for (int i = 0; i < 3; i++) lastSw[i] = pressed(i);
  chuteForward();                             // always forward, even if the bin is "behind": never backward
  note("chute spinning forward toward %s (signal %d us); other switches are ignored",
       BIN_NAME[bin], forwardUs());
  step = CHUTE_MOVING; stepStart = millis(); seenSince = 0;
}

void chuteArrived() {
  chuteStop();
  current = target;
  note("chute STOPPED at %s switch (P%d) after %lu ms", BIN_NAME[target], SW_PIN[target], millis() - stepStart);
  if (!sorting) { step = IDLE; return; }
  Serial.printf("OK:%d\n", target + 1);
  if (scaleOk) scale.tare(5);                 // the plate is empty: the item is still in the customer's hand
  grams = 0; peak = 0;
  note("waiting up to %lu s for the item to land on the plate (bump of %.0f g)", DROP_WAIT_MS / 1000, DROP_MIN_G);
  step = WAIT_DROP; stepStart = millis();
}

void checkChutePosition() {                   // at power-up: no spinning, just read where it is
  current = pressedBin();                     // -1 if it is between bins (the first deposit turns forward)
}

// ── fill sensors ─────────────────────────────────────────────────────────────
float distCm[3] = {-1, -1, -1};

void sendFill() { Serial.printf("FILL:%d,%d,%d\n", fill[0], fill[1], fill[2]); lastFill = millis(); }

float pingCm(int i) {                         // one ping; -1 = no echo (unwired, nothing in range)
  digitalWrite(TRIG_PIN[i], LOW); delayMicroseconds(4);
  digitalWrite(TRIG_PIN[i], HIGH); delayMicroseconds(10); digitalWrite(TRIG_PIN[i], LOW);
  long us = pulseIn(ECHO_PIN[i], HIGH, 30000); // 30 ms = about 5 m
  return us > 0 ? us * 0.0343 / 2.0 : -1;
}

void readFillSensors() {                      // median of 3 pings per bin; one sensor at a time (no crosstalk)
  if (!HAS_FILL_SENSORS) return;
  for (int i = 0; i < 3; i++) {
    float a[3];
    for (int k = 0; k < 3; k++) { a[k] = pingCm(i); delay(30); }
    if (a[0] > a[1]) { float t = a[0]; a[0] = a[1]; a[1] = t; }
    if (a[1] > a[2]) { float t = a[1]; a[1] = a[2]; a[2] = t; }
    if (a[0] > a[1]) { float t = a[0]; a[0] = a[1]; a[1] = t; }
    distCm[i] = a[1];
    if (distCm[i] < 0) { fill[i] = -1; continue; }
    float d = constrain(distCm[i], BIN_FULL_CM, BIN_EMPTY_CM);
    fill[i] = (int)map((long)(d * 10), (long)(BIN_EMPTY_CM * 10), (long)(BIN_FULL_CM * 10), 0, 100);
  }
}

void plateCleared() {
  if (scaleOk && fabs(grams) < 10) scale.tare(5);   // zero small drift
  readFillSensors();
  sendFill();
  step = IDLE;
}

// ── deposit state machine ────────────────────────────────────────────────────
void runSteps() {
  unsigned long t = millis() - stepStart;
  switch (step) {
    case CHUTE_MOVING:
      for (int i = 0; i < 3; i++) {                     // show the switches it passes
        bool p = pressed(i);
        if (p && !lastSw[i] && i != target) note("passed %s switch (ignored, going to %s)", BIN_NAME[i], BIN_NAME[target]);
        lastSw[i] = p;
      }
      if (pressed(target)) {
        if (!seenSince) seenSince = millis();
        else if (millis() - seenSince >= 10) {          // debounce
          if (cfg.creepMs > 0) { step = CHUTE_CREEP; stepStart = millis(); }
          else chuteArrived();
        }
      } else seenSince = 0;
      if (step == CHUTE_MOVING && t >= CHUTE_TIMEOUT_MS) {
        chuteStop(); current = -1; step = IDLE;
        note("TIMEOUT: %s switch (P%d) never clicked in %lu s. Check its wire, or try FLIP",
             BIN_NAME[target], SW_PIN[target], CHUTE_TIMEOUT_MS / 1000);
        if (sorting) Serial.printf("JAM:%d\n", target + 1);
      }
      break;
    case CHUTE_CREEP:
      if (t >= (unsigned long)cfg.creepMs) chuteArrived();
      break;
    case SPIN_TEST:                                     // SPIN+ never runs forever
      if (t >= CHUTE_TIMEOUT_MS) { chuteStop(); step = IDLE; note("SPIN+ stopped after %lu s", CHUTE_TIMEOUT_MS / 1000); }
      break;
    case WAIT_DROP:                                     // let go: wait for the bump
      if (grams >= DROP_MIN_G) {
        peak = grams; landedAt = millis(); step = PEAK; stepStart = millis();
        note("bump: the item landed (%.1f g)", grams);
      } else if (t >= DROP_WAIT_MS) {
        note("no bump within %lu s: nothing landed on the plate", DROP_WAIT_MS / 1000);
        Serial.printf("NODROP:%d\n", target + 1);
        step = IDLE;
      }
      break;
    case PEAK:                                          // keep the highest reading for a moment
      if (grams > peak) peak = grams;
      if (t >= PEAK_MS) {
        Serial.printf("DONE:%d:%.1f\n", target + 1, peak);
        note("DONE: %s, peak %.1f g", BIN_NAME[target], peak);
        step = ON_PLATE; emptySince = 0;
      }
      break;
    case ON_PLATE:                                      // the item should slide off into the chute
      if (grams < DROP_MIN_G) {
        if (!emptySince) emptySince = millis();
        else if (millis() - emptySince >= EMPTY_STEADY_MS) { note("plate empty again"); plateCleared(); }
      } else emptySince = 0;
      if (step == ON_PLATE && millis() - landedAt >= STUCK_MS) {
        note("item STUCK on the plate (%.1f g) for %lu s", grams, STUCK_MS / 1000);
        Serial.printf("JAM:%d\n", target + 1);
        step = IDLE;
      }
      break;
    default: break;
  }
}

// ── commands ─────────────────────────────────────────────────────────────────
void handle(String cmd) {
  cmd.trim(); cmd.toUpperCase();
  if (cmd.length() == 0) return;

  if (cmd == "P") { Serial.println("PONG"); return; }
  if (cmd == "T") { if (scaleOk) scale.tare(10); return; }
  if (cmd == "1" || cmd == "2" || cmd == "3") {
    if (step != IDLE) { Serial.println("ERR:busy"); return; }
    startChute(cmd.toInt() - 1, true);
    return;
  }

  if (cmd == "SPIN0") { chuteStop(); if (step == SPIN_TEST) step = IDLE; Serial.println("# stopped"); return; }
  if (step != IDLE) { Serial.println("# busy, try again"); return; }

  if (cmd == "QUIET") { quiet = !quiet; Serial.printf("# weight lines %s\n", quiet ? "OFF" : "ON"); }
  else if (cmd == "LOG") { logOn = !logOn; Serial.printf("# progress lines %s\n", logOn ? "ON" : "OFF"); }
  else if (cmd == "INFO") printInfo();
  else if (cmd == "STATUS") Serial.printf("# chute at %s | switches Bio=%s Rec=%s Res=%s | plate %.1f g | scale %s\n",
      current >= 0 ? BIN_NAME[current] : "unknown", pressed(0) ? "PRESSED" : "-", pressed(1) ? "PRESSED" : "-",
      pressed(2) ? "PRESSED" : "-", grams, scaleOk ? "ok" : "NOT FOUND");
  else if (cmd.startsWith("TEST") && cmd.length() == 5 && cmd[4] >= '1' && cmd[4] <= '3') {
    note("TEST deposit (same as app.py sending %c): wait for OK, then drop something on the plate", cmd[4]);
    startChute(cmd[4] - '1', true);
  }
  else if (cmd == "SW") Serial.printf("# Bio=%s Rec=%s Res=%s\n", pressed(0) ? "PRESSED" : "-",
                                      pressed(1) ? "PRESSED" : "-", pressed(2) ? "PRESSED" : "-");
  else if (cmd == "SAVE") { saveSettings(); Serial.println("# saved"); printInfo(); }
  else if (cmd == "RESET") { cfg = Settings(); saveSettings();
                             if (scaleOk) scale.set_scale(cfg.factor); Serial.println("# defaults"); printInfo(); }
  else if (cmd == "FLIP") { cfg.dir = -cfg.dir;
                            Serial.printf("# forward is now the other way (dir=%d); nothing moved. SAVE to keep\n", cfg.dir); }
  else if (cmd == "US") {
    readFillSensors();
    for (int i = 0; i < 3; i++)
      Serial.printf("# %s: %s%.1f cm -> %d %%\n", BIN_NAME[i], distCm[i] < 0 ? "no echo " : "", distCm[i], fill[i]);
    sendFill();
  }
  else if (cmd == "SPIN+") { chuteForward(); step = SPIN_TEST; stepStart = millis();
                             Serial.println("# spinning forward (SPIN0 to stop, stops by itself after 12 s)"); }
  else if (cmd == "SPIN-") { Serial.println("# backward spinning is disabled (protects the end switches)"); }
  else if (cmd.startsWith("GO") && cmd.length() == 3 && cmd[2] >= '1' && cmd[2] <= '3') startChute(cmd[2] - '1', false);
  else if (cmd.startsWith("K") && cmd.length() > 1) {
    float known = cmd.substring(1).toFloat();
    if (!scaleOk || known <= 0) { Serial.println("# use K100 with a known item on the plate"); return; }
    long raw = scale.get_value(20);
    if (raw == 0) { Serial.println("# no change: is the item on the plate?"); return; }
    cfg.factor = raw / known; scale.set_scale(cfg.factor);
    Serial.printf("# factor = %.1f (SAVE to keep)\n", cfg.factor);
  }
  else if (cmd.startsWith("SET ")) {
    int sp = cmd.indexOf(' ', 4);
    String name = sp > 0 ? cmd.substring(4, sp) : "";
    int v = sp > 0 ? cmd.substring(sp + 1).toInt() : 0;
    if      (name == "STOP")  { cfg.stopUs = constrain(v, 1300, 1700); }
    else if (name == "SPEED") { cfg.speedUs = constrain(v, MIN_SPEED_US, 500); v = cfg.speedUs; }
    else if (name == "CREEP") { cfg.creepMs = constrain(v, 0, 1000); }
    else { Serial.println("# use: SET STOP|SPEED <us>, SET CREEP <ms>"); return; }
    Serial.printf("# %s = %d (SAVE to keep)\n", name.c_str(), v);
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

void setup() {
  Serial.begin(115200);
  loadSettings();
  for (int i = 0; i < 3; i++) pinMode(SW_PIN[i], INPUT_PULLUP);
  ESP32PWM::allocateTimer(0);                 // the chute servo; HAS_TRAY = false: no tray servo is attached

  if (HAS_FILL_SENSORS)
    for (int i = 0; i < 3; i++) { pinMode(TRIG_PIN[i], OUTPUT); digitalWrite(TRIG_PIN[i], LOW); pinMode(ECHO_PIN[i], INPUT); }

  scale.begin(DT_PIN, SCK_PIN);
  scaleOk = scale.wait_ready_timeout(1000);
  if (scaleOk) { scale.set_scale(cfg.factor); scale.tare(10); }   // the drop plate must be empty at power-up

  checkChutePosition();
  readFillSensors();

  Serial.println("READY");
  if (!scaleOk) Serial.println("ERR:HX711 not found");
  sendFill();

  note("==== ECOReward bin ready (hold mode, no tray) ====");
  note("chute P%d (360, stop %d us, speed %d)", CHUTE_PIN, cfg.stopUs, cfg.speedUs);
  note("end switches: Bio P13=%s  Rec P17=%s  Res P21=%s", pressed(0) ? "PRESSED" : "-",
       pressed(1) ? "PRESSED" : "-", pressed(2) ? "PRESSED" : "-");
  note("chute is at: %s", current >= 0 ? BIN_NAME[current] : "between bins (first deposit turns forward)");
  note("drop plate scale: %s", scaleOk ? "ok, zeroed" : "NOT FOUND - check DT P16, SCK P4, 3V3, G");
  note("fill: Bio %d %%  Rec %d %%  Res %d %%  (-1 = no echo; type US to see the distances)", fill[0], fill[1], fill[2]);
  note("test without app.py: TEST1 / TEST2 / TEST3, then drop something on the plate.  QUIET hides the W: lines");
}

void loop() {
  readCommands();
  if (scaleOk && scale.is_ready()) grams = scale.get_units(1);
  runSteps();
  if (step == IDLE && HAS_FILL_SENSORS && millis() - lastFill >= FILL_EVERY_MS) { readFillSensors(); sendFill(); }
  if (!quiet && millis() - lastWeight >= WEIGHT_EVERY_MS) {
    lastWeight = millis();
    Serial.printf("W:%.2f\n", grams);
  }
}
