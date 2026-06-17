#include <ctype.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define X_PLS 2
#define X_DIR 3
#define Y_PLS 4
#define Y_DIR 5
#define Z_PLS 6
#define Z_DIR 7

const uint8_t SENSOR_PINS[] = {22, 23, 24, 25, 26, 27, 28, 29, 30};
const uint8_t ALM_PINS[] = {31, 32, 33};

const uint8_t AXIS_COUNT = 3;
const uint8_t SENSOR_COUNT = 9;
const uint8_t CMD_BUFFER_SIZE = 64;
const uint8_t STEP_BATCH_SIZE = 10;
const long DEFAULT_JOG_STEPS = 50;
const unsigned long IDLE_REPORT_INTERVAL_MS = 500;
const unsigned long MOVING_REPORT_INTERVAL_MS = 200;
const uint16_t SPEED_DELAYS_US[] = {0, 2000, 1000, 500, 300, 200, 150, 100, 50, 25};
const unsigned long HOME_FAST_TIMEOUT_MS = 60000;
const unsigned long HOME_SLOW_TIMEOUT_MS = 30000;
const uint16_t HOME_FAST_DELAY_US = 30;
const uint16_t HOME_RETRACT_DELAY_US = 100;
const uint16_t HOME_SLOW_DELAY_US = 300;
const long HOME_BOUNCE_STEPS = 50;

enum MachineState {
  STATE_IDLE,
  STATE_READY,
  STATE_HOMING,
  STATE_MOVING,
  STATE_ERROR,
  STATE_ESTOP
};

enum HomePhase : uint8_t {
  HOME_IDLE = 0,
  HOME_Z_FAST_FIND,
  HOME_Z_RETRACT,
  HOME_Z_SLOW_FIND,
  HOME_X_FAST_FIND,
  HOME_X_RETRACT,
  HOME_X_SLOW_FIND,
  HOME_Y_FAST_FIND,
  HOME_Y_RETRACT,
  HOME_Y_SLOW_FIND,
  HOME_COMPLETE
};

struct Axis {
  char name;
  uint8_t plsPin;
  uint8_t dirPin;
  uint8_t negLimitPin;
  uint8_t homePin;
  uint8_t posLimitPin;
  bool invertDir;
  bool homeDirectionPositive;
  uint16_t stepDelayUs;
  long position;
  long remainingSteps;
  bool directionPositive;
  bool active;
};

Axis axes[AXIS_COUNT] = {
  {'X', X_PLS, X_DIR, 22, 23, 24, true, false, SPEED_DELAYS_US[4], 0, 0, true, false},
  {'Y', Y_PLS, Y_DIR, 25, 26, 27, false, false, SPEED_DELAYS_US[4], 0, 0, true, false},
  {'Z', Z_PLS, Z_DIR, 28, 29, 30, false, true, SPEED_DELAYS_US[4], 0, 0, true, false}
};

MachineState currentState = STATE_IDLE;
bool homed = false;
uint8_t currentSpeed = 4;
char cmdBuffer[CMD_BUFFER_SIZE];
uint8_t cmdLen = 0;
bool discardUntilNewline = false;
unsigned long lastReportMs = 0;
char lastFault[24] = "";
uint8_t homePhase = HOME_IDLE;
unsigned long homePhaseStartMs = 0;
bool homeSingleAxis = false;
uint8_t homeSingleAxisIndex = 0;
bool moveInProgress = false;

int axisIndexFromChar(char c) {
  switch (toupper(static_cast<unsigned char>(c))) {
    case 'X':
      return 0;
    case 'Y':
      return 1;
    case 'Z':
      return 2;
    default:
      return -1;
  }
}

const char *stateName(MachineState state) {
  switch (state) {
    case STATE_IDLE:
      return "IDLE";
    case STATE_READY:
      return "READY";
    case STATE_HOMING:
      return "HOMING";
    case STATE_MOVING:
      return "MOVING";
    case STATE_ERROR:
      return "ERROR";
    case STATE_ESTOP:
      return "ESTOP";
    default:
      return "UNKNOWN";
  }
}

