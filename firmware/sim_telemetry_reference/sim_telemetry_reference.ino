// sim_telemetry_reference.ino
//
// REFERENCE FIRMWARE: shows how a microcontroller on the rig would emit the
// same frame format the Python host expects. It is a sketch to read alongside
// protocol.py, not tested production firmware. Pin assignments and sensor
// scaling are placeholders for whatever hardware the rig actually uses.
//
// Frame (little-endian), 20 bytes:
//   0xAA 0x55 | len u8 | seq u16 | timestamp_ms u32 | payload (9) | crc16 u16
// Payload: steering i16 (0.1 deg) | throttle u8 % | brake u8 % | gear i8 |
//          speed u16 (0.01 km/h) | rpm u16
// CRC-16/CCITT-FALSE over len..payload (poly 0x1021, init 0xFFFF).

const uint8_t PAYLOAD_LEN = 9;
const unsigned long PERIOD_MS = 20;  // 50 Hz

const int PIN_STEERING = A0;  // potentiometer on the steering column
const int PIN_THROTTLE = A1;  // pedal potentiometers
const int PIN_BRAKE = A2;
const int PIN_GEAR_UP = 2;    // paddle shifters, active low
const int PIN_GEAR_DOWN = 3;

uint16_t seq = 0;
int8_t gear = 1;
unsigned long nextFrameAt = 0;

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

void setup() {
  Serial.begin(115200);
  pinMode(PIN_GEAR_UP, INPUT_PULLUP);
  pinMode(PIN_GEAR_DOWN, INPUT_PULLUP);
}

void loop() {
  readShifters();
  unsigned long now = millis();
  if (now < nextFrameAt) return;
  nextFrameAt = now + PERIOD_MS;

  // Map 10-bit ADC readings to engineering units (placeholder calibration).
  int16_t steering = map(analogRead(PIN_STEERING), 0, 1023, -4500, 4500);  // +-450.0 deg
  uint8_t throttle = map(analogRead(PIN_THROTTLE), 0, 1023, 0, 100);
  uint8_t brake = map(analogRead(PIN_BRAKE), 0, 1023, 0, 100);
  uint16_t speed = 0;  // speed and rpm come from the PC game on a real rig;
  uint16_t rpm = 0;    // left at zero here to keep the sketch self-contained

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
