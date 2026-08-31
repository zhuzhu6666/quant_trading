"""标准库重试装饰器，兼容 tenacity 调用形态。"""
from __future__ import annotations
import time, functools
from typing import Any, Callable

class TransientError(Exception): pass
class _Stop:
    def __init__(self, n:int): self.max_attempts=int(n)
def stop_after_attempt(n:int): return _Stop(n)
class _WaitFixed:
    def __init__(self,d:float): self.delay=float(d)
    def __call__(self, attempt:int): return self.delay
def wait_fixed(d:float): return _WaitFixed(d)
class _WaitExp:
    def __init__(self,m=1.0,min=0.0,max=None,base=2.0): self.m=float(m);self.min=float(min);self.max=float(max) if max is not None else None;self.base=float(base)
    def __call__(self,a:int):
        d=self.m*(self.base**(a-1))
        if d<self.min: d=self.min
        if self.max is not None and d>self.max: d=self.max
        return d
def wait_exponential(multiplier=1.0,min=0.0,max=None,exp_base=2.0): return _WaitExp(multiplier,min,max,exp_base)
class _RetryIf:
    def __init__(self, t): self.t=(t,) if not isinstance(t,tuple) else t
    def __call__(self, e): return isinstance(e,self.t)
def retry_if_exception_type(t): return _RetryIf(t)
def retry_if_exception(p): return p
def retry(stop=None,wait=None,retry=None,reraise=True,before_sleep=None,sleep=None,max_attempts=None,delay=None):
    if stop is None and max_attempts is not None: stop=stop_after_attempt(max_attempts)
    if wait is None and delay is not None: wait=wait_fixed(delay)
    if stop is None: stop=stop_after_attempt(3)
    attempts=getattr(stop,'max_attempts',3)
    def _wait(a):
        if wait is None: return 0.0
        if callable(wait): return float(wait(a))
        return 0.0
    def _should(e):
        if retry is None: return True
        try: return bool(retry(e))
        except: return False
    _sleep=sleep or time.sleep
    def dec(fn):
        @functools.wraps(fn)
        def wrap(*a,**kw):
            last=None
            for attempt in range(1,attempts+1):
                try: return fn(*a,**kw)
                except BaseException as exc:
                    last=exc
                    if not _should(exc): raise
                    if attempt>=attempts: break
                    d=_wait(attempt)
                    if before_sleep:
                        try: before_sleep(attempt,exc,d)
                        except: pass
                    if d>0: _sleep(d)
            if reraise and last is not None: raise last
            return None
        return wrap
    return dec
__all__=["TransientError","stop_after_attempt","wait_fixed","wait_exponential","retry_if_exception_type","retry_if_exception","retry"]
