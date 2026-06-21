#!/usr/bin/env python
"""
一键迁移项目到远程 Ubuntu 服务器，并通过 Docker 部署。

用法:
  python deploy_to_server.py ubuntu@你的服务器IP

前提:
  1. 远程服务器已安装 Docker + docker-compose
  2. 本机能 ssh 到远程服务器（推荐 ssh-keygen + ssh-copy-id）
  3. 远程服务器有至少 5GB 磁盘剩余

流程:
  1. rsync 项目代码 + data 目录到远程 ~/quant_trading/
  2. 远程执行 docker-compose up --build -d
  3. 验证后端健康检查
"""

import subprocess
import sys
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent

REMOTE_PATH = "~/quant_trading"
EXCLUDE = [
    "--exclude", ".git",
    "--exclude", "__pycache__",
    "--exclude", "*.pyc",
    "--exclude", ".pytest_cache",
    "--exclude", ".venv",
    "--exclude", "venv",
    "--exclude", "*.duckdb.wal",
    "--exclude", "*.duckdb.tmp",
    "--exclude", "data/dukascopy_raw", # tick 原始数据很大, 可以首次先不传
]


def main():
    if len(sys.argv) < 2:
        print("用法: python deploy_to_server.py user@server-ip")
        print("示例: python deploy_to_server.py ubuntu@192.168.1.100")
        sys.exit(1)

    host = sys.argv[1]
    print(f"目标服务器: {host}")
    print(f"本地项目:   {ROOT}")
    print()

    # Step 1: rsync 代码和数据
    print("=" * 60)
    print("Step 1/3: rsync 项目到远程服务器...")
    rsync_cmd = [
        "rsync", "-avz", "--progress",
        *EXCLUDE,
        str(ROOT) + "/",
        f"{host}:{REMOTE_PATH}/",
    ]
    print(" ".join(rsync_cmd))
    result = subprocess.run(rsync_cmd, cwd=str(ROOT))
    if result.returncode != 0:
        print(f"rsync 失败 (exit={result.returncode})")
        sys.exit(1)
    print("rsync 完成")
    print()

    # Step 2: 远程 build + 启动 Docker
    print("=" * 60)
    print("Step 2/3: 远程 Docker build + up...")
    docker_cmd = [
        "ssh", host,
        f"cd {REMOTE_PATH} && docker compose build --no-cache && docker compose up -d",
    ]
    print(" ".join(docker_cmd))
    result = subprocess.run(docker_cmd)
    if result.returncode != 0:
        print(f"Docker 部署失败 (exit={result.returncode})")
        print("可能是 Docker 未安装? 在服务器上执行:")
        print("  sudo apt install docker.io docker-compose-v2")
        sys.exit(1)
    print("Docker 启动完成")
    print()

    # Step 3: 等待健康检查
    print("=" * 60)
    print("Step 3/3: 等待后端就绪 (最多 60 秒)...")
    import time

    for i in range(12):
        time.sleep(5)
        check_cmd = [
            "ssh", host,
            f"curl -s -o /dev/null -w '%{{http_code}}' http://localhost:8000/api/health",
        ]
        try:
            output = subprocess.check_output(check_cmd, stderr=subprocess.STDOUT).decode().strip()
            if output == "200":
                ip = host.split("@")[-1] if "@" in host else host
                print(f"✅ 后端就绪! http://{ip}:8000")
                print()
                print("下一步:")
                print(f"  1. 编辑 miniprogram/utils/config.js 中的 SERVER = 'http://{ip}:8000'")
                print(f"  2. 在微信开发者工具中预览小程序")
                print(f"  3. POST /api/live/start 启动实盘 loop (先 POST /api/auth/login)")
                return
            else:
                print(f"  等待中... (HTTP {output})")
        except subprocess.CalledProcessError:
            print(f"  等待中... (连接失败)")

    print("⚠️  超时, 请手动检查:")
    print(f"  ssh {host} 'docker logs quant-backend --tail 50'")


if __name__ == "__main__":
    main()
