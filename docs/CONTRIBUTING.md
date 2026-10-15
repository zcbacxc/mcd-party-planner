# 贡献指南

## 开发设置

```bash
git clone https://github.com/zcbacxc/mcd-party-planner.git
cd mcd-party-planner

# 配置凭据（申请入口：https://open.mcd.cn/mcp，手机号登录 → 控制台 → 激活）
cp .env.example .env
# 编辑 .env 填入 MCD_MCP_TOKEN

python -m src.cli selfcheck    # 应输出「Token 已就绪」
```

无需虚拟环境、无需安装依赖：**仅依赖 Python 标准库**（Python 3.9+）。

## 运行测试

```bash
python -m unittest discover -s tests -p "test_*.py"   # 单元测试（离线，无需 Token）
python tests/smoke.py                                  # 真实 MCP 端到端（需 Token）
```

## 目录结构

```
mcd-party-planner/
├── src/
│   ├── mcp_client.py       # MCP 调用层：鉴权、节流、429 退避、结果解析、入参白名单
│   ├── party_planner.py    # 业务层：三场景编排 + 报告渲染
│   ├── cli.py              # 命令行入口（selfcheck/tools/probe/plan/party/calendar）
│   └── config.py           # 本地 .env 读取（Token 不回显）
├── skill/SKILL.md          # WorkBuddy Skill 定义（主交付形态）
├── tests/                  # unittest 单元测试 + smoke 真实端到端
├── docs/                   # 开发文档（本文件、CHANGELOG 等）
├── MCP_INTEGRATION.md      # MCP 集成说明（赛事交付物）
├── workbuddy.md            # WorkBuddy 开发上下文（赛事交付物）
├── CONTEST_DECLARATION.md  # 参赛声明（赛事规则：文件名与内容均不可改动）
└── mcp-config.example.json # 脱敏 MCP 配置示例
```

## 代码风格

- 遵循各模块现有风格：模块顶部写职责说明，类型注解齐全，注释说明“为什么”。
- **调用层与业务层严格分离**：业务层不得直接发 HTTP，只通过 `client.call_tool(name, args)`。
- **新增工具先登记再用**：在 `TOOL_ARG_SPEC` 登记工具名与入参字段，禁止在业务代码里硬编码未登记的入参。
- **降级优于中断**：菜单为空、计价失败、门店非营业、助餐不可用，写入 `warnings` 并继续，不抛异常终止。
- **金额单位**：服务端为“分”，换算只在调用层完成，业务层与输出一律为“元”。
- **新增逻辑必须带测试**：替身客户端（`StubClient`）返回**真实响应形态**的样本，不写理想化结构。
- 注释与文档使用简体中文；代码标识符保持英文。
- 不得在注释或文档中出现内部追踪码（如 `EP*`、`WP*`、`NA-M*` 一类），用技术描述表达。

## 提交约定

- `feat:` —— 新功能
- `fix:` —— 缺陷修复
- `docs:` —— 仅文档变更
- `chore:` —— 维护、工具、赛事交付物
- `refactor:` —— 既不修缺陷也不加功能的重构

## MCP schema 校准流程

服务端 schema 会演进，任何“调用失败 / 返回结构不符”的排查都按以下顺序：

1. `python -m src.cli selfcheck` —— 确认 Token 有效；
2. `python -m src.cli probe` —— 拉取最新 `tools/list`（写入 `docs/mcp-schema.json`，已 gitignore）；
3. 对照结果更新 `src/mcp_client.py:TOOL_ARG_SPEC` 与响应解析逻辑；
4. 更新 `MCP_INTEGRATION.md` 中的工具清单与调用流程；
5. 跑 `python -m unittest discover -s tests` 与 `python tests/smoke.py` 验证。

**禁止凭官方 README 描述推断工具名与入参** —— 已实测存在系统性差异
（排查与校准流程见上文「MCP schema 校准流程」）。

## 合规红线

以下行为属于硬性禁止，违反会直接危及参赛资格：

- 提交真实 MCP Token、手机号、收货信息到仓库（含测试与示例）；
- 修改 `CONTEST_DECLARATION.md` 的文件名或内容；
- 出现与其他品牌对比、贬损麦当劳品牌、或宣扬不健康饮食的文案；
- 刷 Star / 多账号互点等操纵榜单行为；
- 输出编造的价格或餐品数据（计价失败必须显式提示并跳过该方案）。
