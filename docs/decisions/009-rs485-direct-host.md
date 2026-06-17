# 决策记录 — RS485模块由上位机直连，不经过Arduino

**日期**：2026-03-19
**状态**：已采纳
**决策人**：FENG

## 背景

原架构方案是Arduino通过Serial1 + MAX485模块控制旋涂模块。用户提出是否可以直接用上位机控制。

## 决策

旋涂模块和移液模块的RS485通信直接由上位机（Mac/树莓派）通过USB转RS485适配器控制，不经过Arduino。

## 考虑的替代方案

- 方案A（原始）：全部走Arduino（Serial1 + MAX485 → 旋涂模块）
- 方案C：树莓派GPIO UART + MAX485硬件模块

## 理由

1. 职责分离：Arduino管电机脉冲（微秒级），上位机管RS485命令（毫秒级）
2. 独立调试：可单独测试RS485设备
3. 避免干扰：电机脉冲与RS485通信不会冲突
4. 简化固件：Arduino代码不用改
5. Python pymodbus/minimalmodbus库比Arduino处理Modbus协议能力强

## 影响

需购买USB转RS485适配器（CH340芯片，约15元），代码从Mac迁移到树莓派无需修改。
