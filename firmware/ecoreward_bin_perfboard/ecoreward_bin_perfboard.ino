// ECOReward Bin — ESP32 firmware for app.py
// CHUTE: 360° continuous servo + 3 limit switches (one per bin)
// TRAY : 180° servo that swings the board away so the wall drops the item
//
// HOW THE CHUTE MOVES
//   The chute stays still until app.py sends 1, 2 or 3 (after the scan).
//   It then spins toward that bin and stops ONLY on that bin's switch.
//   Other switches it passes on the way are ignored.
//   No spinning at power-up: the first sort finds its way by turning forward.
//
// app.py -> ESP32:  P ping · T tare · 1/2/3 sort into Biodegradable / Recyclable / Residual
// ESP32 -> app.py:  READY · PONG · W:<g> every 200 ms · OK:<n> · DONE:<n>:<g> · JAM:<n> · FILL:<b>,<r>,<s> · ERR:<text>
//
// SETUP COMMANDS (Arduino Serial Monitor, 115200, "New Line"; replies start with "#", app.py ignores them)
//   QUIET           hide/show the W: weight lines (easier to read the # progress lines)
//   LOG             turn the # progress lines off/on
//   INFO            show all settings          SW     show the 3 limit switches
//   STATUS          one line: chute, tray, switches, weight
//   TEST1 TEST2 TEST3  full sort test without app.py (chute -> tray drops -> back)
//   SPIN+  SPIN-    spin the chute (test)      SPIN0  stop it
//   GO1 GO2 GO3     move the chute to a bin only (no tray)
//   FLIP            reverse the chute's "forward" direction
//   SET STOP 1500   chute stop pulse (µs) – raise/lower until the chute stands still at SPIN0
//   SET SPEED 120   chute speed (µs away from STOP; smaller = slower)
//   SET CREEP 0     ms to keep turning after the switch clicks (centres the opening)
//   Y30             move the tray to 30° (test)
//   SET FLAT 90     tray closed angle          SET OPEN 30   tray swung-away angle
//   K100            calibrate the scale with a known 100 g item (zero first with T)
//   SAVE / RESET    keep settings in the ESP32 / back to defaults
//
// WIRING
//   Chute 360° servo signal -> G18 (J2)      Tray servo signal -> G19 (J3)      (power from the perfboard)
//   Limit switches (end switches): black C -> G, white NO -> S pin of  Bio = P13 · Rec = P17 (TX2) · Res = P21   (NC unused)
//   HX711: VCC 3V3, GND G, DT P16, SCK P4
//   HC-SR04 fill sensors: NOT CONNECTED for now (HAS_FILL_SENSORS = false, the kiosk gets FILL:0,0,0)
//                         later: TRIG P25/P26/P27, ECHO P32/P33/P34 through the perfboard dividers
// Libraries: "ESP32Servo", "HX711 Arduino Library" (bogde). Board: ESP32 Dev Module.

#include <stdarg.h>
#include <ESP32Servo.h>
#include <Preferences.h>
#include "HX711.h"

// ── pins ─────────────────────────────────────────────────────────────────────
const int CHUTE_PIN = 18, TRAY_PIN = 19;
const int DT_PIN = 16, SCK_PIN = 4;
const int SW_PIN[3] = {13, 17, 21};          // Bio, Rec, Res

// ── fill sensors: OFF until HC-SR04P / RCWL-1601 are installed ──────────────
const bool HAS_FILL_SENSORS = false;
const int  TRIG_PIN[3] = {25, 26, 27};
const int  ECHO_PIN[3] = {32, 33, 34};
const float BIN_EMPTY_CM = 30.0, BIN_FULL_CM = 5.0;