MachineState stateAfterMotion() {
  return homed ? STATE_READY : STATE_IDLE;
}

void respondOk() {
  Serial.println("OK");
}

void respondOk(const char *detail) {
  Serial.print("OK ");
  Serial.println(detail);
}

void respondErr(const char *code) {
  Serial.print("ERR:");
  Serial.println(code);
}

void emitState() {
  Serial.print("@STATE ");
  Serial.println(stateName(currentState));
}

void setState(MachineState nextState) {
  if (currentState == nextState) {
    return;
  }
  currentState = nextState;
  emitState();
}

bool anyAxisActive() {
  for (uint8_t i = 0; i < AXIS_COUNT; i++) {
    if (axes[i].active) {
      return true;
    }
  }
  return false;
}

void stopAxis(Axis &axis) {
  axis.active = false;
  axis.remainingSteps = 0;
}

void stopAllAxes() {
  for (uint8_t i = 0; i < AXIS_COUNT; i++) {
    stopAxis(axes[i]);
  }
}

bool sensorTriggered(uint8_t pin) {
  return digitalRead(pin) == HIGH;
}

bool alarmTriggered(uint8_t pin) {
  return digitalRead(pin) == LOW;
}

bool limitTriggeredForAxis(const Axis &axis, bool directionPositive) {
  return sensorTriggered(directionPositive ? axis.posLimitPin : axis.negLimitPin);
}

void readSensorBits(char *buffer) {
  for (uint8_t i = 0; i < SENSOR_COUNT; i++) {
    buffer[i] = sensorTriggered(SENSOR_PINS[i]) ? '1' : '0';
  }
  buffer[SENSOR_COUNT] = '\0';
}

void readAlarmBits(char *buffer) {
  for (uint8_t i = 0; i < AXIS_COUNT; i++) {
    buffer[i] = alarmTriggered(ALM_PINS[i]) ? '1' : '0';
  }
  buffer[AXIS_COUNT] = '\0';
}

void emitPos() {
  Serial.print("@POS ");
  Serial.print(axes[0].position);
  Serial.print(",");
  Serial.print(axes[1].position);
  Serial.print(",");
  Serial.println(axes[2].position);
}

void emitSens() {
  char bits[SENSOR_COUNT + 1];
  readSensorBits(bits);
  Serial.print("@SENS ");
  Serial.println(bits);
}

void emitAlm() {
  char bits[AXIS_COUNT + 1];
  readAlarmBits(bits);
  Serial.print("@ALM ");
  Serial.println(bits);
}

void rememberFault(const char *reason) {
  strncpy(lastFault, reason, sizeof(lastFault) - 1);
  lastFault[sizeof(lastFault) - 1] = '\0';
}

void clearFault() {
  lastFault[0] = '\0';
}

void emitFault(const char *reason) {
  rememberFault(reason);
  Serial.print("@FAULT ");
  Serial.println(reason);
}

void buildLimitReason(uint8_t axisIndex, bool directionPositive, char *buffer, size_t bufferSize) {
  snprintf(buffer, bufferSize, "LIMIT_%c%c", axes[axisIndex].name, directionPositive ? '+' : '-');
}

void triggerLimitFault(uint8_t axisIndex, bool directionPositive) {
  char reason[24];
  stopAllAxes();
  homePhase = HOME_IDLE;
  moveInProgress = false;
  homed = false;

  Serial.print("@LIMIT ");
  Serial.print(axes[axisIndex].name);
  Serial.println(directionPositive ? '+' : '-');

  buildLimitReason(axisIndex, directionPositive, reason, sizeof(reason));
  emitFault(reason);
  setState(STATE_ERROR);
}

void applySpeedToAllAxes(uint8_t speedLevel) {
  currentSpeed = speedLevel;
  for (uint8_t i = 0; i < AXIS_COUNT; i++) {
    axes[i].stepDelayUs = SPEED_DELAYS_US[speedLevel];
  }
}

