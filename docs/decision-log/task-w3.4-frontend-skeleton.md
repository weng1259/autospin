# 任务卡 W3.4：前端骨架 + 设计规范（本卡的设计规范段对 W3.5/3.6 同样有效）

**发卡**：2026-07-17 · **执行者**：Codex · **前置**：W3.3 已合入 main

## 背景（一段话）

单页静态前端，无构建链（no npm/no bundler），原生 HTML/CSS/JS（ES modules）。本卡只做
骨架：设计系统、页面布局、全局状态栏、急停按钮、token 输入、SSE 接线。设备面板下一卡。

## 硬边界

1. **禁止连接真实硬件**；自测用 `tools/run_webserver.py --mock` 本机起服务（允许，纯软件）。
2. 只许新增 `src/webapp/static/`（`index.html`/`style.css`/`app.js`）、
   `tests/test_webapp_static.py`（静态文件可服务 + 关键元素存在的冒烟测试）；只许修改
   `src/webapp/app.py`（挂静态目录，`/` 重定向 index）。
3. `scripts/check.sh` 全绿；全部 commit。

## 设计规范（PM 拍板，**逐条硬性**，W3.5/3.6 引用本段）

反面清单（出现即返工）：
- ❌ 蓝紫渐变、任何 `linear-gradient` 背景、玻璃拟态（backdrop-filter blur 卡片）
- ❌ 大圆角：任何元素 `border-radius > 6px`（按钮 4px、面板 6px 封顶；禁 pill 按钮）
- ❌ 阴影堆叠：禁 box-shadow（分隔一律 1px 实线边框）
- ❌ 满屏 emoji 图标、营销式大标题/hero 区、"欢迎使用"类空话文案
- ❌ 每个控件占一整行的松散布局

正面规范（苹果系简洁 + 高信息密度）：
- **配色**：白底 `#FFFFFF`，面板底 `#F5F5F7`，边框 `#D2D2D7`，正文 `#1D1D1F`，
  次要文字 `#6E6E73`。功能色只允许三个且只用于语义：主操作蓝 `#0066CC`、
  危险/急停红 `#D70015`、正常/在线绿 `#28A745`。无其它彩色。
- **字体**：`-apple-system, "SF Pro Text", "Helvetica Neue", "PingFang SC", sans-serif`，
  正文 13px/1.4；**所有数值读数**（坐标/温度/转速/位置/seq）一律等宽
  `ui-monospace, "SF Mono", Menlo, monospace`，右对齐，带单位。
- **密度**：控件内边距 ≤ 8px；面板间距 12px；一屏（1440×900）不滚动能看到全局状态 +
  至少四个设备面板。信息优先于留白。
- **布局**：顶部 40px 全局状态栏（sticky）：左=服务/连接状态点+文字，中=当前 operation
  （设备·动作·已运行秒数，无则灰字"空闲"），右=**急停按钮**。主体 CSS Grid 多列
  （≥1200px 三列，窄屏两列/一列）。
- **急停按钮**：红底白字、状态栏常驻、**任何情况下不 disabled**（审计 A6 教训），走独立
  fetch（不复用通用请求封装的 busy 逻辑），点击后按钮文字切换为"已发送…"1 秒后恢复，
  显示 EstopReport 每步结果。
- 每个可交互控件旁边直接标注它做什么和单位（如 "进给 mm/min"），不用悬浮 tooltip 藏信息。

## 功能范围（仅骨架）

- token 输入框（存 localStorage），401 时状态栏红字提示。
- SSE 订阅 `/api/status/stream`，断线自动重连（指数退避 1s→8s 封顶），连接状态反映在状态栏。
- 全局状态栏 + 急停按钮 + 底部事件日志区（等宽字体，倒序 100 条：operation 起止、estop
  结果、SSE 断连）。设备面板区先渲染占位框（设备名 + "面板于 W3.5 接入"）。
- `app.js` 里建好薄封装：`api(path, body)`（带 token/错误解析）与 `apiEstop()`（独立、
  无 busy、无排队）。

## 测试要求

1. `/` 与静态文件带 token 可达（TestClient）；index.html 含急停按钮元素与状态栏骨架。
2. CSS 静态断言（读文件字符串）：不含 `linear-gradient`、不含 `box-shadow`、
   所有 `border-radius` 值 ≤ 6px（简单正则即可）。——设计规范进 CI，防回潮。

## 验收

- [ ] `scripts/check.sh` 全绿
- [ ] `--mock` 起服务人工截图（浏览器打开）随汇报描述：状态栏/急停/日志区可见