// ── settings (changed with SET / K, kept with SAVE) ──────────────────────────
struct Settings {
  float factor  = 2280.0;
  int   stopUs  = 1500;    // 360° servo stop pulse
  int   speedUs = 350;     // how far from stop = speed (1850 µs = fast, as in the switch test). Overshoots? SET SPEED 250
  int   dir     = 1;       // 1 or -1 (FLIP)
  int   creepMs = 0;
  int   flat    = 90;      // tray closed
  int   open    = 30;      // tray swung away
} cfg;

// ── timing ───────────────────────────────────────────────────────────────────
const unsigned long WEIGHT_EVERY_MS   = 200;
const unsigned long CHUTE_TIMEOUT_MS  = 8000;  // target switch must click within 8 s (a full turn at worst)
const unsigned long TRAY_MOVE_MS      = 600;   // tray travel time
const unsigned long IGNORE_AFTER_OPEN = 400;   // scale is shaky right after the swing
const unsigned long EMPTY_STEADY_MS   = 300;   // must read empty this long
const unsigned long DROP_TIMEOUT_MS   = 4000;  // per try (one retry before JAM)
const float         EMPTY_BELOW_G     = 2.0;

HX711 scale;
Servo chute, tray;
Preferences prefs;
bool scaleOk = false, quiet = false, logOn = true;
float grams = 0;
int fill[3] = {0, 0, 0};
const char* BIN[3] = {"Bio", "Rec", "Res"};
bool lastSw[3] = {false, false, false};

int current = -1;          // bin the chute is at (0..2), -1 = unknown

// progress lines for the Serial Monitor; app.py ignores lines that start with "#"
void note(const char* fmt, ...) {
  if (!logOn) return;
  char buf[160];
  va_list ap; va_start(ap, fmt); vsnprintf(buf, sizeof(buf), fmt, ap); va_end(ap);
  Serial.print("# "); Serial.println(buf);
}

enum Step { IDLE, CHUTE_MOVING, CHUTE_CREEP, OPENING, WAIT_DROP, RETRY_CLOSE, CLOSING };
Step step = IDLE;
int target = 0;            // 0..2
bool sorting = false, retried = false;
float gramsBefore = 0;
unsigned long stepStart = 0, emptySince = 0, seenSince = 0, lastWeight = 0;
String line;

void chuteArrived();

// ── settings storage ─────────────────────────────────────────────────────────
void loadSettings() {
  prefs.begin("eco360", true);
  cfg.factor  = prefs.getFloat("factor", cfg.factor);
  cfg.stopUs  = prefs.getInt("stop", cfg.stopUs);
  cfg.speedUs = prefs.getInt("speed", cfg.speedUs);
  cfg.dir     = prefs.getInt("dir", cfg.dir);
  cfg.creepMs = prefs.getInt("creep", cfg.creepMs);
  cfg.flat    = prefs.getInt("flat", cfg.flat);
  cfg.open    = prefs.getInt("open", cfg.open);
  prefs.end();
}
void saveSettings() {
  prefs.begin("eco360", false);
  prefs.putFloat("factor", cfg.factor);
  prefs.putInt("stop", cfg.stopUs);   prefs.putInt("speed", cfg.speedUs);
  prefs.putInt("dir", cfg.dir);       prefs.putInt("creep", cfg.creepMs);
  prefs.putInt("flat", cfg.flat);     prefs.putInt("open", cfg.open);
  prefs.end();
}

bool pressed(int i) { return digitalRead(SW_PIN[i]) == LOW; }
int  pressedBin()   { for (int i = 0; i < 3; i++) if (pressed(i)) return i; return -1; }

void printInfo() {
  Serial.printf("# factor=%.1f stop=%d speed=%d dir=%d creep=%d flat=%d open=%d chute_at=%d scale=%s\n",
                cfg.factor, cfg.stopUs, cfg.speedUs, cfg.dir, cfg.creepMs, cfg.flat, cfg.open,
                current + 1, scaleOk ? "ok" : "NOT FOUND");
}

