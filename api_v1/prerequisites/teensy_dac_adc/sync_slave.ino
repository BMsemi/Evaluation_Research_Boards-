// ================================================================
// sync_slave.ino
//
// Teensy-side implementation of the Caravel <-> Teensy GPIO
// handshake described in the synced READ/SET/RESET scheme.
// Caravel is the packet master; the Teensy is the measurement
// slave. This file only adds the sync state machine as a new
// serial command ("SYNC") on top of the existing sketch -- it
// does not change any of the existing RSRR/SET/CLEAR behavior.
//
// Pin mapping (Teensy 4.1 digital pins, matches synced_mode_wb.c):
//
//   Signal           Direction            Teensy pin   Caravel
//   ADC_READY        Teensy -> Caravel    5 (OUTPUT)   GPIO18
//   MODE0            Caravel -> Teensy    7 (INPUT)    GPIO19
//   MODE1            Caravel -> Teensy    21 (INPUT)   GPIO20
//   CAPTURE_ACTIVE   Caravel -> Teensy    18 (INPUT)   GPIO23
//   GND              both                 GND          GND
//
// Pins 2, 3, 4, 6 are already used elsewhere in this sketch
// (DAC1 chip-select, DAC2 chip-select, ldac_Pin, ldacPin), and
// pin 9 is occupied on this board, so the sync signals were
// deliberately routed around all of them.
//
// Mode encoding (must match synced_mode_wb.c):
//   MODE1 MODE0
//     0     1   = READ
//     1     0   = SET
//     1     1   = RESET
//     0     0   = IDLE
// ================================================================

static const uint8_t SYNC_PIN_ADC_READY      = 5;   // OUTPUT to Caravel
static const uint8_t SYNC_PIN_MODE0          = 7;   // INPUT from Caravel
static const uint8_t SYNC_PIN_MODE1          = 21;  // INPUT from Caravel
static const uint8_t SYNC_PIN_CAPTURE_ACTIVE = 18;  // INPUT from Caravel

static const uint8_t SYNC_MODE_IDLE  = 0;
static const uint8_t SYNC_MODE_READ  = 1;
static const uint8_t SYNC_MODE_SET   = 2;
static const uint8_t SYNC_MODE_RESET = 3;

// DAC channel + target voltage per mode. Reuses the same channels
// already used by runReadSetReadResetSequence() (dac[2]=READ,
// dac[0]=SET, dac[5]=RESET) so results stay comparable.
static const int SYNC_DEFAULT_READ_MV  = 0;
static const int SYNC_DEFAULT_SET_MV   = 1500;
static const int SYNC_DEFAULT_RESET_MV = 0;
static const unsigned long SYNC_DEFAULT_DAC_SETTLE_US = 5000;

static int syncReadMv = SYNC_DEFAULT_READ_MV;
static int syncSetMv = SYNC_DEFAULT_SET_MV;
static int syncResetMv = SYNC_DEFAULT_RESET_MV;
static unsigned long syncDacSettleUs = SYNC_DEFAULT_DAC_SETTLE_US;

static const unsigned long SYNC_SAMPLE_PERIOD_US = 1000; // ADC poll period while active
static const unsigned long SYNC_TIMEOUT_MS = 180000;     // safety timeout waiting on Caravel
static const uint8_t SYNC_TARGET_MONITOR_SAMPLES = 38;   // host summary also includes final sample

void syncSlaveInit() {
  pinMode(SYNC_PIN_ADC_READY, OUTPUT);
  digitalWrite(SYNC_PIN_ADC_READY, LOW);
  pinMode(SYNC_PIN_MODE0, INPUT_PULLDOWN);
  pinMode(SYNC_PIN_MODE1, INPUT_PULLDOWN);
  pinMode(SYNC_PIN_CAPTURE_ACTIVE, INPUT_PULLDOWN);
}

static uint8_t syncReadMode() {
  uint8_t m0 = digitalRead(SYNC_PIN_MODE0) ? 1 : 0;
  uint8_t m1 = digitalRead(SYNC_PIN_MODE1) ? 1 : 0;
  return (m1 << 1) | m0;
}

static const char *syncModeName(uint8_t mode) {
  switch (mode) {
    case SYNC_MODE_READ:  return "READ";
    case SYNC_MODE_SET:   return "SET";
    case SYNC_MODE_RESET: return "RESET";
    default:              return "IDLE";
  }
}

static void syncDacForMode(uint8_t mode, bool apply) {
  int mv = 0;
  uint8_t channel = dac[2]; // default READ channel

  switch (mode) {
    case SYNC_MODE_READ:  channel = dac[2]; mv = apply ? syncReadMv  : 0; break;
    case SYNC_MODE_SET:   channel = dac[0]; mv = apply ? syncSetMv   : 0; break;
    case SYNC_MODE_RESET: channel = dac[5]; mv = apply ? syncResetMv : 0; break;
    default: return;
  }

  writeDacMilliVolts(channel, mv);
}

