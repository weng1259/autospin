# 决策记录 — Arduino信号线采用共阳极接法

**日期**：2026-03-17
**状态**：已采纳
**决策人**：FENG

## 背景

驱动器PLS/DIR端口有PLS+/PLS-/DIR+/DIR-四个端子，支持共阳极或共阴极两种接法。

## 决策

PLS+/DIR+接Arduino 5V（共用正极），PLS-/DIR-接Arduino GPIO信号引脚。

## 考虑的替代方案

- 共阴极接法（PLS-/DIR-接GND，PLS+/DIR+接GPIO）
- 差分信号接法（需额外硬件）

## 理由

共阳极接法在Arduino 5V逻辑下最简单直接，驱动器手册典型接线图即采用此接法，Arduino GPIO拉低即发脉冲，符合NPN输入特性，接线最少。

## 影响

Arduino 5V引脚需通过驱动器端子间跳接来串联供电（解决5V引脚数量不足的问题）。
