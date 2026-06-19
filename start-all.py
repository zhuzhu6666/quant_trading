#!/usr/bin/env python
"""一键启动：后端 FastAPI :8000 + 前端 Vite :5173。

用法:
    python start-all.py              # 启动后端+前端
    python start-all.py --prod       # 生产模式 (单端口 :8000)
    python start-all.py --minimized  # 启动后隐藏终端 + 自动开浏览器
    python start-all.py --refresh-data  # 启动前刷新外部数据
"""
import argparse
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND_PORT = 8000
FRONTEND_PORT = 5173

# 设置子进程默认编码为 UTF-8 + errors=replace, 防 Windows 终端输出非 UTF-8 字节时抛异常
os.environ.setdefault("PYTHONIOENCODING", "utf-8:replace")
# Windows console encoding fix: prevent OSError on CJK chars
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

logger = logging.getLogger("start-all")
logging.basicConfig(level=logging.INFO, format="%(message)s")

# ── 环境变量 ──
if not os.environ.get("QUANT_JWT_SECRET"):
    import secrets as _sec
    os.environ["QUANT_JWT_SECRET"] = _sec.token_hex(32)
    logger.warning("QUANT_JWT_SECRET 未设置，已生成临时密钥（重启动态变化）")


def _hide_terminal():
    """Windows: 隐藏当前控制台窗口 (仅当有控制台时有效, pythonw 下无效果)。"""
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        hwnd = kernel32.GetConsoleWindow()
        if hwnd:
            user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:
        pass


