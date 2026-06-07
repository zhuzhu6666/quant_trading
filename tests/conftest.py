"""
tests/conftest.py — pytest 配置,自动把项目根加到 sys.path

框架审计 2026-06-04 修复计划的统一测试入口。
"""
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Ensure subprocesses spawned by tests can also import `backend.*`.
os.environ.setdefault("PYTHONPATH", str(PROJECT_ROOT))
