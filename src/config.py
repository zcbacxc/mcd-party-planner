"""本地配置读取（Token 安全约束的实现细节模块）。

职责：
1. 从环境变量读取 MCP Token 与接入地址；
2. 可选读取仓库根目录下的 `.env`（该文件被 .gitignore 排除，永不入库）；
3. 提供 `describe()` 用于自检输出，且**绝不回显 Token 明文**。
"""

from __future__ import annotations

import os
from typing import Dict, Optional

ENV_FILE_CANDIDATES = (".env",)


def load_local_env(base_dir: Optional[str] = None, override: bool = False) -> Dict[str, str]:
    """读取本地 .env（若存在），返回已加载的键名集合（不含值）。"""
    base_dir = base_dir or os.getcwd()
    loaded: Dict[str, str] = {}
    for filename in ENV_FILE_CANDIDATES:
        path = os.path.join(base_dir, filename)
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if not key:
                    continue
                if override or key not in os.environ:
                    os.environ[key] = value
                loaded[key] = ""
    return loaded


def get_token() -> str:
    return os.environ.get("MCD_MCP_TOKEN", "").strip()


def get_endpoint(default: str = "https://mcp.mcd.cn") -> str:
    return os.environ.get("MCD_MCP_ENDPOINT", default).strip() or default


def describe() -> Dict[str, object]:
    """自检输出：只暴露 Token 是否已配置与长度，不泄露内容。"""
    token = get_token()
    return {
        "MCD_MCP_TOKEN": "已配置" if token else "未配置",
        "tokenLength": len(token),
        "MCD_MCP_ENDPOINT": get_endpoint(),
    }
