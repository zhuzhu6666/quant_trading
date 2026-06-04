"""
execution/_env.py — 自动从 .env 加载环境变量 (轻量 dotenv, 无依赖)

解决 PowerShell / bash / zsh 跨 shell 兼容问题.
用户不需手动 export, Python 启动时调 load_env() 即可.

用法:
  from execution._env import load_env
  load_env()  # 自动从项目根 .env 读 CTRADER_* 灌到 os.environ
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# 项目根 = 当前文件父目录的父目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"


def load_env(env_file: Path | str = None, prefix: str = "CTRADER_",
             override: bool = False) -> int:
    """
    读 .env, 灌到 os.environ.

    Args:
        env_file: 默认 .env (项目根)
        prefix: 只灌指定前缀的 key, 默认 'CTRADER_' (跟 broker 凭证相关)
        override: 已存在 env 变量是否覆盖, 默认 False (命令行 export 优先)

    Returns:
        成功灌入的 key 数
    """
    env_file = Path(env_file or ENV_FILE)
    if not env_file.exists():
        logger.debug(f".env not found at {env_file}; skip")
        return 0
    count = 0
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")  # 去引号
        if prefix and not k.startswith(prefix):
            continue
        if k in os.environ and not override:
            continue  # 不覆盖现有 env
        os.environ[k] = v
        count += 1
    if count:
        logger.debug(f"load_env: loaded {count} new keys from {env_file}")
    else:
        logger.debug(f"load_env: no new keys from {env_file} "
                    f"(already in os.environ or empty)")
    return count


# 便利: 主动调一次, 这样 import _env 就生效
load_env()
