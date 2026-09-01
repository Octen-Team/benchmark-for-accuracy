"""fetch 评测线的落盘与进度：两个小工具，刻意不依赖别的模块。

`append` 单条 flush + fsync 落盘 —— 判定是这条流水线里最贵的一步，进程中途被杀时
已判完的格子不能跟着重来。加锁是因为多线程共写一个文件。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from threading import Lock

WRITE_LOCK = Lock()


def append(path: Path, rec: dict) -> None:
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    with WRITE_LOCK:
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())


def progress(done: int, total: int, t0: float, *, every: int = 10) -> str | None:
    """进度行。返回 None 表示这次不该打印：`if (s := progress(...)): print(s)`。

    长跑必须能观察到进度，否则真正的错误会被挤出视野。
    """
    if done % every and done != total:
        return None
    el = time.time() - t0
    eta = el / max(1, done) * (total - done)
    return f"  {done}/{total}  已用 {el/60:.1f} 分  预计还需 {eta/60:.1f} 分"
