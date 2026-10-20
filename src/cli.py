"""命令行入口。

子命令：
- version ：显示版本号
- selfcheck：环境自检（Token 是否配置，不回显明文）
- tools   ：列出服务端工具（校准 TOOL_ARG_SPEC / 阶段 A 的 A5）
- probe   ：导出工具 inputSchema 到 docs/mcp-schema.json（若目录存在则写入）
- plan    ：场景 1 团餐方案生成
- party   ：场景 2 主题活动预约（默认 dry_run，不真实下单）
- calendar：场景 3 活动日历组织建议
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from . import __version__, config, party_planner
from .mcp_client import McpClient, McpError, OFFICIAL_RATE_LIMIT_PER_MINUTE

# 允许携带 Bearer Token 的接入地址：https + mcd.cn 域（避免 Token 被引导到任意主机）
TRUSTED_ENDPOINT_HOST = "mcd.cn"


def is_trusted_endpoint(endpoint: str) -> bool:
    """判断接入地址是否属于受信范围：必须 https，且主机为 mcd.cn 或其子域。"""
    parsed = urlparse(endpoint)
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    return host == TRUSTED_ENDPOINT_HOST or host.endswith("." + TRUSTED_ENDPOINT_HOST)


def build_client(args: argparse.Namespace) -> McpClient:
    config.load_local_env(os.getcwd())
    endpoint = args.endpoint or os.environ.get("MCD_MCP_ENDPOINT") or ""
    if endpoint and not is_trusted_endpoint(endpoint) \
            and not getattr(args, "allow_custom_endpoint", False):
        raise ValueError(
            f"接入地址 {endpoint} 不在受信范围（https://*.mcd.cn）；"
            "Token 会随请求发往该地址。如确需自定义，请显式加 --allow-custom-endpoint"
        )
    client = McpClient(
        endpoint=args.endpoint,
        rate_limit_per_minute=args.rate_limit,
        timeout=args.timeout,
    )
    # 挂到 args 上，便于 main() 在 finally 中终止会话（不依赖全局变量）
    args.client = client
    return client


def require_token(client: McpClient) -> None:
    if not client.has_token:
        print(
            "错误：未检测到 MCP Token。\n"
            "请先申请麦当劳 MCP Token，并在仓库根目录创建 .env：\n"
            "    MCD_MCP_TOKEN=你的Token\n"
            "（.env 已被 .gitignore 排除，绝不会提交到仓库）",
            file=sys.stderr,
        )
        raise SystemExit(2)


def cmd_version(args: argparse.Namespace) -> int:
    print(f"mcd-party-planner {__version__}")
    return 0


def cmd_selfcheck(args: argparse.Namespace) -> int:
    config.load_local_env(os.getcwd())
    info = config.describe()
    print("=== 环境自检 ===")
    for key, value in info.items():
        print(f"{key}: {value}")
    print("结论：" + ("Token 已就绪，可执行真实调用" if info["MCD_MCP_TOKEN"] == "已配置"
                     else "Token 缺失，需先申请（入口：https://open.mcd.cn/mcp）"))
    return 0


def cmd_tools(args: argparse.Namespace) -> int:
    client = build_client(args)
    require_token(client)
    tools = client.list_tools()
    print(f"=== 服务端工具（{len(tools)} 个）===")
    for tool in tools:
        print(f"- {tool.get('name')}: {(tool.get('description') or '').splitlines()[0][:80]}")
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    client = build_client(args)
    require_token(client)
    snapshot = client.probe()
    out_dir = os.path.join(os.getcwd(), "docs")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "mcp-schema.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(snapshot, handle, ensure_ascii=False, indent=2)
    print(f"已导出 {snapshot['toolCount']} 个工具定义 → {path}")
    print("请据此校准 src/mcp_client.py 的 TOOL_ARG_SPEC 与响应解析辅助函数。")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    client = build_client(args)
    require_token(client)
    result = party_planner.plan_group_meal(
        client, city=args.city, keyword=args.keyword, headcount=args.headcount,
        budget=args.budget, store_limit=args.store_limit,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(party_planner.render_group_meal_report(result))
    return 0


def cmd_party(args: argparse.Namespace) -> int:
    client = build_client(args)
    require_token(client)
    result = party_planner.plan_party_booking(
        client, city_name=args.city, spu_id=args.spu_id, date=args.date,
        count=args.headcount, party_type=args.party_type, dry_run=not args.confirm,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(party_planner.render_party_report(result))
    return 0


def cmd_calendar(args: argparse.Namespace) -> int:
    client = build_client(args)
    require_token(client)
    result = party_planner.suggest_calendar_plan(
        client, city=args.city, keyword=args.keyword, store_limit=args.store_limit,
        specified_date=args.date,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(party_planner.render_calendar_report(result))
    return 0


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--endpoint", default=None, help="MCP 接入地址，默认 https://mcp.mcd.cn")
    parser.add_argument("--rate-limit", type=int, default=300,
                        help=f"每分钟请求上限，默认 300（官方上限 {OFFICIAL_RATE_LIMIT_PER_MINUTE}）")
    parser.add_argument("--timeout", type=float, default=15.0, help="单次请求超时秒数")
    parser.add_argument("--verbose", action="store_true",
                        help="在 stderr 输出调用统计（次数 / 重试 / 限流等待 / 耗时）")
    parser.add_argument("--allow-custom-endpoint", action="store_true",
                        help="允许把 Token 发往非 https://*.mcd.cn 的地址（仅调试用）")


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcd-party-planner",
        description="基于麦当劳 MCP 的聚会 / 团餐组织助手",
    )
    # 与 `version` 子命令等价，便于 `mcd-party-planner --version` 直接查看
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("version", help="显示版本号")
    p.set_defaults(func=cmd_version)

    p = sub.add_parser("selfcheck", help="环境自检")
    p.set_defaults(func=cmd_selfcheck)

    p = sub.add_parser("tools", help="列出服务端工具")
    add_common(p)
    p.set_defaults(func=cmd_tools)

    p = sub.add_parser("probe", help="导出工具定义用于校准入参")
    add_common(p)
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("plan", help="场景 1：团餐方案生成")
    p.add_argument("--city", required=True, help="城市名，如 成都")
    p.add_argument("--keyword", required=True, help="门店关键词，如 春熙路")
    p.add_argument("--headcount", type=int, required=True, help="人数")
    p.add_argument("--budget", type=float, required=True, help="总预算（元）")
    p.add_argument("--store-limit", type=int, default=3, help="最多比对的门店数")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    add_common(p)
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("party", help="场景 2：主题活动预约（默认不下单）")
    p.add_argument("--city", required=True, help="城市名，如 成都市")
    p.add_argument("--spu-id", type=int, default=None, help="主题商品 spuId，缺省自动发现派对类")
    p.add_argument("--date", default=None, help="指定预约日期，格式 2026-10-20")
    p.add_argument("--headcount", type=int, default=5, help="参与人数")
    p.add_argument("--party-type", type=int, default=1, help="派对类型：1-包场，2-拼团")
    p.add_argument("--confirm", action="store_true", help="真实下单；不加则为 dry_run 演示")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    add_common(p)
    p.set_defaults(func=cmd_party)

    p = sub.add_parser("calendar", help="场景 3：活动日历组织建议")
    p.add_argument("--city", required=True, help="城市名，如 成都")
    p.add_argument("--keyword", required=True, help="门店关键词，如 春熙路")
    p.add_argument("--store-limit", type=int, default=3, help="参考门店数")
    p.add_argument("--date", default=None, help="锚点日期，格式 2026-10-16；默认当月")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    add_common(p)
    p.set_defaults(func=cmd_calendar)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = make_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except ValueError as exc:
        # 入参校验失败（如人数 <= 0）：给一行提示 + 退出码 2，不抛堆栈
        print(f"参数错误：{exc}", file=sys.stderr)
        return 2
    except McpError as exc:
        print(f"MCP 调用失败：{exc}", file=sys.stderr)
        return 1
    finally:
        client = getattr(args, "client", None)
        if client is not None:
            if getattr(args, "verbose", False):
                print(_format_stats(getattr(client, "stats", {})), file=sys.stderr)
            closer = getattr(client, "close", None)
            if callable(closer):
                closer()


def _format_stats(stats: Dict[str, int]) -> str:
    """调用统计单行格式，供 `--verbose` 输出（只写 stderr，不污染 stdout）。"""
    return (f"calls={stats.get('calls', 0)} retries={stats.get('retries', 0)} "
            f"rateLimitWaits={stats.get('rateLimitWaits', 0)} "
            f"elapsedMs={stats.get('elapsedMs', 0)}")


if __name__ == "__main__":
    raise SystemExit(main())
