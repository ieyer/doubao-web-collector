# 豆包网页采集器（B39 首期）

独立服务：在**豆包网页端普通联网搜索**模式下，一题一任务地执行提问，并交回本题的完整答案与原始证据（正文、搜索资料、正文引用、分段截图、会话地址、明确状态）。品牌识别、提及率、SOV、分析与报告留在 NegencyGEO，本服务不做统计与计价。

- 不是方舟模型 API、不是豆包 App、不是深度思考模式。
- platform 固定 `doubao`，channel 固定 `browser`，mode 固定 `web_search`；其他值明确拒绝（400），不静默改模式。
- 遇到平台真人验证/封禁/限流：暂停受影响槽位并返回真实原因，**不绕过、不换账号/IP 规避**。

## 架构

单进程 FastAPI 服务 + 每槽位一个工作线程 + SQLite 持久化（任务/attempt/幂等键/资产元数据）+ Playwright 驱动本地 Chromium（有头模式，运行在 Xvfb 虚拟显示上）。

- 最多 5 个执行槽位；每个槽位一个独立浏览器资料目录（一个账号），同一账号同时只跑一道题。
- 5 槽共享有界队列（默认上限 100）；无可用槽位时任务排队等待，状态真实返回。
- 结果与 attempt 持久化在 `data/collector.db`（WAL），截图在 `data/assets/<task_id>/`，默认保留 30 天（配置项）。

## 接口（v1）

所有 `/v1/*`（除 `/v1/health`）要求请求头 `Authorization: Bearer <token>`。任务与截图按同一鉴权边界保护，仅有 ID 不能读取。

| 接口 | 行为 |
| --- | --- |
| `POST /v1/tasks` | 一题一任务，202 受理。请求头 `Idempotency-Key`（缺省时服务端按题文+关联生成）：同 key 同参数复用原任务（`reused: true`，不重复发问）；同 key 不同参数 → 409。题文上限 2000 字，超限 400，不静默截断。队列满 → 429。 |
| `GET /v1/tasks/{task_id}` | 状态、排队/运行/尝试记录、结果（终态后可反复查询，不触发新提问）。 |
| `POST /v1/tasks/{task_id}/cancel` | 排队中 → 直接 `cancelled` 且永不提交；运行中 → 先停当前会话再标 `cancelled`；已终态 → 原样返回。重复取消不创建任务。 |
| `GET /v1/tasks/{task_id}/assets/{asset_id}` | 本任务截图（PNG），同样需要令牌。 |
| `GET /v1/health` | 仅服务可达性，不代表槽位可用。 |
| `GET /v1/slots` | 槽位配置/可用/忙碌/暂停/未初始化及简洁原因；不返回手机号、Cookie、密码。 |

请求示例：

```json
{
  "question_id": "Q01",
  "prompt": "小户型装风管机，有哪些品牌值得考虑？",
  "platform": "doubao",
  "mode": "web_search",
  "retry_limit": 1,
  "context": {"run_id": "example_run", "brand_id": "example_brand"}
}
```

`prompt` 原样提交，不改字、不加引导；`context` 仅原样关联留存，不发给豆包。每题新建独立对话。

### 状态与重试语义

- 状态机：`queued → running → succeeded / partial / failed`；取消经 `cancel_requested → cancelled`；已提交但结果无法确认 → `unknown`（保留记录待核对，不按时间自动改判）。
- 重试是同一任务的 attempt：默认最多重试 1 次（`retry_limit` 0/1）。仅对**已确认未提交**或**已明确失败结束**的故障重试；已提交且结果未知时绝不换账号重发。
- `succeeded` 的含义：提交可确认 + 联网执行有证据 + 回答结束有证据（操作栏出现、无停止控件、正文稳定）+ 完整正文与必要截图已保存。任一关键证据缺失 → `partial`/`failed`/`unknown` 的真实状态，不用 HTTP 200 冒充采集成功。
- 拒答作为已完成网页输出保存，`response_kind: "refusal"` 单列，不当作有效推荐答案。
- 服务重启：已完成结果保留；未提交的排队任务恢复执行；运行中任务一律转 `unknown`（错误码 `SERVICE_RESTART_UNVERIFIED`），不无条件重入队。
- 单题运行等待上限默认 360 秒（配置 `run_timeout_sec`），排队另算。

