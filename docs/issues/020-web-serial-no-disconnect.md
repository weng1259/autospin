# Issue #020 — 网页面板缺少断开按钮和串口释放

**日期**：2026-03-24
**提出人**：Codex（审查 #023）
**状态**：obsolete（2026-04-23 —— `web_control/` 系列网页面板已废弃，L5 改 Streamlit，连接/断开由 backend 管理；见 ADR-002/ADR-003）
**优先级**：中
**类型**：工程
**涉及文件**：`web_control/index.html`、`web_control/homing_test.html`、`web_control/sensor_test.html`

## 问题描述

三个主页面只有"连接"没有"断开"按钮，没有 `reader.cancel()`、`writer.releaseLock()`、`port.close()` 清理流程，也没有 `beforeunload` 事件处理。已知问题：Web Serial 占口后需重启电脑才能上传固件。

## 建议方案

1. 每页补"断开"按钮，执行完整清理（cancel reader → release writer → close port）
2. 添加 `beforeunload` 事件自动清理
3. 此问题可在 #009（公共模块抽取）中一并解决

## 验收标准

- [ ] 每页有断开按钮
- [ ] 关页/刷新时自动释放串口
- [ ] 断开后可立即用 Arduino IDE 上传固件
