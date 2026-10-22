#include <FastLED.h>

#define LED_PIN     6
#define NUM_LEDS    30
#define FRAME_BYTES (NUM_LEDS * 3)

CRGB leds[NUM_LEDS];

void setup() {
  Serial.begin(115200);
  FastLED.addLeds<WS2812B, LED_PIN, GRB>(leds, NUM_LEDS);
  FastLED.clear();
  FastLED.show();
}

void loop() {
  // Ищем "LED", потом читаем ровно 90 байт цвета.
  // Без Serial.find — он плохо переживает потери байт в буфере.
  static uint8_t state = 0;
  static uint16_t got = 0;
  static uint8_t *rgb = (uint8_t *)leds;

  while (Serial.available()) {
    uint8_t b = Serial.read();

    if (state == 0) {
      state = (b == 'L') ? 1 : 0;
    } else if (state == 1) {
      state = (b == 'E') ? 2 : (b == 'L' ? 1 : 0);
    } else if (state == 2) {
      if (b == 'D') {
        state = 3;
        got = 0;
      } else {
        state = (b == 'L') ? 1 : 0;
      }
    } else {
      rgb[got++] = b;
      if (got >= FRAME_BYTES) {
        FastLED.show();
        state = 0;
      }
    }
  }
}
