# WorkBuddy 开发对话上下文

> 用途：本文件用于核验「麦当劳程序员节创意开发大赛」WorkBuddy 专项奖励条件
> （规则：参赛项目须真实使用腾讯 WorkBuddy 智能体，并提交 `workbuddy.md`）。
> 以下内容为本项目在 WorkBuddy 中开发过程的真实上下文记录。

- 项目：mcd-party-planner（麦当劳聚会 / 团餐组织助手）
- 开发时间：2026-10-16
- 开发环境：WorkBuddy（Intelligent Design Assistant）＋ 本机 Python 3.13（仅标准库）
- 关联 MCP：mcd-mcp（麦当劳 MCP，`https://mcp.mcd.cn`，Streamable HTTP）

---

## 1. 需求提出

**用户**：调研 McDonald's 开发者创新挑战赛仓库，产出结构化调研摘要；再基于摘要输出可直接落地的实施方案；最后严格按方案执行实现。

**WorkBuddy 处理**：
1. 抓取并解析活动仓库 `README.md`、`activityGuidelines.md`、`CONTEST_DECLARATION.md`、`RANKING.md`，以及 `mcd-mcp-server` 的工具清单与 GitHub API 实时数据；
2. 产出调研摘要，逐项标注「仓库中未说明」；
3. 产出实施方案，含目标分级、六阶段排期、技术选型、风险与验收标准、8 项待确认问题。

## 2. 方案落地过程中的关键决策（对话要点）

| # | 决策 | 依据 |
|---|---|---|
| 1 | 选题定为「聚会 / 团餐组织」 | 榜单 Top3 `mcd-party-officer` 244★ 验证天花板；竞争密度低于省钱类；与组织者真实场景契合 |
| 2 | 主形态为 WorkBuddy Skill，辅以 Python 逻辑层 | 官方合作伙伴 + Skill 属规则明文形态 + 复杂编排用 Python 表达 |
| 3 | 三个核心场景，不做更多 | 控制 9 天窗口内的工作量（范围控制：不扩场景） |
| 4 | 主题活动下单默认 `dry_run=True` | 避免误真实下单；需显式 `--confirm` + 联系人信息才调用 `party-order-create` |
| 5 | 客户端节流默认 300 次/分钟 | 官方上限 600，留 50% 余量 |
| 6 | `CONTEST_DECLARATION.md` 字节级复制官方文件 | 规则：文件名及内容均不可改动 |

## 3. 在 WorkBuddy 中完成的具体工作（执行记录）

| 阶段 | WorkBuddy 执行内容 | 产出 |
|---|---|---|
| A | 环境勘查（`gh` 登录状态、Python 版本）、建公开仓库 `zcbacxc/mcd-party-planner`、按官方 API 原样拉取参赛声明 | 仓库 + `CONTEST_DECLARATION.md`（sha256 与官方一致） |
| B | 冻结选题与目录结构 | `skill/`、`src/`、`tests/` 骨架 |
| C | 编写 MCP 调用层、业务逻辑层、CLI、单元测试与冒烟脚本；修复 4 处测试失败（分类关键字 `" ".join` 误用中文字符串、组合降级未记录、预算用例取值、通知消息占用响应脚本） | `src/mcp_client.py`、`src/party_planner.py`、`src/cli.py`、`src/config.py`、`tests/*`（32 项测试全通过） |
| C′ | 按真实 schema 校准 `TOOL_ARG_SPEC` 与业务层（工具名、入参、金额单位、餐品名补齐），补充 2 处真实数据适配（活动标题在 `articleDto`、助餐服务名在 `gmServiceName`） | `tests/*`（40 项测试全通过）+ `tests/smoke.py` 三场景真实调用全通过 |
| D | 编写集成说明、Skill 定义、README、本文件；敏感信息扫描 | `MCP_INTEGRATION.md`、`skill/SKILL.md`、`README.md`、`workbuddy.md`、`mcp-config.example.json` |

## 4. 真实 MCP 接入与 schema 校准（已实测完成）

MCP Token 已在 WorkBuddy 中配置为 `mcd-mcp` 连接器，随后在 WorkBuddy 中完成了真实联调：

1. `python -m src.cli probe` 拉取服务端 `tools/list`，确认**真实工具数为 35 个**；
2. 发现 README 描述与真实 schema 存在多处差异，据此校准：
   - 主题活动工具实际名为 `query-party-store-date` / `query-party-store-session`（非 `query-partystore-*`）；
   - `query-nearby-stores` 无 address 参数，用 `beType + searchType + city/keyword`；
   - 计价商品条目字段为 `items[].productCode`；
   - 金额单位为**分**（`data.price=3180` → ¥31.80）；
   - 菜单只返回 `code`，餐品名需 `query-meal-detail` 补齐；
   - 主题活动链路全程需要 `spuId`（由 `mall-points-products` 发现）。
3. 校准后跑通三场景真实调用：

- 团餐方案：`query-nearby-stores` → `query-meals` → `query-meal-detail` → `calculate-price`
- 主题活动：`mall-points-products` →（可选 `mall-product-detail`）→ `query-party-city`
  → `query-party-store` → `query-party-store-date` → `query-party-store-session`
  →（dry_run 默认不下单）`party-order-create`
- 活动日历：`campaign-calendar` × `query-nearby-stores`，并
  `delivery-query-addresses` → `delivery-query-stores` → `query-meal-assistance`

实测结果（成都 · 春熙路，8 人 / ¥400）：经济型 ¥352.0（人均 ¥44.0，预算内）、均衡型 ¥591.2（超预算）；
派对预约命中「麦当劳奇妙生日派对」spuId=1779，成都高新区大魔方餐厅 2026-10-16 12:00-13:30 场次余位 12、¥50.0；
活动日历取到真实活动标题，并查得 3 家门店支持「保鲜速达」助餐服务。

校准方式（WorkBuddy 中执行的命令）：

```bash
python -m src.cli selfcheck   # 环境自检
python -m src.cli probe       # 拉取 tools/list 的 inputSchema → docs/mcp-schema.json
python tests/smoke.py         # 真实 MCP 端到端冒烟
```

## 5. 备注

- Token 仅存于本地 `.env`（`.gitignore` 排除），未写入仓库任何文件；
- 用户输入（地址、人数、预算）仅运行时内存使用，不落盘；
- 所有餐品与价格数据均为 MCP 实时返回，仓库内不含任何静态菜单或价格快照。
