# 更新日志

本项目变更记录遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 分类：
`Added` / `Changed` / `Deprecated` / `Removed` / `Fixed` / `Security`。

## [Unreleased]

### Added

- **协议版本协商**：握手声明 `2025-06-18`，服务端返回不支持的版本时立即中止并给出双方版本；
  握手成功后所有请求携带 `MCP-Protocol-Version` 头（2025-06-18 规范要求）。
- **`tools/list` 分页**：跟随 `nextCursor` 翻页，上限 10 页，防止异常 cursor 导致死循环。
- **会话终止**：新增 `McpClient.close()`，CLI 退出时尽力发送 HTTP `DELETE`，失败不影响结果输出。
- **`--verbose` 调用统计**：在 stderr 输出 `calls` / `retries` / `rateLimitWaits` / `elapsedMs`，
  便于自证限流安全；stdout 报告不受影响。
- **`probe` 保留完整工具元数据**：快照新增 `title` / `outputSchema` / `annotations`
  （实测服务端 35/35 个工具都有 `outputSchema`）。
- **端点白名单**：`--endpoint` 仅接受 `https://*.mcd.cn`，其它地址需显式加
  `--allow-custom-endpoint`，避免 Bearer Token 被引导到任意主机。
- **Tests**：27 项用例。协议协商与版本拒绝、协议头时机、分页与页数上限、`probe` 元数据、
  会话终止、调用统计、`--verbose` 输出分流、端点白名单、载荷剔除员工工号、日历标题去重、
  退避抖动边界、文档契约。测试总数 72 → **99 项**。

### Changed

- **重试退避改为 Full Jitter**：等待时长由固定 `0.5·2^n` 改为 `uniform(0, 0.5·2^n)`，
  避免多客户端同时重试；重试次数与总时长上界不变。

- **下单载荷剔除门店员工工号**：服务端场次对象中的 `cookEmpNo` / `opEmpNo` / `rgmEmpNo`
  不再随 `orderPayload.partyTimeInfo` 外显；其余字段原样保留，避免误删下单所需信息。
- **活动日历标题去重**：同一活动被服务端返回多条 event 时，报告中的同名标题只出现一次。

## [1.1.1] - 2026-10-20

### Changed

- **活动日历报告改为分组呈现**：原先逐日输出（真实服务端一次返回 13 天），同一句建议重复 11 次，
  重点被淹没。现按「今日 / 未来 / 往期」分组，**每组只输出一次建议**；未来活动默认只展开最近
  `CALENDAR_FUTURE_PREVIEW`（5）天，其余折叠为「…另有 N 天未来活动已省略」。
  输出行数由 40+ 行降至约 20 行，信息量不变。

### Added

- 新增 `version` 子命令与全局 `--version` 参数（均读取 `src/__init__.py:__version__`，保持版本单一来源）。
- **Tests**：5 项用例。`version` 子命令、`--version` 参数、`budget=0` 语义、非法入参退出码、
  日历分组与建议折叠。测试总数 67 → **72 项**。

### Fixed

- **`budget=0` 被当作「不限预算」**：`plan_group_meal` 的预算判定写作 `if budget else True`，
  零预算时所有方案都被标为「预算内」。改为 `budget is not None`，零预算即全部超预算，
  推荐仍给出最便宜方案作参考。
- **非法入参抛 Python 堆栈**：`--headcount 0`、`--budget -1` 等触发 `ValueError` 时直接打堆栈。
  现由 CLI 统一捕获，输出 `参数错误：…` 并以退出码 2 结束（与「Token 缺失」一致）。

### Removed

- 清理零引用符号：`iter_strings`、`CODE_KEYS`、`MEAL_CODE_KEYS`、`CATEGORY_KEYS`
  （定义后从未被调用，易被误读为「未完成的功能」）。

## [1.1.0] - 2026-10-20

### Fixed

- **写操作可能重复下单**：调用层对 429/5xx 一律重试，且不区分工具是否幂等；
  `party-order-create` 在 `--confirm` 下若遇服务端 5xx（订单可能已创建），重发会产生重复订单。
  新增 `WRITE_TOOLS`（下单 / 建址 / 取消 / 绑券 / 抽奖）与 `call_tool(idempotent=...)`：
  写操作**只对 429 重试**（请求被限流拒绝、未被受理），5xx 立即失败并提示「请求可能已被受理，
  请先到官方渠道核对」。读操作的重试行为不变。
- **网络异常裸抛 Python 堆栈**：`_http_transport` 原先只捕获 `HTTPError`，企业代理 502、
  DNS 失败、连接重置、超时都会把 `urllib.error.URLError` 直接抛给用户。现统一归一化为
  新增的 `McpNetworkError`，CLI 输出 `MCP 调用失败：网络不可达：…`（退出码 1，无堆栈）。
