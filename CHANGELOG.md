# 更新日志

本项目变更记录遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 分类：
`Added` / `Changed` / `Deprecated` / `Removed` / `Fixed` / `Security`。

## [Unreleased]

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

[Unreleased]: https://github.com/zcbacxc/mcd-party-planner/compare/v1.0.1...HEAD
[1.0.1]: https://github.com/zcbacxc/mcd-party-planner/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/zcbacxc/mcd-party-planner/releases/tag/v1.0.0