uint8_t homeAxisIndex(uint8_t phase) {
  if (phase <= HOME_Z_SLOW_FIND) return 2;
  if (phase <= HOME_X_SLOW_FIND) return 0;
  return 1;
}

uint8_t homeStageType(uint8_t phase) {
  return ((phase - 1) % 3) + 1;
}

unsigned long homeTimeout(uint8_t phase) {
  return (homeStageType(phase) == 1) ? HOME_FAST_TIMEOUT_MS : HOME_SLOW_TIMEOUT_MS;
}

void beginHomePhase(uint8_t phase) {
  homePhase = phase;
  homePhaseStartMs = millis();

  uint8_t ai = homeAxisIndex(phase);
  Axis &axis = axes[ai];
  uint8_t stage = homeStageType(phase);
  bool homeDir = axis.homeDirectionPositive;

  switch (stage) {
    case 1:
      axis.stepDelayUs = HOME_FAST_DELAY_US;
      axis.directionPositive = homeDir;
      break;
    case 2:
      axis.stepDelayUs = HOME_RETRACT_DELAY_US;
      axis.directionPositive = !homeDir;
      break;
    case 3:
      axis.stepDelayUs = HOME_SLOW_DELAY_US;
      axis.directionPositive = homeDir;
      break;
  }
}

void advanceHomePhase() {
  uint8_t ai = homeAxisIndex(homePhase);
  uint8_t stage = homeStageType(homePhase);

  if (stage == 3) {
    axes[ai].position = 0;
  }

  if (homeSingleAxis) {
    if (stage == 3) {
      homePhase = HOME_IDLE;
      Serial.print("@HOME_OK ");
      Serial.println(axes[ai].name);
      setState(stateAfterMotion());
      return;
    }
    beginHomePhase(homePhase + 1);
    return;
  }

  uint8_t nextPhase = homePhase + 1;
  if (nextPhase > HOME_Y_SLOW_FIND) {
    homePhase = HOME_IDLE;
    homed = true;
    applySpeedToAllAxes(currentSpeed);
    Serial.println("@HOME_OK");
    setState(STATE_READY);
    return;
  }

  beginHomePhase(nextPhase);
}

void startJog(uint8_t axisIndex, bool directionPositive, long steps) {
  Axis &axis = axes[axisIndex];
  axis.directionPositive = directionPositive;
  axis.remainingSteps = steps;
  axis.active = true;
}

void doStepBatch(Axis &axis, long steps) {
  bool dirLevel = axis.directionPositive;
  if (axis.invertDir) {
    dirLevel = !dirLevel;
  }

  digitalWrite(axis.dirPin, dirLevel ? HIGH : LOW);
  delayMicroseconds(10);

  for (long i = 0; i < steps; i++) {
    digitalWrite(axis.plsPin, HIGH);
    delayMicroseconds(axis.stepDelayUs);
    digitalWrite(axis.plsPin, LOW);
    delayMicroseconds(axis.stepDelayUs);
    axis.position += axis.directionPositive ? 1 : -1;
  }
}

void normalizeCommand(char *line) {
  size_t len = strlen(line);
  while (len > 0 && isspace(static_cast<unsigned char>(line[len - 1]))) {
    line[--len] = '\0';
  }

  size_t start = 0;
  while (line[start] != '\0' && isspace(static_cast<unsigned char>(line[start]))) {
    start++;
  }

  if (start > 0) {
    memmove(line, line + start, strlen(line + start) + 1);
  }

  for (char *p = line; *p != '\0'; ++p) {
    *p = static_cast<char>(toupper(static_cast<unsigned char>(*p)));
  }
}

bool parsePositiveLongToken(const char *token, long &value) {
  if (token == NULL || *token == '\0') {
    return false;
  }

  const char *start = token;
  if (token[0] == 'N') {
    start = token + 1;
  }
  if (*start == '\0') {
    return false;
  }

  char *end = NULL;
  long parsed = strtol(start, &end, 10);
  if (*end != '\0' || parsed <= 0) {
    return false;
  }

  value = parsed;
  return true;
}

