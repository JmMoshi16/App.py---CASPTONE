// Simple servo test - chute (G18) and tray (G19)
// Library: ESP32Servo
#include <ESP32Servo.h>

Servo chute;   // G18
Servo tray;    // G19

void setup() {
  Serial.begin(115200);
  ESP32PWM::allocateTimer(0);
  chute.setPeriodHertz(50);
  tray.setPeriodHertz(50);
  chute.attach(18, 500, 2400);
  tray.attach(19, 500, 2400);
  Serial.println("=== Servo test ===");
}

void loop() {
  // 0 -> 180
  for (int a = 0; a <= 180; a += 2) {
    chute.write(a);
    tray.write(a);
    delay(20);
  }
  Serial.println("At 180");
  delay(1000);

  // 180 -> 0
  for (int a = 180; a >= 0; a -= 2) {
    chute.write(a);
    tray.write(a);
    delay(20);
  }
  Serial.println("At 0");
  delay(1000);
}