"""backend.runtime — FastAPI 进程内运行时底座。

模块:
- runtime_state: 进程内单例,集中所有长生命周期共享状态
- locks: asyncio.Lock 池
- loop_host: 在 FastAPI 进程内管理 paper/sync 长循环
- cli: 统一 CLI 入口
"""

from .loop_host import LoopHost, RunnerFactory
from .locks import LockPool
from .runtime_state import LoopStatus, RuntimeState
from .scheduler import InProcessScheduler, JobInfo

__all__ = ["LoopHost", "RunnerFactory", "LockPool", "LoopStatus", "RuntimeState",
           "InProcessScheduler", "JobInfo"]
