// sim_telemetry_reference.ino
//
// REFERENCE FIRMWARE for an Arduino Leonardo on the SIH 2025 rig's sensor set
// (see docs/hardware.md). Shows how the rig's inputs could be streamed in the
// frame format the Python host expects. It is not the firmware used at SIH and
// has not been flashed to the rig; pin numbers and calibration are examples.
//
// Frame (little-endian), 20 bytes:
//   0xAA 0x55 | len u8 | seq u16 | timestamp_ms u32 | payload (9) | crc16 u16
// Payload: steering i16 (0.1 deg) | throttle u8 % | brake u8 % | gear i8 |
//          speed u16 (0.01 km/h) | rpm u16
// CRC-16/CCITT-FALSE over len..payload (poly 0x1021, init 0xFFFF).

const uint8_t PAYLOAD_LEN = 9;
const unsigned long PERIOD_MS = 20;  // 50 Hz

// Steering: 600 PPR quadrature encoder, geared 1:4 to the wheel.
// Counting one edge of channel A gives 600 counts per encoder turn,
// so 2400 counts per wheel turn = 0.15 degree per count.
const int PIN_ENC_A = 2;  // Leonardo interrupt-capable pins: 0, 1, 2, 3, 7
const int PIN_ENC_B = 3;
const long COUNTS_PER_WHEEL_REV = 2400;

// Pedals: 20k potentiometers.
const int PIN_THROTTLE = A0;
const int PIN_BRAKE = A1;
const int PIN_CLUTCH = A2;  // wired on the rig; the v1 frame has no clutch field yet

// Paddle shifters: limit switches, active low.
const int PIN_GEAR_UP = 4;
const int PIN_GEAR_DOWN = 5;

volatile long encoderCount = 0;  // 0 = wheel centred at power-up
uint16_t seq = 0;
int8_t gear = 1;
unsigned long nextFrameAt = 0;

void onEncoderA() {
  // On a rising edge of A, B tells us the direction.
  if (digitalRead(PIN_ENC_B) == HIGH) encoderCount--;
  else encoderCount++;
}

uint16_t crc16(const uint8_t *data, size_t len) {
  uint16_t crc = 0xFFFF;
  for (size_t i = 0; i < len; i++) {
    crc ^= (uint16_t)data[i] << 8;
    for (uint8_t bit = 0; bit < 8; bit++) {
      crc = (crc & 0x8000) ? (crc << 1) ^ 0x1021 : (crc << 1);
    }
  }
  return crc;
}

void putU16(uint8_t *p, uint16_t v) { p[0] = v & 0xFF; p[1] = v >> 8; }
void putU32(uint8_t *p, uint32_t v) { for (int i = 0; i < 4; i++) p[i] = (v >> (8 * i)) & 0xFF; }

void readShifters() {
  static bool lastUp = HIGH, lastDown = HIGH;
  bool up = digitalRead(PIN_GEAR_UP), down = digitalRead(PIN_GEAR_DOWN);
  if (lastUp == HIGH && up == LOW && gear < 6) gear++;    // falling edge = press
  if (lastDown == HIGH && down == LOW && gear > -1) gear--;
  lastUp = up;
  lastDown = down;
}

uint8_t pedalPercent(int pin) {
  // Placeholder calibration: full ADC range = 0-100 %. Real pedals need
  // per-pedal min/max captured during a calibration step.
  return map(analogRead(pin), 0, 1023, 0, 100);
}

void setup() {
  Serial.begin(115200);
  pinMode(PIN_ENC_A, INPUT_PULLUP);
  pinMode(PIN_ENC_B, INPUT_PULLUP);
  pinMode(PIN_GEAR_UP, INPUT_PULLUP);
  pinMode(PIN_GEAR_DOWN, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(PIN_ENC_A), onEncoderA, RISING);
}

void loop() {
  readShifters();
  unsigned long now = millis();
  if (now < nextFrameAt) return;
  nextFrameAt = now + PERIOD_MS;

  noInterrupts();
  long count = encoderCount;
  interrupts();

  // counts -> tenths of a degree: count * 3600 / 2400
  int16_t steering = (int16_t)constrain(count * 3600L / COUNTS_PER_WHEEL_REV, -32768L, 32767L);
  uint8_t throttle = pedalPercent(PIN_THROTTLE);
  uint8_t brake = pedalPercent(PIN_BRAKE);
  uint16_t speed = 0;  // speed and rpm come from the simulator on the PC,
  uint16_t rpm = 0;    // so the rig itself sends zero

  uint8_t frame[20];
  frame[0] = 0xAA;
  frame[1] = 0x55;
  frame[2] = PAYLOAD_LEN;
  putU16(&frame[3], seq++);
  putU32(&frame[5], now);
  putU16(&frame[9], (uint16_t)steering);
  frame[11] = throttle;
  frame[12] = brake;
  frame[13] = (uint8_t)gear;
  putU16(&frame[14], speed);
  putU16(&frame[16], rpm);
  putU16(&frame[18], crc16(&frame[2], 16));  // len..payload = 16 bytes

  Serial.write(frame, sizeof(frame));
}
