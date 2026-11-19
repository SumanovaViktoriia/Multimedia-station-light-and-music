#include <FastLED.h>
#include <EEPROM.h>

#define LED_PIN      6      // Умная лента
#define SINGLE_LED   5      // Обычный диод
#define RELAY_PIN    7      // Реле парогенератора (active LOW)
#define EE_STEAM     0      // Адрес в EEPROM: 1 — плата считает, что пар идёт
#define PROTOCOL     4      // Версия скетча; 4 — помнит пар между запусками
#define MAX_LEDS     60     // Сколько диодов на ленте физически (можно с запасом, до 250)
#define FOG_PULSE_MS 100    // Длина нажатия кнопки
#define FOG_GAP_MS   300    // Пауза между нажатиями
#define SILENCE_MS   2000   // Столько мс нет кадров — всё гасим
#define FRAME_GAP_MS 100    // Кадр оборвался — ждём следующий
#define STEAM_STOP   201    // Значение "пар" в кадре: выключить сейчас
#define STEAM_IS_ON  202    // ...пар сейчас идёт (плата не знала) — выключить
#define STEAM_IS_OFF 203    // ...пар сейчас выключен (плата думала, что идёт) — запомнить

// Кадр: "LED", N, N*3 байт RGB, диод, пар, XOR всех байт после "LED".
// Пар: 0 — ничего, 1..200 — включить на N/10 секунд (плата сама выключит),
//      201 — выключить сейчас, 202 / 203 — сверить состояние (см. выше).
// Кнопка парогенератора переключает его: нажатие — включился, ещё нажатие — выключился.
// Что плата считает, пар идёт или нет, она хранит в EEPROM: после перезагрузки
// (а Uno перезагружается при каждом запуске программы) пар, который остался
// включённым, выключится сам.
// N = 255 — вопрос "кто ты", отвечаем READY <MAX_LEDS> <PROTOCOL>.

CRGB leds[MAX_LEDS];

byte stage = 0;             // 0-2 ждём 'L','E','D'; 3 — N; 4 — данные; 5 — XOR
byte count = 0;             // диодов в кадре
unsigned int pos = 0;       // сколько байт данных уже принято
byte crc = 0;
byte extra[2];              // [0] яркость диода, [1] пар

bool alive = false;         // что-то сейчас включено
byte relay_state = 0;       // 0 — свободно, 1 — нажато, 2 — пауза после нажатия
bool steam_on = false;      // пар идёт (каждое нажатие его переключает)
bool steam_stop = false;    // надо выключить пар
unsigned long steam_start = 0;  // когда включили
unsigned long steam_len = 0;    // на сколько мс
unsigned long relay_time = 0;   // когда реле нажали / отпустили
unsigned long last_frame = 0;
unsigned long last_byte = 0;

void hello() {
  Serial.print("READY ");
  Serial.print(MAX_LEDS);
  Serial.print(" ");
  Serial.println(PROTOCOL);
}

void setSteam(bool on) {
  steam_on = on;
  EEPROM.update(EE_STEAM, on ? 1 : 0);   // пишет только при смене; ресурс 100 000 записей
}

void click() {
  digitalWrite(RELAY_PIN, LOW);
  relay_state = 1;
  relay_time = millis();
  setSteam(!steam_on);
}

void relayTick(unsigned long now) {
  // время вышло — выключаем сами, даже если ПК молчит
  if (steam_on && !steam_stop && now - steam_start >= steam_len) steam_stop = true;

  if (relay_state == 1 && now - relay_time >= FOG_PULSE_MS) {
    digitalWrite(RELAY_PIN, HIGH);
    relay_state = 2;
    relay_time = now;
  } else if (relay_state == 2 && now - relay_time >= FOG_GAP_MS) {
    relay_state = 0;
  } else if (relay_state == 0 && steam_stop) {
    steam_stop = false;
    if (steam_on) click();
  }
}

void steamCommand(byte v) {
  if (v == STEAM_STOP) {
    steam_stop = true;
  } else if (v == STEAM_IS_ON) {
    setSteam(true);              // не нажимаем: пар уже идёт
    steam_start = millis();
    steam_len = 0;               // время вышло — ниже в relayTick сразу нажмём и выключим
  } else if (v == STEAM_IS_OFF) {
    setSteam(false);
    steam_stop = false;
  } else if (v >= 1 && v <= 200 && !steam_stop) {
    steam_start = millis();
    steam_len = (unsigned long)v * 100UL;          // новый дроп продлевает пар
    if (!steam_on && relay_state == 0) click();    // уже идёт — второй раз не жмём
  }
}

void allOff() {
  FastLED.clear();
  FastLED.show();
  analogWrite(SINGLE_LED, 0);
  steam_stop = true;
  alive = false;
}

// Кадр принят целиком и XOR сошёлся
void applyFrame() {
  for (int i = count; i < MAX_LEDS; i++) leds[i] = CRGB::Black;  // хвост ленты гасим
  FastLED.show();
  analogWrite(SINGLE_LED, extra[0]);
  steamCommand(extra[1]);
  last_frame = millis();
  alive = true;
}

void feed(byte b) {
  switch (stage) {
    case 0:
      if (b == 'L') stage = 1;
      break;
    case 1:
      stage = (b == 'E') ? 2 : (b == 'L') ? 1 : 0;
      break;
    case 2:
      stage = (b == 'D') ? 3 : (b == 'L') ? 1 : 0;
      break;
    case 3:
      if (b == 255) {
        hello();
        stage = 0;
      } else if (b > MAX_LEDS) {
        stage = 0;
      } else {
        count = b;
        crc = b;
        pos = 0;
        stage = 4;
      }
      break;
    case 4: {
      unsigned int rgb_len = count * 3;
      crc ^= b;
      if (pos < rgb_len) ((byte*)leds)[pos] = b;
      else extra[pos - rgb_len] = b;
      pos++;
      if (pos == rgb_len + 2) stage = 5;
      break;
    }
    case 5:
      if (b == crc) applyFrame();   // битый кадр просто выбрасываем
      stage = 0;
      break;
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(SINGLE_LED, OUTPUT);
  // Реле низкого уровня: HIGH = выкл. Сначала HIGH, потом OUTPUT — без щелчка
  digitalWrite(RELAY_PIN, HIGH);
  pinMode(RELAY_PIN, OUTPUT);
  FastLED.addLeds<WS2812B, LED_PIN, GRB>(leds, MAX_LEDS);
  FastLED.setMaxPowerInVoltsAndMilliamps(5, 1800);  // блок 2 А
  steam_on = (EEPROM.read(EE_STEAM) == 1);   // пустой EEPROM читается как 255 — это «выключен»
  allOff();                                   // если пар остался включённым — выключит
  hello();
}

void loop() {
  unsigned long now = millis();

  relayTick(now);
  // Программа на ПК закрылась или зависла
  if (alive && now - last_frame > SILENCE_MS) allOff();
  if (stage != 0 && now - last_byte > FRAME_GAP_MS) stage = 0;

  while (Serial.available()) {
    feed(Serial.read());
    last_byte = millis();
  }
}
