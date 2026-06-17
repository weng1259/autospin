# Issue #004 — architecture.md 拨码开关记录未更新为闭环配置

**日期**：2026-03-24
**提出人**：Claude（代码审查）
**状态**：open
**优先级**：低
**类型**：文档
**涉及文件**：`docs/architecture.md`

## 问题描述

`docs/architecture.md` 第 103 行记录的拨码开关配置仍为开环模式：

```
SW1=OFF  SW2=OFF  SW3=OFF  SW4=OFF  SW5=ON  SW6=ON  SW7=OFF  SW8=ON（开环）
```

实际上已切换到闭环模式，应更新为：

```
SW1=OFF  SW2=OFF  SW3=OFF  SW4=OFF  SW5=ON  SW6=ON  SW7=ON  SW8=OFF（闭环）
```

变化点：
- **SW7**: OFF → ON（开启指令平滑）
- **SW8**: ON → OFF（闭环模式）

## 影响

- 组员参考文档接线/调试时会用错误的拨码配置
- 新加入的组员可能把驱动器拨回开环模式

## 建议方案

更新 `docs/architecture.md` 第 103 行的拨码记录，并在下方加注说明变更原因。

同时建议在 `docs/decisions/` 中补一条闭环切换的决策记录（如 `010-closed-loop-mode.md`），记录为什么从开环切到闭环、什么时候切的、编码器接线方式等。

## 验收标准

- [ ] architecture.md 拨码配置与实际一致
- [ ] 有决策记录说明开环→闭环的切换