// ── chute (360° servo) ───────────────────────────────────────────────────────
void chuteSpin(int d) {                       // d = +1 forward, -1 backward
  if (!chute.attached()) { chute.setPeriodHertz(50); chute.attach(CHUTE_PIN, 500, 2500); }
  chute.writeMicroseconds(cfg.stopUs + d * cfg.dir * cfg.speedUs);
}
void chuteStop() {
  if (chute.attached()) { chute.writeMicroseconds(cfg.stopUs); delay(80); chute.detach(); }   // detach = no creeping
}

int directionTo(int from, int to) {           // order forward: Bio -> Rec -> Res -> Bio
  if (from < 0) return 1;
  return ((to - from + 3) % 3 == 1) ? 1 : -1; // next bin forward, previous bin backward
}

void startChute(int bin, bool isSort) {
  target = bin; sorting = isSort; retried = false;
  if (isSort) note("SORT -> %s (bin %d), item %.1f g on the tray", BIN[bin], bin + 1, gramsBefore);
  if (pressed(bin)) { note("chute already at %s", BIN[bin]); current = bin; chuteArrived(); return; }
  int d = directionTo(current, bin);
  for (int i = 0; i < 3; i++) lastSw[i] = pressed(i);
  chuteSpin(d);
  note("chute spinning %s toward %s (signal %d us); other switches are ignored",
       d > 0 ? "forward" : "backward", BIN[bin], cfg.stopUs + d * cfg.dir * cfg.speedUs);
  step = CHUTE_MOVING; stepStart = millis(); seenSince = 0;
}

void chuteArrived() {
  chuteStop();
  current = target;
  note("chute STOPPED at %s switch (P%d) after %lu ms", BIN[target], SW_PIN[target], millis() - stepStart);
  if (!sorting) { step = IDLE; return; }
  Serial.printf("OK:%d\n", target + 1);
  tray.write(cfg.open);                        // swing the board away: the wall drops the item
  note("tray OPEN (%d deg): dropping the item", cfg.open);
  step = OPENING; stepStart = millis();
}

void checkChutePosition() {                   // at power-up: no spinning, just read where it is
  current = pressedBin();                     // -1 if it is between bins (the first sort turns forward)
}

// ── fill sensors ─────────────────────────────────────────────────────────────
void sendFill() { Serial.printf("FILL:%d,%d,%d\n", fill[0], fill[1], fill[2]); }
void readFillSensors() {
  if (!HAS_FILL_SENSORS) return;
  for (int i = 0; i < 3; i++) {
    digitalWrite(TRIG_PIN[i], HIGH); delayMicroseconds(20); digitalWrite(TRIG_PIN[i], LOW);
    long us = pulseIn(ECHO_PIN[i], HIGH, 30000);
    delay(50);
    if (us > 0) {
      float d = constrain(us * 0.0343 / 2.0, BIN_FULL_CM, BIN_EMPTY_CM);
      fill[i] = (int)map(d * 10, BIN_EMPTY_CM * 10, BIN_FULL_CM * 10, 0, 100);
    }
  }
}

