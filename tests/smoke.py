"""真实 MCP 端到端冒烟（团餐规划 / 派对预约 / 日历建议三场景联通校验）。

前置条件：环境变量 MCD_MCP_TOKEN 或仓库根目录 .env 中配置真实 Token。
Token 缺失时以退出码 0 跳过（明确打印原因），不判定为失败，便于 CI 与本地通用执行。

用法：
    python tests/smoke.py
    python tests/smoke.py --city 成都 --keyword 春熙路 --headcount 8 --budget 300
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import config, party_planner  # noqa: E402
from src.mcp_client import McpClient, McpError  # noqa: E402


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    parser = argparse.ArgumentParser(description="mcd-party-planner 真实 MCP 冒烟测试")
    parser.add_argument("--city", default="成都")
    parser.add_argument("--keyword", default="春熙路")
    parser.add_argument("--headcount", type=int, default=8)
    parser.add_argument("--budget", type=float, default=300)
    parser.add_argument("--store-limit", type=int, default=2)
    args = parser.parse_args()

    config.load_local_env(root)
    if not config.get_token():
        print("跳过：未配置 MCD_MCP_TOKEN（阶段 A 的 A2 待完成），冒烟测试不执行真实调用。")
        return 0

    client = McpClient(rate_limit_per_minute=300)
    print("=== 环境 ===")
    print(json.dumps(config.describe(), ensure_ascii=False))

    started = time.time()
    failures = []

    print("\n=== 场景 1：团餐方案生成 ===")
    plan = party_planner.plan_group_meal(client, args.city, args.keyword,
                                         args.headcount, args.budget,
                                         store_limit=args.store_limit)
    print(party_planner.render_group_meal_report(plan))
    if len(plan["plans"]) < 2:
        failures.append("团餐方案不足 2 套")

    print("\n=== 场景 2：主题活动预约（dry_run）===")
    party = party_planner.plan_party_booking(client, city_name=args.city + "市")
    print(party_planner.render_party_report(party))
    if not party.get("ok"):
        failures.append(f"主题活动链路未完成（{party.get('reason')}）")

    print("\n=== 场景 3：活动日历组织建议 ===")
    calendar = party_planner.suggest_calendar_plan(client, args.city, args.keyword,
                                                   store_limit=args.store_limit)
    print(party_planner.render_calendar_report(calendar))
    if not calendar["days"]:
        failures.append("活动日历无数据")

    print(f"\n=== 冒烟完成，用时 {time.time() - started:.1f}s ===")
    if failures:
        for item in failures:
            print("x " + item)
        return 1
    print("三场景联通校验全部通过（真实 MCP 调用）")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except McpError as exc:
        print(f"MCP 调用失败：{exc}", file=sys.stderr)
        raise SystemExit(1)