bool parseAxisDirToken(const char *token, int &axisIndex, bool &directionPositive) {
  if (token == NULL || strlen(token) != 2) {
    return false;
  }

  axisIndex = axisIndexFromChar(token[0]);
  if (axisIndex < 0) {
    return false;
  }

  if (token[1] == '+') {
    directionPositive = true;
    return true;
  }
  if (token[1] == '-') {
    directionPositive = false;
    return true;
  }
  return false;
}

void handleStateQuery(char *extra) {
  if (extra != NULL) {
    respondErr("PARAM");
    return;
  }

  Serial.print("OK STATE ");
  Serial.println(stateName(currentState));
}

void handlePosQuery(char *extra) {
  if (extra != NULL) {
    respondErr("PARAM");
    return;
  }

  Serial.print("OK POS ");
  Serial.print(axes[0].position);
  Serial.print(",");
  Serial.print(axes[1].position);
  Serial.print(",");
  Serial.println(axes[2].position);
}

void handleSensQuery(char *extra) {
  if (extra != NULL) {
    respondErr("PARAM");
    return;
  }

  char bits[SENSOR_COUNT + 1];
  readSensorBits(bits);
  Serial.print("OK SENS ");
  Serial.println(bits);
}

void handleSpeedCommand(char *arg, char *extra) {
  if (arg == NULL || extra != NULL) {
    respondErr("PARAM");
    return;
  }

  long speedLevel = 0;
  if (!parsePositiveLongToken(arg, speedLevel) || speedLevel < 1 || speedLevel > 9) {
    respondErr("PARAM");
    return;
  }

  applySpeedToAllAxes(static_cast<uint8_t>(speedLevel));
  Serial.print("OK SPEED ");
  Serial.println(currentSpeed);
}

void handleJogCommand(char *axisDirToken, char *stepsToken, char *extra) {
  if (axisDirToken == NULL || extra != NULL) {
    respondErr("PARAM");
    return;
  }

  if (currentState == STATE_MOVING || currentState == STATE_HOMING) {
    respondErr("BUSY");
    return;
  }

  if (currentState != STATE_IDLE && currentState != STATE_READY) {
    respondErr("STATE");
    return;
  }

  int axisIndex = -1;
  bool directionPositive = true;
  if (!parseAxisDirToken(axisDirToken, axisIndex, directionPositive)) {
    respondErr("PARAM");
    return;
  }

  long steps = DEFAULT_JOG_STEPS;
  if (stepsToken != NULL && !parsePositiveLongToken(stepsToken, steps)) {
    respondErr("PARAM");
    return;
  }

  if (limitTriggeredForAxis(axes[axisIndex], directionPositive)) {
    respondErr("LIMIT");
    triggerLimitFault(axisIndex, directionPositive);
    return;
  }

  startJog(static_cast<uint8_t>(axisIndex), directionPositive, steps);
  respondOk("MOVING");
  setState(STATE_MOVING);
}

void handleHomeCommand(char *axisToken, char *extra) {
  if (extra != NULL) {
    respondErr("PARAM");
    return;
  }

  if (currentState == STATE_MOVING || currentState == STATE_HOMING) {
    respondErr("BUSY");
    return;
  }

  if (currentState != STATE_IDLE && currentState != STATE_READY) {
    respondErr("STATE");
    return;
  }

  if (axisToken == NULL) {
    homeSingleAxis = false;
    beginHomePhase(HOME_Z_FAST_FIND);
  } else {
    if (axisToken[1] != '\0') {
      respondErr("PARAM");
      return;
    }
    int ai = axisIndexFromChar(axisToken[0]);
    if (ai < 0) {
      respondErr("PARAM");
      return;
    }
    homeSingleAxis = true;
    homeSingleAxisIndex = static_cast<uint8_t>(ai);

    uint8_t startPhase;
    switch (ai) {
      case 0: startPhase = HOME_X_FAST_FIND; break;
      case 1: startPhase = HOME_Y_FAST_FIND; break;
      default: startPhase = HOME_Z_FAST_FIND; break;
    }
    beginHomePhase(startPhase);
  }

  respondOk("HOMING");
  setState(STATE_HOMING);
}