static void syncDacForPacket(const char *packetLabel, bool apply) {
  int mv = 0;
  uint8_t channel = dac[2];

  if (strcmp(packetLabel, "FORM") == 0 || strcmp(packetLabel, "SET") == 0) {
    channel = dac[0];
    mv = apply ? syncSetMv : 0;
  } else if (strcmp(packetLabel, "RESET") == 0) {
    channel = dac[5];
    mv = apply ? syncResetMv : 0;
  } else {
    channel = dac[2];
    mv = apply ? syncReadMv : 0;
  }

  writeDacMilliVolts(channel, mv);
}

static bool syncReadKeyMv(const String &command, const char *key, int *outMv) {
  int keyStart = command.indexOf(key);
  if (keyStart < 0) {
    return false;
  }
  int valueStart = keyStart + strlen(key);
  int valueEnd = command.indexOf(' ', valueStart);
  String value = (valueEnd < 0) ? command.substring(valueStart) : command.substring(valueStart, valueEnd);
  value.trim();
  if (value.length() == 0) {
    return false;
  }
  *outMv = value.toInt();
  return true;
}

static bool syncReadKeyUlong(const String &command, const char *key, unsigned long *outValue) {
  int keyStart = command.indexOf(key);
  if (keyStart < 0) {
    return false;
  }
  int valueStart = keyStart + strlen(key);
  int valueEnd = command.indexOf(' ', valueStart);
  String value = (valueEnd < 0) ? command.substring(valueStart) : command.substring(valueStart, valueEnd);
  value.trim();
  if (value.length() == 0) {
    return false;
  }
  *outValue = (unsigned long)value.toInt();
  return true;
}

static void syncApplyCommandConfig(const String &command) {
  int mv = 0;
  unsigned long settleUs = 0;

  if (syncReadKeyMv(command, "READ_MV=", &mv)) {
    syncReadMv = mv;
  }
  if (syncReadKeyMv(command, "SET_MV=", &mv)) {
    syncSetMv = mv;
  }
  if (syncReadKeyMv(command, "RESET_MV=", &mv)) {
    syncResetMv = mv;
  }
  if (syncReadKeyUlong(command, "SETTLE_US=", &settleUs)) {
    syncDacSettleUs = settleUs;
  }
}

// MODE requests the upcoming packet and lets the DAC settle before
// CAPTURE_ACTIVE rises. This keeps the DAC turn-on transient outside
// the Saleae operation window while retaining the same four wires.
static bool syncServiceOnePacket(const char *packetLabel) {
  unsigned long waitStart = millis();
  while (syncReadMode() == SYNC_MODE_IDLE) {
    if (millis() - waitStart > SYNC_TIMEOUT_MS) {
      Serial.println("SYNC_TIMEOUT waiting_for=MODE");
      return false;
    }
  }

  delayMicroseconds(50);
  uint8_t mode = syncReadMode();
  digitalWrite(SYNC_PIN_ADC_READY, LOW);
  Serial.print("SYNC_BEGIN packet=");
  Serial.print(packetLabel);
  Serial.print(" mode=");
  Serial.println(syncModeName(mode));

  // 1. Apply the DAC voltage for this mode and let it settle.
  syncDacForPacket(packetLabel, true);
  delayMicroseconds(syncDacSettleUs);

  // 2. Baseline ADC sample, then tell Caravel we're ready.
  ShuntCurrents baseline = readMonitoredShunts();
  printMonitorSample(packetLabel, 0, baseline);
  digitalWrite(SYNC_PIN_ADC_READY, HIGH);

  // 3. Caravel opens the operation-only capture after seeing ADC_READY.
  while (digitalRead(SYNC_PIN_CAPTURE_ACTIVE) == LOW) {
    if (millis() - waitStart > SYNC_TIMEOUT_MS) {
      Serial.print("SYNC_TIMEOUT packet=");
      Serial.print(packetLabel);
      Serial.println(" waiting_for=CAPTURE_ACTIVE_HIGH");
      syncDacForPacket(packetLabel, false);
      digitalWrite(SYNC_PIN_ADC_READY, LOW);
      return false;
    }
  }

  // 4. Keep sampling for a fixed sample count. Caravel keeps
  // CAPTURE_ACTIVE high until ADC_READY drops, so the sample count
  // stays comparable when the Caravel clock frequency changes.
  unsigned long windowStart = micros();
  uint8_t lastMode = mode;
  uint8_t monitorSamples = 1; // baseline sample above
  bool measurementDone = false;
  while (digitalRead(SYNC_PIN_CAPTURE_ACTIVE) == HIGH) {
    if (millis() - waitStart > SYNC_TIMEOUT_MS) {
      Serial.print("SYNC_TIMEOUT packet=");
      Serial.print(packetLabel);
      Serial.println(" waiting_for=CAPTURE_ACTIVE_LOW");
      syncDacForPacket(packetLabel, false);
      digitalWrite(SYNC_PIN_ADC_READY, LOW);
      return false;
    }
    uint8_t currentMode = syncReadMode();
    if (currentMode != lastMode) {
      Serial.print("SYNC_MODE_CHANGE packet=");
      Serial.print(packetLabel);
      Serial.print(" t_us=");
      Serial.print(micros() - windowStart);
      Serial.print(" mode=");
      Serial.println(syncModeName(currentMode));
      lastMode = currentMode;
    }

    if (measurementDone) {
      continue;
    }

    ShuntCurrents sample = readMonitoredShunts();
    printMonitorSample(packetLabel, micros() - windowStart, sample);
    monitorSamples++;
    if (monitorSamples >= SYNC_TARGET_MONITOR_SAMPLES) {
      digitalWrite(SYNC_PIN_ADC_READY, LOW);
      measurementDone = true;
      Serial.print("SYNC_MEASURE_DONE packet=");
      Serial.print(packetLabel);
      Serial.print(" samples=");
      Serial.print(monitorSamples);
      Serial.print(" t_us=");
      Serial.println(micros() - windowStart);
      continue;
    }
    delayMicroseconds(SYNC_SAMPLE_PERIOD_US);
  }

  // 5. Capture window closed: zero the DAC, take the final sample.
  unsigned long captureLowUs = micros() - windowStart;
  Serial.print("SYNC_CAPTURE_LOW packet=");
  Serial.print(packetLabel);
  Serial.print(" t_us=");
  Serial.println(captureLowUs);

  if (!measurementDone) {
    Serial.print("SYNC_CAPTURE_EARLY_CLOSE packet=");
    Serial.print(packetLabel);
    Serial.print(" samples=");
    Serial.println(monitorSamples);
    digitalWrite(SYNC_PIN_ADC_READY, LOW);
  }
  syncDacForPacket(packetLabel, false);
  ShuntCurrents final_sample = readMonitoredShunts();
  Serial.print("SYNC_FINAL_SAMPLE packet=");
  Serial.print(packetLabel);
  Serial.print(" mode=");
  Serial.print(syncModeName(mode));
  Serial.print(" read_uA=");
  Serial.print(final_sample.readUa, 4);
  Serial.print(" set_uA=");
  Serial.print(final_sample.setUa, 4);
  Serial.print(" reset_uA=");
  Serial.println(final_sample.resetUa, 4);

  // 6. Tell Caravel we're done so it can start the next packet.
  digitalWrite(SYNC_PIN_ADC_READY, LOW);

  // Do not let the next packet slot consume the previous MODE value.
  // Caravel clears MODE immediately after CAPTURE_ACTIVE falls, but
  // the Teensy can otherwise return to the outer loop first.
  while (syncReadMode() != SYNC_MODE_IDLE) {
    if (millis() - waitStart > SYNC_TIMEOUT_MS) {
      Serial.print("SYNC_TIMEOUT packet=");
      Serial.print(packetLabel);
      Serial.println(" waiting_for=MODE_IDLE");
      return false;
    }
  }

  Serial.print("SYNC_END mode=");
  Serial.println(syncModeName(mode));
  return true;
}

