// Y轴电机最简测试
// 让电机慢慢转200步，停2秒，反转200步，停2秒，循环

#define Y_PLS 2   // Y轴脉冲引脚
#define Y_DIR 3   // Y轴方向引脚

void setup() {
  pinMode(Y_PLS, OUTPUT);
  pinMode(Y_DIR, OUTPUT);
  Serial.begin(115200);
  Serial.println("Y轴电机测试开始！");
}

void loop() {
  // 正转200步
  Serial.println("正转...");
  digitalWrite(Y_DIR, HIGH);
  for (int i = 0; i < 200; i++) {
    digitalWrite(Y_PLS, HIGH);
    delayMicroseconds(500);
    digitalWrite(Y_PLS, LOW);
    delayMicroseconds(500);
  }

  delay(2000);

  // 反转200步
  Serial.println("反转...");
  digitalWrite(Y_DIR, LOW);
  for (int i = 0; i < 200; i++) {
    digitalWrite(Y_PLS, HIGH);
    delayMicroseconds(500);
    digitalWrite(Y_PLS, LOW);
    delayMicroseconds(500);
  }

  delay(2000);
}
