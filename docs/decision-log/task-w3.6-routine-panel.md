# 任务卡 W3.6：示教-重放（routine）端点 + 面板

**发卡**：2026-07-17 · **执行者**：Codex · **前置**：W3.5 已合入 main
**设计规范**：遵守 `task-w3.4-frontend-skeleton.md` 的"设计规范"段。

## 背景（一段话）

`src/routine.py` 已有完整示教-重放实现（RecordingProxy 录高层动作 / RoutinePlayer 顺序
回放 / JSON 存盘 / 含运动的程序要求已归零）。本卡把它暴露成端点 + 面板：PM 手动跑一遍
流程 → 录成程序 → 一键重放。这是 W4 编排的 v1，也是演示视频的生产工具。

## 硬边界

1. 禁真硬件；测试与自测全 mock。
2. 只许新增 `src/webapp/routes_routines.py`、`tests/test_webapp_routines.py`；只许修改
   `src/webapp/app.py`（挂路由）、`src/webapp/registry.py`（接 RecordingProxy 包装）、
   `src/webapp/static/`（面板）。**禁改** `src/routine.py` 与 `src/hardware/`——routine
   模块行为不合适时停下写汇报，不就地改。
3. `scripts/check.sh` 全绿；全部 commit。

## 端点（录制控制不过门闸——它只翻内存旗标；重放过门闸——它真动硬件）

- `POST /api/routines/record/arm`（body: name）/ `disarm` / `GET /api/routines/record`
  （当前录制状态 + 已录步数 + 最近 5 步摘要）。
- registry 侧：构造真设备时用 `RecordingProxy` 包住各 backend（录制关闭时零开销直通），
  录制 armed 后经面板做的成功操作自动成步。
- `GET /api/routines`（列表：名称/步数/时长/含运动否）、`GET /api/routines/{name}`
  （步骤全文）、`DELETE /api/routines/{name}`。
- `POST /api/routines/{name}/replay`：过 operation 门闸（device="routine"），后台线程
  RoutinePlayer 回放；进度回调写进 gate 的 operation 记录（当前第 N/共 M 步 + 步标签），
  SSE 快照带出去；`POST /api/routines/replay/abort` 调 player 的中止（**直通不排队**，
  文件顶注释说明与 estop 同级）。
- 存盘目录 `runtime/routines/`（.gitignore 确认排除）；**写盘必须原子**（tmp +
  `os.replace`——审计 P2 示教点丢失教训；routine.py 的 save 若已原子则直接用，汇报注明）。

## 面板（加一个"程序"卡片）

- 录制区：名称输入 + 开始/停止录制按钮 + 录制中红点 + 实时步数；停止后自动存盘并刷新列表。
- 列表区：紧凑表格（名称/步数/时长/含运动标记/重放按钮/删除按钮），删除要二次确认
  （行内变红"确认删除？"，不弹窗）。
- 重放区：进度 `N/M` + 当前步标签（等宽），中止按钮（红边、永不禁用）。
- 重放前置校验的错误（未归零等）显示 human_message + suggested_action_zh。

## 测试要求

1. 录制流：arm → 假 backend 经 proxy 做 2 个成功动作 + 1 个失败动作 → disarm 存盘 →
   文件存在且恰 2 步。
2. 原子写：monkeypatch `os.replace` 抛错 → 原文件不损坏（或 routine.py 已有等价保证的
   断言）。
3. 重放：mock 下回放 3 步程序，operation 记录进度推进到 3/3；中途 abort → player 停、
   gate 释放。
4. 重放占用门闸：回放中 gantry move → 409；abort 端点直通。
5. 含运动程序 + 未归零假 gantry → 重放拒绝（routine.py 安全闸透传成结构化错误）。

## 验收

- [ ] `scripts/check.sh` 全绿
- [ ] `--mock` 人工走一遍录→列→放→中止（汇报勾选）
- [ ] W3 全系列此卡收尾：汇报附一段"面板功能全景清单"（对照施工计划 W3 功能范围 v1 逐项勾）
