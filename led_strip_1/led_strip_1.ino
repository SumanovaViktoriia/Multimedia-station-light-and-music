#include <FastLED.h> 

#define LED_PIN     6      // Сюда мы воткнем зеленый провод (пин D6)
#define NUM_LEDS    30     // Для старта включаем только 30 штук (из-за блока на 2А)
#define BRIGHTNESS  50     // Безопасная яркость (из 255)

CRGB leds[NUM_LEDS];       

void setup() {
  FastLED.addLeds<WS2812B, LED_PIN, GRB>(leds, NUM_LEDS);
  FastLED.setBrightness(BRIGHTNESS);
}

void loop() {
  uint8_t hue = millis() / 10; 
  fill_rainbow(leds, NUM_LEDS, hue, 7);
  FastLED.show(); 
  delay(20);      
}