### 结果字段（节选）

`answer{text, markdown, completeness}`、`web_search_observed`（true/false/null + `attempts[].evidence.web_search_signals` 迹象）、`search_queries{items, known}`（网页未展示真实检索词时 known=false，不从题文拆词冒充）、`references{items[{title,url,site}], complete}`（complete 无法核对时为 null）、`citations`（正文 `[n]` 标记可映射时给出，无法区分时 null + `citations_note`）、`conversation_url`、`screenshots[{asset_id, coverage}]`。Token 用量网页端不可观察，不提供、不补 0。

## 运行

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium   # 首次
cp config.example.json config.json                # 填 token 与槽位档案路径
./run.sh                                          # 自动拉起 Xvfb 并启动服务
```

生产部署为 systemd 单元 `doubao-collector.service`（WorkingDirectory 为本目录，`ExecStart=run.sh`，`Restart=always`）。

配置（`config.json`，不入 Git 的只有 token 与本机路径）：

| 键 | 说明 |
| --- | --- |
| `token` | 接口令牌（也可用环境变量 `COLLECTOR_TOKEN` 覆盖） |
| `port` / `host` | 默认 8790 / 0.0.0.0 |
| `driver` | `doubao`（真实）或 `fake`（受控测试专用合成驱动，结果必须标注合成） |
| `queue_limit` | 排队上限，默认 100 |
| `run_timeout_sec` | 单题运行等待上限，默认 360 |
| `slots[]` | `{slot_id, profile_dir, enabled}`，最多 5 个 |

## 账号与维护

- 新增槽位账号：把已登录的浏览器资料目录放到 `profiles/slot-N/`（含 `Default/` 子目录），重启服务；槽位状态以 `/v1/slots` 为准（未初始化会显示原因）。
- 登录维护入口（受保护）：服务器上运行有头浏览器 + x11vnc + noVNC（端口 6080，密码保护），维护人员可远程人工登录、处理真人验证。调试端口、Cookie、资料目录不对外公开。
- 身份失效或触发真人验证时，对应槽位自动 `paused` 并给出原因，其他槽位继续工作；维护完成后重启服务恢复槽位。

## 调试页

`GET /` 提供简易调试页：填令牌 → 提交题文 → 看排队/执行/结果/原文 → 取消 → 看五槽状态。截图在调试页内引用需带令牌的接口地址，建议用接口工具核对原图。

## 成本与依赖

- 运行成本：零付费项。开源组件（FastAPI/uvicorn/Playwright/Chromium/SQLite）、自有服务器、自有账号；无 LLM/Agent 运行时依赖，无 Token 消耗。
- 账号：由服务运营方提供并维护（当前 slot-1 为运营方自有账号）。
- 迁移：代码与数据（SQLite + PNG）可整体导出；浏览器资料目录与具体机器绑定，换机需重新人工登录。

## 已知边界（与自验报告一致）

- 当前仅 slot-1 有真实账号；其余槽位为未初始化状态，5 账号真实并发吞吐**未验证**（受控合成验证覆盖了调度/隔离/取消/重试/重启语义）。
- 本机出口曾被豆包风控连续拦截提问（真人验证）；账号持有人经维护入口人工通过一次后，连续 4 道真实题正常完成（详见 `SELFTEST.md`）。这不构成对未来不触发验证的承诺：再触发时服务按设计暂停槽位并返回真实原因。
- 资料面板的实际渲染条数小于网页声明条数（实测 25 篇声明只渲染 16 条链接），`references.complete` 会如实为 false，声明数保存在 `references.declared_count`。
- 检索词词面网页端不展示（只显示个数），`search_queries.known` 常态为 false、个数在 `declared_count`。
