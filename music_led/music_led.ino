#include <FastLED.h>

#define LED_PIN     6      // Умная лента
#define SINGLE_LED  5      // Одиночный диод на макетке
#define NUM_LEDS    30

CRGB leds[NUM_LEDS];
byte extra_data[1];        // Коробочка для яркости диода

void setup() {
  Serial.begin(115200);
  Serial.setTimeout(50); 
  
  pinMode(SINGLE_LED, OUTPUT); // Настраиваем D5 на выход
  
  FastLED.addLeds<WS2812B, LED_PIN, GRB>(leds, NUM_LEDS);
  FastLED.clear(); 
  FastLED.show();
}

void loop() {
  if (Serial.find("LED")) {
    // 1. Читаем цвета для умной ленты (90 байт)
    Serial.readBytes((char*)leds, NUM_LEDS * 3);
    
    // 2. Читаем яркость для обычного диода (1 байт)
    Serial.readBytes((char*)extra_data, 1);
    
    FastLED.show();
    
    // Включаем обычный диод (extra_data[0] содержит число от 0 до 255)
    analogWrite(SINGLE_LED, extra_data[0]);
  }
}