def _notify_error(title: str, msg: str):
    """Windows: 弹窗显示错误 (pythonw 下也能看到)。"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, msg, title, 0x10)  # MB_ICONERROR
    except Exception:
        pass


def _open_browser(url: str):
    """打开默认浏览器。os.startfile 在 python 和 pythonw 下都可靠。"""
    import os as _os
    try:
        _os.startfile(url)
    except Exception:
        try:
            import subprocess
            subprocess.run(["cmd", "/c", "start", url], shell=True, timeout=5)
        except Exception:
            pass


def _kill_residuals(ports=(8000, 5173)):
    """端口级清理：只杀占用指定端口的进程，不碰其他 Python/node。

    比 `taskkill /im python.exe` 地图炮安全得多，
    不会误杀 Hermes Agent、其他终端或 IDE 进程。
    """
    import re
    try:
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True, text=True, timeout=5,
            errors="replace",  # 处理中文 Windows 本地化输出
        )
        for line in result.stdout.splitlines():
            # 匹配 LISTENING 行，提取端口和 PID
            # 格式:  TCP    0.0.0.0:8000    0.0.0.0:0    LISTENING    1234
            m = re.search(r"TCP\s+\S+:(\d+)\s+\S+\s+LISTENING\s+(\d+)", line)
            if not m:
                continue
            port, pid = int(m.group(1)), m.group(2)
            if port not in ports:
                continue
            subprocess.run(
                ["taskkill", "/f", "/pid", pid],
                capture_output=True,
            )
    except Exception:
        pass


def check_port(host: str, port: int) -> bool:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
        s.close()
        return False
    except OSError:
        return True


def _find_python() -> str:
    """优先找 Hermes venv Python，再 fallback。"""
    candidates = [
        r"C:\Users\zhu\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe",
        sys.executable,
        "python",
        "python3",
    ]
    for exe in candidates:
        if not exe:
            continue
        try:
            subprocess.run([exe, "-c", "import uvicorn"], capture_output=True, check=True)
            return exe
        except (subprocess.CalledProcessError, FileNotFoundError):
            continue
    print("找不到带 uvicorn 的 Python")
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Quant Trading — 一键启动")
    parser.add_argument("--prod", action="store_true", help="生产模式 (build + 单端口 uvicorn)")
    parser.add_argument("--minimized", action="store_true", help="启动后隐藏终端 + 自动开浏览器")
    parser.add_argument("--backend-port", type=int, default=BACKEND_PORT)
    parser.add_argument("--frontend-port", type=int, default=FRONTEND_PORT)
    parser.add_argument("--refresh-data", action="store_true",
                        help="启动前先刷新外部数据 (COT/Events/ETF)")
    args = parser.parse_args()
    os.chdir(ROOT)

    # ── 清理残留 ──
    _kill_residuals(ports=(args.backend_port, args.frontend_port))

    # ── 外部数据刷新 (可选) ──
    if args.refresh_data:
        print(":: 检查外部数据时效性 ...")
        refresh_script = ROOT / "scripts/refresh_external_data.py"
        if refresh_script.exists():
            status_code = subprocess.call([sys.executable or "python", str(refresh_script), "--once"])
            if status_code != 0:
                print("(外部数据部分过期, 不影响启动)")
        else:
            print("  (scripts/refresh_external_data.py 未找到, skip)")

    # ── 选 Python ──
    PY = _find_python()
    NPM = "npx"

    if args.prod:
        print(":: 生产模式 — 构建前端并启动 uvicorn (单端口 :8000) ...")
        # 前端构建 (npx run build 会挂死, 显式用 npm)
        build_result = subprocess.run(
            ["npm", "run", "build"],
            cwd=ROOT / "frontend-v2",
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if build_result.returncode != 0:
            print("前端构建失败:")
            print(build_result.stderr[-2000:] if build_result.stderr else "无错误输出")
            sys.exit(1)

        # 复制到 backend/static
        os.makedirs(ROOT / "backend/static", exist_ok=True)
        import shutil
        for item in (ROOT / "frontend-v2/dist").iterdir():
            dest = ROOT / "backend/static" / item.name
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest)
        print("前端构建完成, 启动 uvicorn ...")

        # 启动 uvicorn
        be = subprocess.Popen(
            [PY, "-m", "uvicorn", "backend.app:app",
             "--host", "0.0.0.0", "--port", str(args.backend_port),
             "--log-level", "info"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

        # 轮询 health
        import urllib.request, urllib.error
        ready = False
        t0 = time.time()
        while time.time() - t0 < 30:
            try:
                r = urllib.request.urlopen(f"http://localhost:{args.backend_port}/api/health", timeout=1)
                if r.status == 200:
                    ready = True
                    break
            except Exception:
                pass
            time.sleep(0.5)
        if not ready:
            print("后端启动超时")
            be.kill()
            sys.exit(1)

        print(":: 后端就绪")
        if args.minimized:
            _hide_terminal()
        # 浏览器由 start.bat 负责打开, 避免重复
        print("  浏览器将由启动脚本打开")
        print("  按 Ctrl+C 停止")
        try:
            be.wait()
        except KeyboardInterrupt:
            print("\n:: stopping ...")
        finally:
            be.kill()
            print(":: stopped")
        return

    # ── 开发模式 ──
    if check_port("0.0.0.0", args.backend_port):
        print(f"端口 {args.backend_port} 已被占用")
        sys.exit(1)

    print(f":: 启动后端 (uvicorn :{args.backend_port}) ...")
    be = subprocess.Popen(
        [PY, "-m", "uvicorn", "backend.app:app",
         "--host", "0.0.0.0", "--port", str(args.backend_port),
         "--log-level", "warning"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    # 轮询 health endpoint (最长 20s)
    import urllib.request, urllib.error
    ready = False
    t0 = time.time()
    while time.time() - t0 < 20:
        try:
            r = urllib.request.urlopen(f"http://localhost:{args.backend_port}/api/health", timeout=2)
            if r.status == 200:
                ready = True
                break
        except (urllib.error.URLError, ConnectionRefusedError, ConnectionResetError):
            pass
        time.sleep(0.5)
    if not ready:
        print("后端启动超时")
        be.kill()
        sys.exit(1)

    print(f"后端就绪, 启动前端 (Vite :{args.frontend_port}) ...")

    if args.minimized:
        _hide_terminal()

    fe = subprocess.Popen(
        [NPM, "vite", "--port", str(args.frontend_port)],
        cwd=ROOT / "frontend-v2",
        shell=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0,
    )

    print("")
    print("  Quant Trading v4.1")
    print(f"  后端: http://localhost:{args.backend_port}")
    print(f"  前端: http://localhost:{args.frontend_port}")
    print("  面板: 交易 · 因子 · 风控 · 运维 · 回测 · 数据 · 系统")
    print("  新接口: /api/risk /api/ops /api/experiments")

    if args.minimized:
        _open_browser(f"http://localhost:{args.frontend_port}")
        print("  (终端已隐藏, 关闭浏览器退出)")
    else:
        print("  按 Ctrl+C 停止全部")
    print("")

    try:
        fe.wait()
    except KeyboardInterrupt:
        print("\n:: stopping ...")
    finally:
        be.kill()
        fe.kill()
        try:
            print(":: stopped")
        except OSError:
            pass  # Windows 终端已关闭时 print 可能抛 [Errno 22]


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        pass
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        with open(ROOT / "start-error.log", "w") as f:
            f.write(tb)
        _notify_error("启动失败", f"{e}\n\n详情见 start-error.log")
