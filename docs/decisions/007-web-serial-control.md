# 决策记录 — 控制界面选网页（Web Serial API）

**日期**：2026-03-17
**状态**：已采纳
**决策人**：FENG

## 背景

电机首次通电成功，需要一个可交互的界面来手动控制XYZ三轴移动。

## 决策

制作基于浏览器Web Serial API的网页控制面板（`web_control/index.html`，localhost:8080）。

## 考虑的替代方案

- 桌面串口调试软件（CoolTerm等）
- Python脚本交互界面
- Electron桌面应用

## 理由

Web Serial API允许浏览器直接访问串口，无需额外安装；一个HTML文件开发快速；跨平台（Mac/手机）；与未来树莓派+API的架构方向一致。

## 影响

需注意Web Serial API会占用串口，上传固件前必须先断开连接，否则需重启电脑。