// ── sort state machine ───────────────────────────────────────────────────────
void runSteps() {
  unsigned long t = millis() - stepStart;
  switch (step) {
    case CHUTE_MOVING:
      for (int i = 0; i < 3; i++) {                     // show the switches it passes
        bool p = pressed(i);
        if (p && !lastSw[i] && i != target) note("passed %s switch (ignored, going to %s)", BIN[i], BIN[target]);
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
             BIN[target], SW_PIN[target], CHUTE_TIMEOUT_MS / 1000);
        if (sorting) Serial.printf("JAM:%d\n", target + 1);
      }
      break;
    case CHUTE_CREEP:
      if (t >= (unsigned long)cfg.creepMs) chuteArrived();
      break;
    case OPENING:                                       // let the swing finish and the scale settle
      if (t >= IGNORE_AFTER_OPEN) { step = WAIT_DROP; stepStart = millis(); emptySince = 0; }
      break;
    case WAIT_DROP: {
      float limit = max(EMPTY_BELOW_G, 0.2f * gramsBefore);
      if (grams < limit) {
        if (!emptySince) emptySince = millis();
        else if (millis() - emptySince >= EMPTY_STEADY_MS) {
          note("item dropped (scale now %.1f g)", grams);
          tray.write(cfg.flat); step = CLOSING; stepStart = millis();
          note("tray CLOSED (%d deg)", cfg.flat);
        }
      } else emptySince = 0;
      if (step == WAIT_DROP && t >= DROP_TIMEOUT_MS) {
        if (!retried) {
          retried = true; tray.write(cfg.flat); step = RETRY_CLOSE; stepStart = millis();
          note("item still on the tray (%.1f g): closing and trying once more", grams);
        } else {
          note("item STUCK on the tray (%.1f g)", grams);
          Serial.printf("JAM:%d\n", target + 1); tray.write(cfg.flat); step = IDLE;
        }
      }
      break;
    }
    case RETRY_CLOSE:                                   // close, then swing again once
      if (t >= TRAY_MOVE_MS) { tray.write(cfg.open); step = OPENING; stepStart = millis(); note("tray OPEN again"); }
      break;
    case CLOSING:
      if (t >= TRAY_MOVE_MS) {
        note("sort DONE: %s, %.1f g", BIN[target], gramsBefore);
        Serial.printf("DONE:%d:%.1f\n", target + 1, gramsBefore);
        if (scaleOk && fabs(grams) < 10) scale.tare(5); // clear small drift
        readFillSensors();
        sendFill();
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
    gramsBefore = grams;
    startChute(cmd.toInt() - 1, true);
    return;
  }

  if (step != IDLE) { Serial.println("# busy, try again"); return; }

  if (cmd == "QUIET") { quiet = !quiet; Serial.printf("# weight lines %s\n", quiet ? "OFF" : "ON"); }
  else if (cmd == "LOG") { logOn = !logOn; Serial.printf("# progress lines %s\n", logOn ? "ON" : "OFF"); }
  else if (cmd == "INFO") printInfo();
  else if (cmd == "STATUS") Serial.printf("# chute at %s | switches Bio=%s Rec=%s Res=%s | weight %.1f g | scale %s\n",
      current >= 0 ? BIN[current] : "unknown", pressed(0) ? "PRESSED" : "-", pressed(1) ? "PRESSED" : "-",
      pressed(2) ? "PRESSED" : "-", grams, scaleOk ? "ok" : "NOT FOUND");
  else if (cmd.startsWith("TEST") && cmd.length() == 5 && cmd[4] >= '1' && cmd[4] <= '3') {
    gramsBefore = grams;
    note("TEST sort (same as app.py sending %c)", cmd[4]);
    startChute(cmd[4] - '1', true);
  }
  else if (cmd == "SW") Serial.printf("# Bio=%s Rec=%s Res=%s\n", pressed(0) ? "PRESSED" : "-",
                                      pressed(1) ? "PRESSED" : "-", pressed(2) ? "PRESSED" : "-");
  else if (cmd == "SAVE") { saveSettings(); Serial.println("# saved"); printInfo(); }
  else if (cmd == "RESET") { cfg = Settings(); saveSettings(); tray.write(cfg.flat);
                             if (scaleOk) scale.set_scale(cfg.factor); Serial.println("# defaults"); printInfo(); }
  else if (cmd == "FLIP") { cfg.dir = -cfg.dir; Serial.printf("# direction flipped (dir=%d), SAVE to keep\n", cfg.dir); }
  else if (cmd == "SPIN+") { chuteSpin(1);  Serial.println("# spinning forward (SPIN0 to stop)"); }
  else if (cmd == "SPIN-") { chuteSpin(-1); Serial.println("# spinning backward (SPIN0 to stop)"); }
  else if (cmd == "SPIN0") { chuteStop(); Serial.println("# stopped"); }
  else if (cmd.startsWith("GO") && cmd.length() == 3 && cmd[2] >= '1' && cmd[2] <= '3') startChute(cmd[2] - '1', false);
  else if (cmd.startsWith("Y") && cmd.length() > 1 && isDigit(cmd[1])) {
    int v = constrain(cmd.substring(1).toInt(), 0, 180); tray.write(v); Serial.printf("# tray -> %d\n", v);
  }
  else if (cmd.startsWith("K") && cmd.length() > 1) {
    float known = cmd.substring(1).toFloat();
    if (!scaleOk || known <= 0) { Serial.println("# use K100 with a known item on the tray"); return; }
    long raw = scale.get_value(20);
    if (raw == 0) { Serial.println("# no change: is the item on the tray?"); return; }
    cfg.factor = raw / known; scale.set_scale(cfg.factor);
    Serial.printf("# factor = %.1f (SAVE to keep)\n", cfg.factor);
  }
  else if (cmd.startsWith("SET ")) {
    int sp = cmd.indexOf(' ', 4);
    String name = sp > 0 ? cmd.substring(4, sp) : "";
    int v = sp > 0 ? cmd.substring(sp + 1).toInt() : 0;
    if      (name == "FLAT")  { cfg.flat = constrain(v, 0, 180); tray.write(cfg.flat); }
    else if (name == "OPEN")  { cfg.open = constrain(v, 0, 180); tray.write(cfg.open); }
    else if (name == "STOP")  { cfg.stopUs = constrain(v, 1300, 1700); }
    else if (name == "SPEED") { cfg.speedUs = constrain(v, 20, 500); }
    else if (name == "CREEP") { cfg.creepMs = constrain(v, 0, 1000); }
    else { Serial.println("# use: SET FLAT|OPEN <angle>, SET STOP|SPEED <us>, SET CREEP <ms>"); return; }
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

  ESP32PWM::allocateTimer(0);
  ESP32PWM::allocateTimer(1);
  tray.setPeriodHertz(50);
  tray.attach(TRAY_PIN, 500, 2400);
  tray.write(cfg.flat);

  if (HAS_FILL_SENSORS)
    for (int i = 0; i < 3; i++) { pinMode(TRIG_PIN[i], OUTPUT); digitalWrite(TRIG_PIN[i], LOW); pinMode(ECHO_PIN[i], INPUT); }

  scale.begin(DT_PIN, SCK_PIN);
  scaleOk = scale.wait_ready_timeout(1000);
  if (scaleOk) { scale.set_scale(cfg.factor); scale.tare(10); }   // tray must be empty at power-up

  checkChutePosition();
  readFillSensors();

  Serial.println("READY");
  if (!scaleOk) Serial.println("ERR:HX711 not found");
  sendFill();

  note("==== ECOReward bin ready ====");
  note("chute P%d (360, stop %d us, speed %d) | tray P%d (flat %d, open %d)", CHUTE_PIN, cfg.stopUs, cfg.speedUs,
       TRAY_PIN, cfg.flat, cfg.open);
  note("end switches: Bio P13=%s  Rec P17=%s  Res P21=%s", pressed(0) ? "PRESSED" : "-",
       pressed(1) ? "PRESSED" : "-", pressed(2) ? "PRESSED" : "-");
  note("chute is at: %s", current >= 0 ? BIN[current] : "between bins (first sort turns forward)");
  note("scale: %s | fill sensors: OFF (HC-SR04 not connected)", scaleOk ? "ok, zeroed" : "NOT FOUND - check DT P16, SCK P4, 3V3, G");
  note("test without app.py: TEST1 / TEST2 / TEST3.  QUIET hides the W: lines");
}

void loop() {
  readCommands();
  if (scaleOk && scale.is_ready()) grams = scale.get_units(1);
  runSteps();
  if (!quiet && millis() - lastWeight >= WEIGHT_EVERY_MS) {
    lastWeight = millis();
    Serial.printf("W:%.2f\n", grams);
  }
}