# 决策记录 — 开发工具选arduino-cli命令行

**日期**：2026-03-16
**状态**：已采纳
**决策人**：FENG

## 背景

需要选择Arduino固件的开发和上传工具。用户希望AI助手能直接在终端中帮助开发。

## 决策

使用arduino-cli在终端中编译和上传固件，不使用Arduino IDE图形界面。

## 考虑的替代方案

- Arduino IDE（官方图形界面）
- PlatformIO（VSCode插件，功能更强）

## 理由

arduino-cli是Arduino官方命令行工具，可被终端直接调用（`arduino-cli compile` / `arduino-cli upload`），无需打开GUI；PlatformIO功能过重；`brew install arduino-cli`即装即用。

## 影响

所有固件编译上传均通过命令行完成，开发效率高。
