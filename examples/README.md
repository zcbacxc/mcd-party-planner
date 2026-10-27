# examples —— 真实调用录制快照

这里存放的是**一次真实 MCP 调用**的完整往返记录（请求 + 响应），用于：

1. **没有 Token 的人也能看到真实效果**——回放不发任何网络请求；
2. **离线回归**——CI 在无 Token 环境下跑一遍，防止「离线路径」悄悄腐化；
3. **可复核**——快照是可读 JSON，任何人都能核对报告里的数字确实来自服务端，而不是编的。

## ⚠️ 这些是快照，不是实时数据

- 录制时间见文件内 `recordedAt`；麦当劳的菜单、价格、活动与场次**按日轮换**，
  回放得到的永远是**录制那一刻**的结果，与今天实时查询可能不同。
- 项目本身不含任何静态菜单或价格表，业务代码也不读取这些文件——
  只有显式加 `--replay` 时才会用到它们。

## 用法

```bash
# 离线看报告（不需要 .env，不需要 Token）
python -m src.cli plan --city 成都 --keyword 春熙路 --headcount 8 --budget 400 \
  --replay examples/2026-10-27-chengdu-8p-400-plan.json

# 生成可分享的单文件 HTML
python -m src.cli party --city 成都市 --headcount 5 \
  --replay examples/2026-10-27-chengdu-party.json --format html > party.html
```

回放时传入的 `--city` / `--headcount` 等参数**只用于业务层计算**，
不会改变快照里的响应——所以同一份快照可以配不同人数 / 预算反复查看报告形态。

## 录制自己的快照

任意命令加 `--record <path>`：

```bash
python -m src.cli plan --city 成都 --keyword 春熙路 --headcount 8 --budget 400 \
  --record examples/my-snapshot.json
```

录制文件会随每次请求增量落盘（中途失败也能留下已完成的部分）。
响应头只保留 `Mcp-Session-Id` 与 `Content-Type`，服务端的请求追踪标识不会进入仓库。

## 文件清单

| 文件 | 场景 | 录制参数 | 调用数 |
|---|---|---|---:|
| `2026-10-27-chengdu-8p-400-plan.json` | 团餐方案 | 成都 / 春熙路 / 8 人 / ¥400 | 26 |
| `2026-10-27-chengdu-party.json` | 主题活动预约（dry_run） | 成都市 / 5 人 | 8 |
| `2026-10-27-chengdu-calendar.json` | 活动日历建议 | 成都 / 春熙路 | 9 |

> 快照合计约 770 KB，主要为服务端返回的完整菜单与活动数据（`query-meals` 单次可达 55 KB）。
> 如需压缩体积，可用 `--store-limit 1` 录制更小的样本。