- **CI 绿勾含义不明**：未配置 `MCD_MCP_TOKEN` Secret 时真实 MCP 冒烟一直被跳过，但汇总门禁
  仍是绿色。现 `smoke` 任务输出 `tokenReady`，汇总门禁据此输出 notice/warning：
  已配置则声明「已验证真实链路」，未配置则明确标注「本次仅验证离线单测」；
  冒烟失败时门禁失败。

### Added

- **Tests**：12 项用例。写操作遇 5xx 不重试（含显式 `idempotent` 覆盖、`WRITE_TOOLS` 防回归）、
  写操作遇 429 仍重试、读操作 5xx 仍重试、`URLError` / 超时 / 连接重置归一化、
  `HTTPError` 不被网络分支吞掉、CLI 网络异常输出无堆栈、`--confirm` 显式声明非幂等、
  `dry_run` 不调用下单工具。测试总数 55 → **67 项**。
- `.gitignore` 增加 `.workbuddy-ai/`（AI 助手本地工作区，不入公开仓库）。

## [1.0.1] - 2026-10-20

### Fixed

- **CI 工作流校验失败导致 0 秒红叉**：`jobs.<job_id>.if` 中使用 `secrets` 上下文不被 GitHub 允许，
  整份工作流校验不通过（0 job、0 秒失败）。改为步骤级探测 Token 并写入 `GITHUB_OUTPUT`，
  未配置 Secret 时跳过冒烟而非让工作流失效。
- **CI 在 Python 3.9 上无法启动**：`ubuntu-latest` 已切换至 Ubuntu 26.04，其 `setup-python`
  版本清单不再提供 3.9 / 3.10 的 x64 构建。矩阵改为显式指定运行环境（3.9 / 3.10 → `ubuntu-22.04`，
  3.11+ → `ubuntu-latest`），保留 3.9 兼容性声明与验证。
- **主题活动链路单点失败即中断**：原实现只探测第一家门店的第一个日期，首店无场次或首场余位不足
  就直接返回失败。改为「门店 × 日期 × 场次」逐级探测（最多 3 店 × 3 日），
  并优先选择 `leftNum ≥ 人数` 的场次；显式指定 `--date` 时不会改到其他日期。
- 余位不足时新增 `！提示：` 警告，不再静默返回不可下单的场次。
- `probe` 子命令提示文案引用了已不存在的 `ITEM_PAYLOAD_CODE_KEY`，改为「响应解析辅助函数」。
- `initialize` 的 `clientInfo.version` 硬编码 `1.0.0`，改为取自 `src/__init__.py:__version__`，
  恢复「版本唯一来源」约定。

### Added

- **Tests**（`tests/test_cli.py`）：10 项命令行契约测试，覆盖参数必填与默认值、Token 缺失退出码 2、
  MCP 异常退出码 1、`--json` 可解析、`party` 默认不下单、自检不回显 Token 明文。
- 主题活动选场降级 4 项用例（换店回退、跳过余位不足的日期、余位不足告警、指定日期不可用）。
- 调用层 `clientInfo.version` 与包版本一致性用例。测试总数 40 → **55 项**。

## [1.0.0] - 2026-10-16

### Added

- 团餐方案生成：按「城市 + 关键词 + 人数 + 预算」比对附近门店，输出经济型 / 均衡型 / 丰盛型多套真实计价方案。
- 主题活动预约：自动串联「派对商品 → 城市 → 门店 → 日期 → 场次」，默认 `dry_run` 不真实下单。
- 活动日历组织建议：活动日历 × 附近门店交叉建议，并查询企业团餐助餐服务可用性。
- MCP 调用层：Streamable HTTP + Bearer 鉴权、滑动窗口节流（300 次/分钟）、429 指数退避、SSE 与纯 JSON 双解析。
- WorkBuddy Skill 定义（`skill/SKILL.md`）与开发上下文（`workbuddy.md`）。
- 赛事交付文件：`CONTEST_DECLARATION.md`（官方原文）、`MCP_INTEGRATION.md`、`mcp-config.example.json`。
- **Tests**（`tests/test_mcp_client.py`、`tests/test_party_planner.py`）：40 项覆盖调用层与三场景行为契约。

### Fixed

- 按真实 MCP schema 校准工具名与入参（`query-party-store-date` / `query-party-store-session`、
  `beType + searchType + city/keyword`、`items[].productCode`），修复此前入参被白名单过滤导致的「缺少参数」。
- 金额单位修正为服务端「分」，展示前统一换算为元。
- 餐品名称改由 `query-meal-detail` 补齐（菜单接口只返回 code）。
- 活动标题改取 `articleDto.title`（`activityTitle` 实际为空）。
- 城市未命中时不再静默取首个城市，避免把派对预约到异地。

[Unreleased]: https://github.com/zcbacxc/mcd-party-planner/compare/v1.1.1...HEAD
[1.1.1]: https://github.com/zcbacxc/mcd-party-planner/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/zcbacxc/mcd-party-planner/compare/v1.0.1...v1.1.0
[1.0.1]: https://github.com/zcbacxc/mcd-party-planner/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/zcbacxc/mcd-party-planner/releases/tag/v1.0.0
