# 架构决策记录（ADR）

## 说明

### 什么是 ADR

架构决策记录（Architecture Decision Record）是一份简短、自包含的笔记，用于捕获单个重要技术决策：
我们面对什么问题、做了什么选择、为什么这么选、代价是什么、以及考虑过哪些替代方案。
**ADR 一旦写入即不可改写**——若决策发生变化，应新增一条 ADR 并将其标记为取代旧记录。

### 如何新增一条 ADR

1. 取下一个可用编号（如已有 ADR-003，则新增 ADR-004）；
2. 复制标准模板，填写七个部分：Status / Context / Decision Drivers / Considered Options /
   Decision Outcome / Consequences / References；
3. 在文末追加 `## ADR-NNN` 章节；
4. 在下方「决策索引」表追加一行；
5. **每条 ADR 必须基于项目真实代码与历史，不得编造不存在的架构细节。**

## 决策索引

| 编号 | 标题 | 状态 | 涉及模块 |
|---|---|---|---|
| ADR-001 | 调用层与业务层分离，工具入参集中在单一校准点 | Accepted | `src/mcp_client.py`、`src/party_planner.py` |
| ADR-002 | 主题活动下单默认 `dry_run`，真实下单需显式确认 | Accepted | `src/party_planner.py`、`src/cli.py` |
| ADR-003 | 场景级失败降级不中断，以 `warnings` 呈现 | Accepted | `src/party_planner.py` |

---

## ADR-001: 调用层与业务层分离，工具入参集中在单一校准点

**Status:** Accepted
**Version:** 自 v1.0.0 起生效

**Context**

本项目要编排麦当劳 MCP 的十余个工具，而服务端 schema 与官方 README 描述存在系统性差异
（工具名、入参字段名、金额单位均不同），且 schema 会持续演进。如果工具调用散落在业务代码里，
每次服务端变更都要在多处修改，且容易漏改。

**Decision Drivers**

- 服务端 schema 与文档描述不一致，且会演进，变更必须可控；
- 鉴权、限流、429 退避、结果解析属横切关注点，不应在业务层重复实现；
- 单元测试需要在不发真实请求的前提下覆盖业务编排逻辑。

**Considered Options**

- 在业务函数中直接发 HTTP 请求（否决：横切逻辑重复，且无法用替身客户端做单测）；
- 每个工具封装一个独立函数、散落在业务模块（否决：入参分散，schema 变更需多处修改）；
- 统一调用层 + 入参白名单集中声明（**采纳**）。

**Decision Outcome**

- `src/mcp_client.py` 只负责传输：Streamable HTTP + Bearer 鉴权、滑动窗口节流（默认 300 次/分钟，
  官方上限 600）、429 指数退避、SSE 与纯 JSON 双解析、`Mcp-Session-Id` 透传；
- 所有工具的入参字段集中在 `TOOL_ARG_SPEC` 声明，`build_arguments()` 按白名单过滤，
  **未登记的字段不会发送到服务端**；
- 业务层（`src/party_planner.py`）只依赖 `client.call_tool(name, args)` 这一鸭子接口，
  单测可用替身客户端注入真实形态的响应样本。

**Consequences**

- 正面：schema 变更只需改 `TOOL_ARG_SPEC` 一处；业务层零网络依赖，单测可离线运行；
- 负面：新增工具必须先登记再使用，多一道手续；未登记字段被静默丢弃，
  排查时需先确认 `TOOL_ARG_SPEC` 是否已更新（这也是最初出现「缺少参数」报错的根因）。

**References**

- `src/mcp_client.py`
- `MCP_INTEGRATION.md`
- `docs/CONTRIBUTING.md`（MCP schema 校准流程）

---

## ADR-002: 主题活动下单默认 `dry_run`，真实下单需显式确认

**Status:** Accepted
**Version:** 自 v1.0.0 起生效

**Context**

主题活动（派对 / 品鉴会）链路的终点是 `party-order-create`，它会创建一笔**真实订单**。
而该链路需串联「商品 → 城市 → 门店 → 日期 → 场次」五级查询，自动执行时一旦某级选错（例如城市未命中
而回退到别的城市），用户会在不知情的情况下订到异地门店。

**Decision Drivers**

- 下单是不可逆的真实交易行为，误操作的代价高；
- 五级链路任一级挑选出错都会传导到下单结果；
- 参赛演示需要展示完整链路，但又不能产生真实订单。

**Considered Options**

- 链路跑到底直接下单（否决：误下单风险不可接受）；
- 提供独立的下单子命令、链路只做查询（否决：无法演示完整链路）；
- 链路照跑但默认 `dry_run=True`，输出可下单载荷，仅在 `--confirm` 时才真实下单（**采纳**）。

**Decision Outcome**

- `plan_party_booking()` 默认 `dry_run=True`：完成五级查询后输出 `orderPayload`，
  不调用 `party-order-create`；
- CLI 需显式 `--confirm` 才真实下单；
- 城市未命中时**不做「取第一个城市」的兜底**，直接返回失败并列出可选城市。

**Consequences**

- 正面：杜绝误下单与异地预约；演示与真实行为边界清晰；
- 负面：用户必须多敲一个 `--confirm`；`orderPayload` 的完整性与可下单性依赖服务端字段
  （如 `skuId` 需额外调用 `mall-product-detail`），字段缺失会导致真实下单失败。

**References**

- `src/party_planner.py:plan_party_booking`
- `src/cli.py`（`party` 子命令）
- `skill/SKILL.md`

---

## ADR-003: 场景级失败降级不中断，以 `warnings` 呈现

**Status:** Accepted
**Version:** 自 v1.0.0 起生效

**Context**

真实服务端存在多种"部分失败"：门店非营业时菜单查询失败、部分餐品「每单限购 1 件」导致计价失败、
门店不支持企业团餐助餐服务、账号无配送地址等。若任一失败即抛异常终止，整个场景将无输出。

**Decision Drivers**

- 团餐方案需要"多店 × 多组合"比对，单点失败不应浪费其余结果；
- 限购等业务约束是常态，而非异常；
- 输出必须诚实——失败的组合不能编造价格。

**Considered Options**

- 任一失败即中断并报错（否决：多店比对场景可用性极差）；
- 静默跳过失败项（否决：用户无从知晓方案为何缺失）；
- 跳过失败项并写入 `warnings`，在报告中以 `！提示：` 呈现（**采纳**）。

**Decision Outcome**

- 菜单为空、计价失败、门店非营业、助餐不可用、无配送地址等，均记入 `warnings` 并继续；
- 组合缺少某个品类角色时，按已得品类降级生成方案，并在 `warnings` 中注明"降级：缺少 X"；
- 报告渲染时统一输出提示行，用户可据此判断结果完整性。

**Consequences**

- 正面：单次运行可得到尽可能多的可用方案，且失败原因对用户透明；
- 负面：调用方需主动检查 `warnings` 才能发现方案不完整；
  `plans` 可能少于预期（如所有组合均被限购拦截），需靠 `affordableCount` 等指标判断。

**References**

- `src/party_planner.py`（`plan_group_meal` / `suggest_calendar_plan` / `_group_meal_assistance`）
- `tests/test_party_planner.py`（降级路径用例）
