"""取数结果落盘缓存。

行情、财报、概况这类数据在同一天内不会变化，而本项目天然会对同一标的反复研究
（跨轮记忆就是为此设计的）。缓存按 (标的, 数据项, 日期) 落盘，
同一标的当天再次研究直接命中，不再重复打 akshare。

缓存只读不删：需要强制刷新时手工删掉 cache 目录即可。
"""

from __future__ import annotations

import functools
import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

# 缓存目录：<项目根>/cache
CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"


def cached(kind: str) -> Callable:
    """装饰器：把取数结果按 (缓存键, 数据项, 日期) 落盘缓存。

    kind 为该数据项的短名；被装饰函数的第一个位置参数用作缓存键，
    通常是标的代码；无参的全局取数（如代码名称对照表）默认键为 "all"。
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(code: str = "all", *args, **kwargs) -> dict:
            path = CACHE_DIR / f"{code}_{kind}_{datetime.now():%Y%m%d}.json"
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
            payload = func(code, *args, **kwargs)
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return payload

        return wrapper

    return decorator