// Entry point for the "SYNC" serial command. Services the fixed
// FORM -> READ1 -> SET -> READ2 -> RESET -> READ3 sequence that
// synced_mode_wb.c drives on the Caravel side, then returns
// control to loop().
void runSyncSlaveSequence(const String &syncCommand) {
  static const char *packetLabels[] = {"FORM", "READ1", "SET", "READ2", "RESET", "READ3"};
  syncApplyCommandConfig(syncCommand);
  Serial.print("SYNC_SEQUENCE_START packets=6 read_mV=");
  Serial.print(syncReadMv);
  Serial.print(" set_mV=");
  Serial.print(syncSetMv);
  Serial.print(" reset_mV=");
  Serial.print(syncResetMv);
  Serial.print(" settle_us=");
  Serial.println(syncDacSettleUs);

  // A previous interrupted Caravel run may leave MODE or CAPTURE_ACTIVE
  // asserted. Require a clean idle phase before accepting this run's FORM.
  unsigned long idleWaitStart = millis();
  while (syncReadMode() != SYNC_MODE_IDLE || digitalRead(SYNC_PIN_CAPTURE_ACTIVE) == HIGH) {
    if (millis() - idleWaitStart > SYNC_TIMEOUT_MS) {
      Serial.println("SYNC_TIMEOUT waiting_for=INITIAL_IDLE");
      digitalWrite(SYNC_PIN_ADC_READY, LOW);
      Serial.println("SYNC_SEQUENCE_ABORTED");
      return;
    }
  }

  for (int i = 0; i < 6; i++) {
    if (!syncServiceOnePacket(packetLabels[i])) {
      Serial.println("SYNC_SEQUENCE_ABORTED");
      digitalWrite(SYNC_PIN_ADC_READY, LOW);
      return;
    }
  }
  Serial.println("SYNC_SEQUENCE_DONE");
}

void runSyncReadyPulseDiag() {
  Serial.println("READY_PULSE_START pin=5 signal=ADC_READY");
  for (int i = 0; i < 5; i++) {
    digitalWrite(SYNC_PIN_ADC_READY, HIGH);
    Serial.println("READY_PULSE state=HIGH");
    delay(500);
    digitalWrite(SYNC_PIN_ADC_READY, LOW);
    Serial.println("READY_PULSE state=LOW");
    delay(500);
  }
  Serial.println("READY_PULSE_DONE");
}
