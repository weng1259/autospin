// alm_test.ino — Phase 0 ALM 报警接线验证 sketch
//
// 用途：验证 2HSS57-C 驱动器的 ALM 报警输出是否正确接到
// Arduino Mega 2560 的 D31 (X)、D32 (Y)、D33 (Z)。
//
// 用法：
//   1. 关闭所有占用串口的程序（cncjs、网页面板、Python 脚本）
//   2. Arduino IDE 上传本 sketch 到 Mega 2560
//   3. 打开串口监视器，波特率 115200
//   4. 正常状态每 500ms 输出：ALM  X=OK  Y=OK  Z=OK
//   5. 触发报警时对应轴变 ALARM
//
// 详见 docs/guides/02-电控接线.md §27 和
// docs/guides/phase0-硬件安全底座.md

const uint8_t ALM_X = 31;
const uint8_t ALM_Y = 32;
const uint8_t ALM_Z = 33;

void setup() {
  pinMode(ALM_X, INPUT_PULLUP);
  pinMode(ALM_Y, INPUT_PULLUP);
  pinMode(ALM_Z, INPUT_PULLUP);
  Serial.begin(115200);
  while (!Serial) { /* wait for USB CDC */ }
  Serial.println();
  Serial.println("=== ALM Test (Phase 0) ===");
  Serial.println("LOW  = alarm triggered");
  Serial.println("HIGH = ok (driver idle)");
  Serial.println();
}

void loop() {
  int x = digitalRead(ALM_X);
  int y = digitalRead(ALM_Y);
  int z = digitalRead(ALM_Z);

  Serial.print("ALM  X=");
  Serial.print(x == LOW ? "ALARM" : "OK   ");
  Serial.print("  Y=");
  Serial.print(y == LOW ? "ALARM" : "OK   ");
  Serial.print("  Z=");
  Serial.println(z == LOW ? "ALARM" : "OK   ");

  delay(500);
}
