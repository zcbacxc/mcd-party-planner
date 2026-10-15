# 更新日志

本项目变更记录遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 分类：
`Added` / `Changed` / `Deprecated` / `Removed` / `Fixed` / `Security`。

## [Unreleased]

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

[Unreleased]: https://github.com/zcbacxc/mcd-party-planner/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/zcbacxc/mcd-party-planner/releases/tag/v1.0.0
