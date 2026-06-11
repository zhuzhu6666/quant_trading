#!/usr/bin/env python
"""一键启动：后端 FastAPI :8000 + 前端 Vite :5173。
用法:
    python start-all.py          # 启动后端+前端
    python start-all.py --prod   # 生产模式 (单端口 :8000)
"""
import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND_PORT = 8000
FRONTEND_PORT = 5173


def check_port(host: str, port: int) -> bool:
    """粗略检查端口是否被占."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
        s.close()
        return False  # 没人占用
    except OSError:
        return True   # 已被占


def main():
    parser = argparse.ArgumentParser(description="Quant Trading — 一键启动")
    parser.add_argument("--prod", action="store_true", help="生产模式 (build + 单端口 uvicorn)")
    parser.add_argument("--backend-port", type=int, default=BACKEND_PORT)
    parser.add_argument("--frontend-port", type=int, default=FRONTEND_PORT)
    parser.add_argument("--refresh-data", action="store_true",
                        help="启动前先刷新外部数据 (COT/Events/ETF)")
    args = parser.parse_args()

    os.chdir(ROOT)

    # ── 外部数据刷新 (可选) ──
    if args.refresh_data:
        print(":: 检查外部数据时效性 ...")
        refresh_script = ROOT / "scripts/refresh_external_data.py"
        if refresh_script.exists():
            status_code = subprocess.call([sys.executable or "python", str(refresh_script), "--once"])
            if status_code != 0:
                print("⚠ 外部数据部分过期, 不影响启动")
        else:
            print("  (scripts/refresh_external_data.py 未找到, skip)")

    # ── 选 Python ──
    for exe in ["python", "python3", sys.executable]:
        try:
            subprocess.run([exe, "-c", "import uvicorn"], capture_output=True, check=True)
            PY = exe
            break
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
    else:
        print("✗ 找不到带 uvicorn 的 Python")
        sys.exit(1)

    # ── 选 npm ──
    NPM = "npx" if os.name == "nt" else "npx"

    if args.prod:
        # ── 生产模式 ──
        print(":: 生产模式 — 构建前端并启动 uvicorn (单端口 :8000) ...")
        subprocess.run([NPM, "run", "build"], cwd=ROOT / "frontend-v2", shell=True, check=True)
        # build 产物在 frontend-v2/dist/ → backend/static/ 由 app.py 自动挂载
        os.makedirs(ROOT / "backend/static", exist_ok=True)
        for item in (ROOT / "frontend-v2/dist").iterdir():
            dest = ROOT / "backend/static" / item.name
            if item.is_dir():
                import shutil
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest)
        print(f":: 前端构建完成, 启动 uvicorn :{args.backend_port} ...")
        subprocess.run([PY, "-m", "uvicorn", "backend.app:app",
                        "--host", "0.0.0.0", "--port", str(args.backend_port)])
        return

    # ── 开发模式 ──
    if check_port("0.0.0.0", args.backend_port):
        print(f"✗ 端口 {args.backend_port} 已被占用")
        sys.exit(1)

    print(f":: 启动后端 (uvicorn :{args.backend_port}) ...")
    be = subprocess.Popen(
        [PY, "-m", "uvicorn", "backend.app:app",
         "--host", "0.0.0.0", "--port", str(args.backend_port),
         "--log-level", "info"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )

    # 等后端就绪 (最长 15s)
    ready = False
    t0 = time.time()
    for line in iter(be.stdout.readline, ""):
        print(line, end="")
        if "Application startup complete" in line:
            ready = True
            break
        if time.time() - t0 > 15:
            break
    if not ready:
        print("✗ 后端启动超时")
        be.kill()
        sys.exit(1)

    print(f"\n:: 后端就绪 ✓  启动前端 (Vite :{args.frontend_port}) ...")
    fe = subprocess.Popen(
        [NPM, "vite", "--port", str(args.frontend_port)],
        cwd=ROOT / "frontend-v2",
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
        shell=True,
    )

    print(f"\n╔══════════════════════════════════════════════╗")
    print(f"║  Quant Trading 已启动                        ║")
    print(f"║  后端: http://localhost:{args.backend_port}      ║")
    print(f"║  前端: http://localhost:{args.frontend_port}      ║")
    print(f"║  按 Ctrl+C 停止全部                          ║")
    print(f"╚══════════════════════════════════════════════╝\n")

    try:
        for line in iter(fe.stdout.readline, ""):
            print(line, end="")
    except KeyboardInterrupt:
        print("\n:: 正在停止 ...")
    finally:
        be.kill()
        fe.kill()
        print(":: 已停止")


if __name__ == "__main__":
    main()