void handleMoveCommand(char *arg1, char *arg2, char *arg3) {
  if (currentState != STATE_READY) {
    respondErr("STATE");
    return;
  }

  if (arg1 == NULL) {
    respondErr("PARAM");
    return;
  }

  long targets[AXIS_COUNT];
  bool hasTarget[AXIS_COUNT] = {false, false, false};

  char *args[] = {arg1, arg2, arg3};
  for (uint8_t i = 0; i < 3; i++) {
    if (args[i] == NULL) break;

    int ai = axisIndexFromChar(args[i][0]);
    if (ai < 0) {
      respondErr("PARAM");
      return;
    }

    if (hasTarget[ai]) {
      respondErr("PARAM");
      return;
    }

    char *numStr = args[i] + 1;
    if (*numStr == '\0') {
      respondErr("PARAM");
      return;
    }

    char *end = NULL;
    long val = strtol(numStr, &end, 10);
    if (*end != '\0') {
      respondErr("PARAM");
      return;
    }

    hasTarget[ai] = true;
    targets[ai] = val;
  }

  bool anyMoving = false;
  for (uint8_t i = 0; i < AXIS_COUNT; i++) {
    if (!hasTarget[i]) continue;

    long diff = targets[i] - axes[i].position;
    if (diff == 0) continue;

    bool dirPos = (diff > 0);
    long steps = diff > 0 ? diff : -diff;

    if (limitTriggeredForAxis(axes[i], dirPos)) {
      respondErr("LIMIT");
      return;
    }

    startJog(i, dirPos, steps);
    anyMoving = true;
  }

  if (!anyMoving) {
    respondOk("MOVING");
    Serial.println("@MOVE_OK");
    return;
  }

  moveInProgress = true;
  respondOk("MOVING");
  setState(STATE_MOVING);
}

void handleStopCommand(char *axisToken, char *extra) {
  if (extra != NULL) {
    respondErr("PARAM");
    return;
  }

  if (currentState == STATE_ERROR || currentState == STATE_ESTOP) {
    respondErr("STATE");
    return;
  }

  if (axisToken == NULL) {
    stopAllAxes();
    respondOk();
  } else {
    if (axisToken[1] != '\0') {
      respondErr("PARAM");
      return;
    }

    int axisIndex = axisIndexFromChar(axisToken[0]);
    if (axisIndex < 0) {
      respondErr("PARAM");
      return;
    }

    stopAxis(axes[axisIndex]);
    respondOk();
  }

  if (currentState == STATE_HOMING) {
    homePhase = HOME_IDLE;
    homed = false;
    Serial.println("@HOME_FAIL STOPPED");
    rememberFault("HOME_STOPPED");
    setState(STATE_ERROR);
    return;
  }

  if (!anyAxisActive() && currentState == STATE_MOVING) {
    if (moveInProgress) {
      moveInProgress = false;
    }
    setState(stateAfterMotion());
  }
}

void handleEstopCommand(char *extra) {
  if (extra != NULL) {
    respondErr("PARAM");
    return;
  }

  stopAllAxes();
  homePhase = HOME_IDLE;
  moveInProgress = false;
  homed = false;
  respondOk();
  setState(STATE_ESTOP);
}

void handleResetCommand(char *extra) {
  if (extra != NULL) {
    respondErr("PARAM");
    return;
  }

  if (currentState != STATE_ERROR && currentState != STATE_ESTOP) {
    respondErr("STATE");
    return;
  }

  stopAllAxes();
  homePhase = HOME_IDLE;
  moveInProgress = false;
  homed = false;
  clearFault();
  respondOk();
  setState(STATE_IDLE);
}

