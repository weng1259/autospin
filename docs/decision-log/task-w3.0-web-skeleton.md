# 任务卡 W3.0：Web 控制服务骨架（FastAPI + 鉴权 + mock 组合根）

**发卡**：2026-07-17 · **执行者**：Codex（无会话记忆，本卡自包含）· **前置**：W2 已合入 main
**W3 系列总原则**：卡故意切小——一张卡一个会话做完就收，不做卡外的"顺手优化"。

## 背景（一段话）

为七个已完成的 L3 backend（gantry/relay/gripper/heater/spincoater/pipette/linearstage，
见 `docs/api-v1.json`）建 Web 控制服务。技术选型已定：FastAPI + uvicorn，静态前端后续卡做。
本卡只做**服务骨架**：应用工厂、设备注册表（真/mock 双模式）、token 鉴权、健康检查。

## 硬边界

1. **禁止连接真实硬件**；mock 模式用假 backend 对象，不开任何串口。
2. 只许新增 `src/webapp/`（`__init__.py`/`app.py`/`registry.py`/`auth.py`）、
   `tests/test_webapp_skeleton.py`、`tools/run_webserver.py`；只许修改 `requirements.txt`
   （追加 pin 版本的 fastapi/uvicorn/httpx，httpx 是 TestClient 依赖）。**禁改** `src/hardware/`。
3. 开始/结束跑 `scripts/check.sh` 全绿（mypy --strict 覆盖 src/webapp）；全部 commit。
4. 依赖装进 `tools/spikes/.venv`（Mac 本机 venv）；Pi 侧安装由搬运人负责，卡内不管。

## 设计（定稿）

- `create_app(registry: DeviceRegistry, *, token: str) -> FastAPI`：应用工厂，测试可注入假注册表。
- `DeviceRegistry`：持有 Optional 的七个 backend + `SystemEstop`（`src/system_estop.py`）。
  两个构造器：`DeviceRegistry.from_mocks()`（骨架阶段全 None 或假对象）与
  `DeviceRegistry.from_config()`（真硬件，**本卡只写签名和 NotImplementedError 占位**，
  接线在后续卡）。
- **token 鉴权**：除 `GET /api/health` 外所有路由要求 `Authorization: Bearer <token>`；
  token 由启动脚本生成（`secrets.token_urlsafe`）并打印到 stdout。错误 401 JSON
  `{"error": {...}}` 结构与 L3Error 字段对齐（error_code/human_message/agent_message）。
- `GET /api/health`：`{"ok": true, "version": <SCHEMA_VERSION>, "mock": bool}`。
- `tools/run_webserver.py`：argparse（`--host` 默认 **127.0.0.1**、`--port 8800`、
  `--mock` 旗标）；非 127.0.0.1 的 host 必须打印醒目警告。uvicorn 编程式启动。
- L3Error → HTTP 的统一异常 handler：L3Error 422/409/503（按 severity 映射，先做保守版
  全 422），其它异常 500；响应体永远是结构化 JSON，不裸 traceback。

## 测试要求（FastAPI TestClient，全 mock）

1. `/api/health` 无 token 可达，返回 mock 标志。
2. 其它任意路径无 token → 401 结构化 JSON；带错 token → 401；带对 token → 不是 401。
3. L3Error handler：注册一个抛 L3Error 的测试路由，断言 JSON 字段齐全。
4. `create_app` 可用假 registry 构造，互不共享全局状态（两次调用两个独立 app）。

## 验收

- [ ] `scripts/check.sh` 全绿（新增测试并入；mypy --strict 含 src/webapp 零错误）
- [ ] `tools/run_webserver.py --mock` 能本机起服务、curl health 通（人工验一次，写进汇报）
- [ ] requirements.txt 版本 pin 死（== 不用 >=）
