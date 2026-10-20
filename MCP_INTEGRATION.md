# MCP 集成说明

> 对应《麦当劳程序员节创意开发大赛活动规则》对 `MCP_INTEGRATION.md` 的要求：
> 说明实际使用的麦当劳 MCP Server、Tool、调用流程和业务价值。
>
> 本文中的工具名、入参与响应结构均以 `python -m src.cli probe` 拉取的
> **真实 `tools/list` schema** 为准（服务端当前共 35 个 Tool）。

## 1. 使用的 MCP Server

| 项 | 值 |
|---|---|
| 名称 | 麦当劳 MCP 服务（mcd-mcp） |
| 接入地址 | `https://mcp.mcd.cn` |
| 传输协议 | Streamable HTTP |
| 鉴权 | `Authorization: Bearer <MCP Token>`，Token 从环境变量 `MCD_MCP_TOKEN` 读取 |
| 客户端配置 | 见 `mcp-config.example.json`（仅环境变量占位符，不含真实凭证） |
| 官方限流 | 600 次 / 分钟（超限返回 429） |
| 适用地区 | 中国大陆地区（不含港澳台） |

**服务端关键约定（实测）**

- 金额单位为**分**：`calculate-price` 返回 `data.price=3180` 表示 ¥31.80，本项目统一除以 100 后再展示；
- 菜单接口 `query-meals` 只返回餐品 `code` 与分类名，**餐品名称需用 `query-meal-detail` 补齐**；
- 门店查询无 address 参数，使用 `beType`（1-到店取餐 / 2-麦乐送 / 5-得来速 / 6-企业团餐）+ `searchType`（2-按位置搜索）+ `city` / `keyword`；
- 非营业门店调用 `query-meals` 会返回「门店可能已关闭或不在营业时间」，故优先筛选 `businessStatus` 为真的门店；
- 主题活动（派对 / 品鉴会）链路全程需要 `spuId`，来自麦麦商城商品列表。

## 2. 使用的 Tool 清单

本项目共接入麦当劳 MCP 的 **15 个 Tool**，覆盖三个核心场景：

| # | Tool 名称 | 能力域 | 在本项目中的用途 |
|:--:|---|---|---|
| 1 | `query-nearby-stores` | 门店 | 按「城市 + 关键词」取附近门店（beType=1、searchType=2），作为方案比对候选集 |
| 2 | `query-meals` | 点餐 | 拉取门店当前可售餐品的分类与餐品 code |
| 3 | `query-meal-detail` | 点餐 | 按 code 补齐餐品名称（菜单接口不返回名称） |
| 4 | `calculate-price` | 点餐 | 对候选组合计算应付总价（真实计价，返回单位为分） |
| 5 | `campaign-calendar` | 活动日历 | 查询活动日历（dailyList，含往期 / 今日 / 未来） |
| 6 | `delivery-query-addresses` | 外送 | 取账号配送地址，用于团餐门店查询 |
| 7 | `delivery-query-stores` | 外送 | 按地址取可配送门店，获得团餐场景必需的 `beCode` |
| 8 | `query-meal-assistance` | 团餐 | 查询门店企业团餐助餐服务（beType=6、orderType=2） |
| 9 | `mall-points-products` | 麦麦商城 | 发现派对 / 品鉴类主题商品，取得主题活动入口 `spuId` |
| 10 | `mall-product-detail` | 麦麦商城 | 由 spuId 取 `skuId`，供下单载荷使用 |
| 11 | `query-party-city` | 主题活动 | 查询主题活动可参与城市（返回城市 code 与经纬度） |
| 12 | `query-party-store` | 主题活动 | 按城市 code + 经纬度查询可参与门店 |
| 13 | `query-party-store-date` | 主题活动 | 查询门店可预约日期 |
| 14 | `query-party-store-session` | 主题活动 | 查询指定日期可预约场次（含余位、价格、人数区间） |
| 15 | `party-order-create` | 主题活动 | 创建主题活动订单（**默认 dry_run，不调用**） |

## 3. 调用流程

### 3.1 场景一：团餐方案生成

```text
query-nearby-stores(beType=1, searchType=2, city, keyword)
   │  └─ 优先取 businessStatus=true 的门店
   ├─▶ query-meals(storeCode, orderType=1, beType=1)
   │      └─ categories[] → 按分类名映射角色（主食/小食/饮料/甜品）
   ├─▶ query-meal-detail(storeCode, code)      # 补齐餐品名称，带缓存
   └─▶ calculate-price(storeCode, orderType=1, beType=1,
                       items=[{productCode, quantity=人数}])
          └─ data.price ÷ 100 = 应付金额 → 人均 = 总价 / 人数

汇总排序：预算内优先，其次人均升序 → 输出 ≥2 套可比对方案
```

### 3.2 场景二：主题活动（派对 / 品鉴会）预约

