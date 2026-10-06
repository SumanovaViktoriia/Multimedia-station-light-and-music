// скорость: меньше — быстрее
int t = 80;

void ledOn(int pin) {
  pinMode(pin, INPUT_PULLUP);
}
void ledOff(int pin) {
  pinMode(pin, INPUT);
  digitalWrite(pin, LOW);
}

void setup() {
}

void loop() {
  // волна
  for (int i = 0; i < 3; i = i + 1) {
    ledOn(2); delay(t); ledOff(2);
    ledOn(3); delay(t); ledOff(3);
    ledOn(4); delay(t); ledOff(4);
    ledOn(3); delay(t); ledOff(3);
  }

  delay(t * 4);

  // бит
  for (int i = 0; i < 4; i = i + 1) {
    ledOn(2); ledOn(4); delay(t / 2);
    ledOff(2); ledOff(4); delay(t);
    ledOn(2); ledOn(4); delay(t / 2);
    ledOff(2); ledOff(4); delay(t * 3);

    ledOn(3); delay(t); ledOff(3);
    delay(t * 3);
  }

  // стробоскоп
  for (int i = 0; i < 10; i = i + 1) {
    ledOn(2); ledOn(3); ledOn(4);
    delay(t / 3);

    ledOff(2); ledOff(3); ledOff(4);
    delay(t / 2);
  }

  delay(t * 15);
}