void parseCommand(char *line) {
  normalizeCommand(line);
  if (line[0] == '\0') {
    return;
  }

  char *savePtr = NULL;
  char *cmd = strtok_r(line, " \t", &savePtr);
  char *arg1 = strtok_r(NULL, " \t", &savePtr);
  char *arg2 = strtok_r(NULL, " \t", &savePtr);
  char *arg3 = strtok_r(NULL, " \t", &savePtr);

  if (strcmp(cmd, "STATE") == 0) {
    handleStateQuery(arg1);
    return;
  }

  if (strcmp(cmd, "POS") == 0) {
    handlePosQuery(arg1);
    return;
  }

  if (strcmp(cmd, "SENS") == 0) {
    handleSensQuery(arg1);
    return;
  }

  if (strcmp(cmd, "SPEED") == 0) {
    handleSpeedCommand(arg1, arg2);
    return;
  }

  if (strcmp(cmd, "JOG") == 0) {
    handleJogCommand(arg1, arg2, arg3);
    return;
  }

  if (strcmp(cmd, "STOP") == 0) {
    handleStopCommand(arg1, arg2);
    return;
  }

  if (strcmp(cmd, "ESTOP") == 0) {
    handleEstopCommand(arg1);
    return;
  }

  if (strcmp(cmd, "RESET") == 0) {
    handleResetCommand(arg1);
    return;
  }

  if (strcmp(cmd, "HOME") == 0) {
    handleHomeCommand(arg1, arg2);
    return;
  }

  if (strcmp(cmd, "MOVE") == 0) {
    handleMoveCommand(arg1, arg2, arg3);
    return;
  }

  respondErr("CMD");
}

void readSerialCommands() {
  while (Serial.available() > 0) {
    char c = static_cast<char>(Serial.read());

    if (discardUntilNewline) {
      if (c == '\n') {
        discardUntilNewline = false;
      }
      continue;
    }

    if (c == '\r') {
      continue;
    }

    if (c == '\n') {
      if (cmdLen == 0) {
        continue;
      }
      cmdBuffer[cmdLen] = '\0';
      parseCommand(cmdBuffer);
      cmdLen = 0;
      continue;
    }

    if (cmdLen < CMD_BUFFER_SIZE - 1) {
      cmdBuffer[cmdLen++] = c;
    } else {
      cmdLen = 0;
      discardUntilNewline = true;
      respondErr("PARAM");
    }
  }
}

void processMotion() {
  if (currentState != STATE_MOVING) {
    return;
  }

  for (uint8_t i = 0; i < AXIS_COUNT; i++) {
    Axis &axis = axes[i];
    if (!axis.active) {
      continue;
    }

    if (limitTriggeredForAxis(axis, axis.directionPositive)) {
      triggerLimitFault(i, axis.directionPositive);
      return;
    }

    long batchSteps = axis.remainingSteps;
    if (batchSteps > STEP_BATCH_SIZE) {
      batchSteps = STEP_BATCH_SIZE;
    }

    doStepBatch(axis, batchSteps);
    axis.remainingSteps -= batchSteps;
    if (axis.remainingSteps <= 0) {
      stopAxis(axis);
    }
  }

  if (!anyAxisActive()) {
    if (moveInProgress) {
      moveInProgress = false;
      Serial.println("@MOVE_OK");
    }
    setState(stateAfterMotion());
  }
}

