# Issue #010 — 夹爪面板串口通信架构修复

**日期**：2026-03-24（合并更新）
**提出人**：Claude + Codex
**状态**：open
**优先级**：高
**类型**：bug
**涉及文件**：`web_control/gripper.html`
**合并自**：原 #010（写流冲突）+ 原 #012（二进制数据被文本污染）+ 原 #013（波特率扫描后端口状态错误）

## 问题描述

夹爪面板的串口通信有三个互相关联的 bug，根因都是**二进制 Modbus 协议和文本流模型混用**：

### 问题 A：双写通道冲突（原 #010）

连接时用 `TextEncoderStream().readable.pipeTo(port.writable)` 建立管道，`sendRaw()` 又直接 `port.writable.getWriter()` 写入。两个写通道互相锁死，导致 HEX 帧发送不稳定。

### 问题 B：二进制数据被文本编解码污染（原 #012）

读取用 `TextDecoderStream` 把字节转文本，再用 `TextEncoder` 转回字节存 buffer。非文本字节（Modbus 响应帧中常见 ≥0x80 的值）被 UTF-8 解码替换/损坏，导致 CRC 校验失败、协议识别误判。

### 问题 C：波特率扫描后端口状态泄漏（原 #013）

扫描循环中每轮 close→open 切波特率，但未正确释放 reader/writer/pipe 链。扫描结束后 reopen 时端口状态残留，连接失败或收发异常。

## 建议方案

统一改为纯二进制收发模型：

```javascript
// 发送：统一用 getWriter()
async function sendRaw(bytes) {
    const writer = port.writable.getWriter();
    await writer.write(new Uint8Array(bytes));
    writer.releaseLock();
}

// 接收：直接读 Uint8Array，不经过 TextDecoder
async function readLoop() {
    const reader = port.readable.getReader();
    while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        // value 是 Uint8Array，原始字节不被污染
        processRawBytes(value);
    }
}

// 波特率切换：封装完整清理流程
async function reopenPort(baudRate) {
    if (reader) { await reader.cancel(); reader.releaseLock(); }
    if (writer) { writer.releaseLock(); }
    await port.close();
    await port.open({ baudRate });
    // 重建 reader/writer
}
```

不使用 `pipeTo()`、`TextEncoderStream`、`TextDecoderStream`。日志面板显示时才把 bytes 转 HEX 字符串。

## 验收标准

- [ ] 发送 Modbus 帧后正确收到响应（CRC 校验通过）
- [ ] 收发全程保持原始字节，无文本编解码
- [ ] 波特率扫描后能正常恢复连接
- [ ] 不再出现写锁冲突