```text
mall-points-products()
   └─▶ 命中「派对」类 spuId
        └─▶ query-party-city(spuId) → 城市 code + 经纬度
             └─▶ query-party-store(spuId, code, latitude, longitude)
                  └─▶ query-party-store-date(spuId, storeCode)
                       └─▶ query-party-store-session(spuId, storeCode, dateStr)
                            ├─ [dry_run 默认] mall-product-detail(spuId) 取 skuId，
                            │                  仅输出可下单载荷
                            └─ [--confirm]   party-order-create(...)
```

### 3.3 场景三：活动日历 × 附近门店 组织建议

```text
campaign-calendar(specifiedDate?)  ┐
                                   ├─▶ 交叉生成「活动日历 × 附近门店」组织建议
query-nearby-stores(city, keyword) ┘        │
                                            └─▶ delivery-query-addresses
                                                 └─ delivery-query-stores(addressId, beType=6)
                                                      └─ query-meal-assistance(
                                                           storeCode, beCode, beType=6, orderType=2)
                                                         （任一环节失败即降级标注，不中断）
```

### 3.4 调用层保障

- **节流**：滑动窗口节流器，默认 300 次 / 分钟（官方上限 600 的 50%）；
- **退避**：429 按 0.5s → 1s → 2s 指数退避重试最多 3 次；5xx 同样退避；
- **重试分级**：读工具（查询 / 计价）遇 5xx 可安全重试；**写工具**（`party-order-create`、
  `create-order`、`mall-create-order`、`delivery-create-address`、`cancel-order` 等，
  登记于 `src/mcp_client.py:WRITE_TOOLS`）**只对 429 重试**，5xx 立即失败并提示
  「请求可能已被受理，请先到官方渠道核对」——避免重复下单等副作用；
- **网络异常**：代理 502、DNS 失败、连接重置、超时统一归一化为 `McpNetworkError`，
  CLI 只输出一行 `MCP 调用失败：网络不可达：…`，不向用户抛 Python 堆栈；
- **鉴权失败**：401 立即抛出明确错误，提示 Token 无效或过期；
- **结果解析**：优先 `structuredContent`，其次解析 `content[].text`，兼容 SSE 与纯 JSON；
- **入参收敛**：所有工具入参由 `src/mcp_client.py:TOOL_ARG_SPEC` 统一声明并过滤，
  未声明字段不会发送到服务端；校准只需改这一处；
- **业务降级**：菜单为空、计价失败（如「每单限购 1 件」）、门店非营业、助餐服务不可用，
  均以 warning 形式呈现并跳过该方案，不中断整体流程。

## 4. 业务价值

| 场景 | 用户痛点 | MCP 带来的价值 |
|---|---|---|
| 团餐方案生成 | 组织 8–30 人聚餐时靠手工凑单、心算预算，既慢又易超支 | 输入「城市 + 关键词 + 人数 + 预算」，实时拉取真实菜单与真实计价，输出 ≥2 套含人均对比的方案，凑单时间从十几分钟压到十几秒 |
| 主题活动预约 | 派对 / 品鉴会需跨「商品 → 城市 → 门店 → 日期 → 场次」五级查询，路径深且易漏 | 自动串联五级查询，输出可直接下单的载荷，并用 dry_run 兜底避免误下单 |
| 活动日历组织建议 | 不知道附近门店当下有什么活动，错过组织窗口 | 把活动日历（往期 / 今日 / 未来）与附近门店交叉，给出组织时机建议，并提示企业团餐助餐服务可用性 |

## 5. 真实调用示例（实测输出节选）

```text
【团餐方案】
城市：成都　关键词：春熙路　人数：8　预算：¥400.0
已扫描门店：2　可选方案：4　预算内方案：1

1. [经济型] 麦当劳成都锦江总府路茂业百货餐厅　合计 ¥352.0　人均 ¥44.0　预算内
   构成：精选单人餐三件套×8、小杯鲜萃咖啡×8
3. [均衡型] 麦当劳成都锦江总府路茂业百货餐厅　合计 ¥591.2　人均 ¥73.9　超预算
   构成：精选单人餐三件套×8、龙焰芝士棒鸡腿堡三件套×8、小杯鲜萃咖啡×8

推荐：[经济型] 麦当劳成都锦江总府路茂业百货餐厅，合计 ¥352.0，人均 ¥44.0
！提示：门店 … / 丰盛型 计价失败：每单限购1件，去看看其他餐品吧
```

```text
【主题活动预约】
主题：麦当劳奇妙生日派对（spuId=1779）
城市：成都市　门店：麦当劳成都高新区大魔方餐厅（3450261）
日期：2026-10-16　场次：12:00-13:30　余位：12　价格：¥50.0
调用链路：query-party-city → query-party-store → query-party-store-date → query-party-store-session
dry_run=True：未真实下单，仅输出可下单载荷
```

## 6. 校准方式

```bash
python -m src.cli selfcheck   # 环境自检
python -m src.cli probe       # 拉取 tools/list 的 inputSchema → docs/mcp-schema.json
python tests/smoke.py         # 真实 MCP 端到端冒烟（三场景联通）
```

若服务端 schema 变更，只需更新 `src/mcp_client.py` 的 `TOOL_ARG_SPEC` 与响应解析辅助函数，
业务层代码无需改动。