void processHoming() {
  if (currentState != STATE_HOMING || homePhase == HOME_IDLE) {
    return;
  }

  uint8_t ai = homeAxisIndex(homePhase);
  Axis &axis = axes[ai];
  uint8_t stage = homeStageType(homePhase);

  if (millis() - homePhaseStartMs > homeTimeout(homePhase)) {
    homePhase = HOME_IDLE;
    homed = false;
    char msg[16];
    snprintf(msg, sizeof(msg), "TIMEOUT_%c", axis.name);
    Serial.print("@HOME_FAIL ");
    Serial.println(msg);
    rememberFault(msg);
    setState(STATE_ERROR);
    return;
  }

  for (uint8_t i = 0; i < AXIS_COUNT; i++) {
    if (alarmTriggered(ALM_PINS[i])) {
      homePhase = HOME_IDLE;
      homed = false;
      Serial.println("@HOME_FAIL ALM");
      rememberFault("HOME_ALM");
      setState(STATE_ERROR);
      return;
    }
  }

  bool homeDir = axis.homeDirectionPositive;
  uint8_t homeLimitPin = homeDir ? axis.posLimitPin : axis.negLimitPin;
  uint8_t retractLimitPin = homeDir ? axis.negLimitPin : axis.posLimitPin;
  bool sensorMet = false;

  switch (stage) {
    case 1:
      if (sensorTriggered(axis.homePin)) {
        sensorMet = true;
      } else if (sensorTriggered(homeLimitPin)) {
        // Hit limit before finding home — origin is the other way.
        // Flip home direction for the rest of this homing sequence.
        axis.homeDirectionPositive = !axis.homeDirectionPositive;
        axis.directionPositive = !homeDir;
        for (long i = 0; i < HOME_BOUNCE_STEPS; i++) {
          doStepBatch(axis, 1);
          if (!sensorTriggered(homeLimitPin)) {
            break;
          }
        }
        // Keep going in the reversed direction (don't reset to homeDir)
        return;
      }
      break;
    case 2:
      if (!sensorTriggered(axis.homePin)) {
        sensorMet = true;
      }
      break;
    case 3:
      if (sensorTriggered(axis.homePin)) {
        sensorMet = true;
      } else if (sensorTriggered(homeLimitPin)) {
        axis.directionPositive = !homeDir;
        for (long i = 0; i < HOME_BOUNCE_STEPS; i++) {
          doStepBatch(axis, 1);
          if (!sensorTriggered(homeLimitPin)) {
            break;
          }
        }
        axis.directionPositive = homeDir;
        return;
      }
      break;
  }

  if (sensorMet) {
    advanceHomePhase();
    return;
  }

  if (stage == 2 && sensorTriggered(retractLimitPin)) {
    homePhase = HOME_IDLE;
    homed = false;
    char msg[16];
    snprintf(msg, sizeof(msg), "LIMIT_%c", axis.name);
    Serial.print("@HOME_FAIL ");
    Serial.println(msg);
    rememberFault(msg);
    setState(STATE_ERROR);
    return;
  }

  doStepBatch(axis, STEP_BATCH_SIZE);
}

void emitPeriodicReports() {
  unsigned long now = millis();
  unsigned long interval = anyAxisActive() ? MOVING_REPORT_INTERVAL_MS : IDLE_REPORT_INTERVAL_MS;
  if (now - lastReportMs < interval) {
    return;
  }

  lastReportMs = now;
  emitPos();
  if (!anyAxisActive()) {
    emitSens();
    emitAlm();
  }
}

void setup() {
  for (uint8_t i = 0; i < AXIS_COUNT; i++) {
    pinMode(axes[i].plsPin, OUTPUT);
    pinMode(axes[i].dirPin, OUTPUT);
    digitalWrite(axes[i].plsPin, LOW);
    digitalWrite(axes[i].dirPin, LOW);
  }

  for (uint8_t i = 0; i < SENSOR_COUNT; i++) {
    pinMode(SENSOR_PINS[i], INPUT_PULLUP);
  }

  for (uint8_t i = 0; i < AXIS_COUNT; i++) {
    pinMode(ALM_PINS[i], INPUT_PULLUP);
  }

  Serial.begin(115200);
  delay(50);

  Serial.println("OK READY");
  emitState();
  emitPos();
  emitSens();
  emitAlm();
}

void loop() {
  readSerialCommands();
  processHoming();
  processMotion();
  emitPeriodicReports();
}
