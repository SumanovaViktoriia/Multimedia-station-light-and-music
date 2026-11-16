#include <FastLED.h>

#define LED_PIN     6      // Умная лента
#define SINGLE_LED  5      // Обычный диод
#define RELAY_PIN   7      // Реле парогенератора (active LOW)
#define NUM_LEDS    30
#define FOG_PULSE_MS 100

CRGB leds[NUM_LEDS];
byte extra_data[2];        // [0] яркость диода, [1] клик тумана (0/1)

unsigned long fog_until = 0;  // до какого millis держать реле нажатым

void setup() {
  Serial.begin(115200);
  Serial.setTimeout(50);

  pinMode(SINGLE_LED, OUTPUT);

  // Реле низкого уровня: HIGH = выкл
  pinMode(RELAY_PIN, OUTPUT);
  digitalWrite(RELAY_PIN, HIGH);

  FastLED.addLeds<WS2812B, LED_PIN, GRB>(leds, NUM_LEDS);
  FastLED.clear();
  FastLED.show();
}

void loop() {
  // Неблокирующий импульс реле (без delay — иначе забьётся Serial)
  if (fog_until != 0 && millis() >= fog_until) {
    digitalWrite(RELAY_PIN, HIGH);
    fog_until = 0;
  }

  if (Serial.find("LED")) {
    Serial.readBytes((char*)leds, NUM_LEDS * 3);
    Serial.readBytes((char*)extra_data, 2);

    FastLED.show();
    analogWrite(SINGLE_LED, extra_data[0]);

    // Туман: единичка от Python → короткий клик минусом
    if (extra_data[1] == 1 && fog_until == 0) {
      digitalWrite(RELAY_PIN, LOW);
      fog_until = millis() + FOG_PULSE_MS;
    }
  }
}
