#!/usr/bin/env python
"""打包数据文件 + docker-compose 配置，方便上传到云服务器。

用法:
    python scripts/pack_for_cloud.py

输出:
    cloud_deploy/ 目录，包含:
      - docker-compose.yml
      - .env.template
      - data/ (DuckDB 库文件)
"""
import shutil
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "cloud_deploy"

def main():
    OUT.mkdir(parents=True, exist_ok=True)

    # ── 打包必要的数据文件 ──
    data_files = [
        "bars.duckdb",
        "external_data.duckdb",
        "ticks.duckdb",
        "l2.duckdb",
    ]
    data_dir = OUT / "data"
    data_dir.mkdir(exist_ok=True)

    for f in data_files:
        src = ROOT / "data" / f
        if src.exists():
            size_mb = src.stat().st_size / (1024 * 1024)
            dst = data_dir / f
            shutil.copy2(src, dst)
            print(f"  data/{f}: {size_mb:.0f} MB")
        else:
            print(f"  data/{f}: (not found, will init on first start)")

    # ── 复制 docker-compose ──
    shutil.copy2(ROOT / "docker-compose.yml", OUT / "docker-compose.yml")
    print(f"  docker-compose.yml")

    # ── 生成 .env 模板 ──
    env_template = """# ── 云部署环境变量 ──
# 填入你的 cTrader 凭证
CTRADER_CLIENT_ID=your_client_id
CTRADER_CLIENT_SECRET=your_client_secret
CTRADER_ACCESS_TOKEN=your_access_token
CTRADER_ACCOUNT_ID=your_account_id

# JWT 密钥 (生产环境必须改)
QUANT_JWT_SECRET=change-me-to-a-random-secret

# CORS (可选, 默认 localhost)
# QUANT_CORS_ALLOWED_ORIGINS=http://your-domain.com
"""
    (OUT / ".env").write_text(env_template)
    print(f"  .env")

    # ── 打包成 tar.gz ──
    tar_path = ROOT / "cloud_deploy.tar.gz"
    with tarfile.open(str(tar_path), "w:gz") as tar:
        for f in OUT.rglob("*"):
            tar.add(str(f), arcname=str(f.relative_to(OUT)))
    print(f"\n✅ 打包完成: {tar_path}")
    print(f"   大小: {tar_path.stat().st_size / (1024*1024):.0f} MB")
    print(f"\n上传到云服务器后:")
    print(f"  tar xzf cloud_deploy.tar.gz")
    print(f"  cd cloud_deploy")
    print(f"  # 编辑 .env 填入凭证")
    print(f"  docker compose up -d")


if __name__ == "__main__":
    main()
