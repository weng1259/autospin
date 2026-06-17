# Issue #010 — 夹爪面板写流模型冲突导致原始帧发送不稳定

**日期**：2026-03-24
**提出人**：Codex（代码审查）
**状态**：open
**优先级**：高
**类型**：bug
**涉及文件**：`web_control/gripper.html`

## 问题描述

连接流程里使用 `TextEncoderStream().readable.pipeTo(port.writable)`，同时 `sendRaw()` 又直接 `port.writable.getWriter()` 写二进制。两个写通道并存会产生写锁冲突，导致 HEX/Modbus 发送路径不可靠。

协议探测和回环测试都依赖 `sendRaw()`，因此影响范围较大。

## 影响

- 发送命令可能随机失败或无效
- 调试结果不稳定，难以区分“设备问题”还是“面板实现问题”

## 建议方案

- 统一为单一写模型：全部走 `sendRaw(Uint8Array)`
- 文本发送通过 `TextEncoder` 转字节后同样走 `sendRaw`
- 移除并行 `pipeTo` 写入链路，避免 writer 锁竞争

## 讨论记录

- 2026-03-24：静态审查发现写通道混用，锁语义存在冲突风险。

## 验收标准

- [ ] HEX、文本、探测按钮均通过同一发送通道
- [ ] 连续发送 100 次无 `writer locked` 类错误
- [ ] 回环测试结果稳定可复现

