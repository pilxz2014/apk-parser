# -*- coding: utf-8 -*-
"""长任务与 SSE 事件流。

安装、拉包、录屏这类操作是"有过程"的 —— 前端需要看到进度而不是转圈。
每个任务一个 `queue.Queue`，SSE handler 阻塞读；任务结束推 `event: end`。

同时保留事件重放：客户端晚连上也能补齐之前发生的事（刷新页面不丢进度）。
"""

import json
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

MAX_EVENTS = 500
KEEP_TASKS = 60


@dataclass
class Task:
    """一个长任务。"""

    id: str = ""
    kind: str = ""
    title: str = ""
    state: str = "running"          # running | done | error
    created: float = field(default_factory=time.time)
    finished: float = 0.0
    result: Optional[dict] = None
    error: str = ""
    events: List[dict] = field(default_factory=list)
    subscribers: List[queue.Queue] = field(default_factory=list)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def _push(self, event: str, data: Any) -> None:
        payload = {"event": event, "data": data, "ts": time.time()}
        with self._lock:
            self.events.append(payload)
            if len(self.events) > MAX_EVENTS:
                self.events = self.events[-MAX_EVENTS:]
            subs = list(self.subscribers)
        for q in subs:
            try:
                q.put_nowait(payload)
            except Exception:
                pass

    # -- 便捷方法 ----------------------------------------------------

    def log(self, msg: str, level: str = "info") -> None:
        self._push("log", {"level": level, "msg": msg})

    def progress(self, stage: str, msg: str, pct: Optional[int] = None) -> None:
        self._push("progress", {"stage": stage, "msg": msg, "pct": pct})

    def info(self, data: dict) -> None:
        self._push("info", data)

    def finish(self, result: dict, ok: bool = True) -> None:
        with self._lock:
            self.state = "done" if ok else "error"
            self.finished = time.time()
            self.result = result
            if not ok:
                self.error = result.get("detail") or result.get("error") or "任务失败"
        self._push("result", result)
        self._push("end", {"state": self.state})

    def fail(self, message: str, result: Optional[dict] = None) -> None:
        with self._lock:
            self.state = "error"
            self.finished = time.time()
            self.error = message
            self.result = result or {"ok": False, "detail": message}
        self._push("error", {"msg": message})
        self._push("end", {"state": self.state})

    # -- 订阅 --------------------------------------------------------

    def subscribe(self, replay: bool = True) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            if replay:
                for ev in self.events:
                    try:
                        q.put_nowait(ev)
                    except Exception:
                        pass
            if self.state != "running":
                try:
                    q.put_nowait({"event": "end", "data": {"state": self.state},
                                  "ts": time.time()})
                except Exception:
                    pass
            else:
                self.subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "title": self.title,
            "state": self.state, "created": self.created, "finished": self.finished,
            "error": self.error, "result": self.result,
            "elapsed": round((self.finished or time.time()) - self.created, 2),
        }


class TaskManager:
    """进程内任务表。"""

    def __init__(self):
        self._tasks: Dict[str, Task] = {}
        self._order: List[str] = []
        self._lock = threading.RLock()

    def create(self, kind: str, title: str = "") -> Task:
        t = Task(id=uuid.uuid4().hex[:12], kind=kind, title=title or kind)
        with self._lock:
            self._tasks[t.id] = t
            self._order.insert(0, t.id)
            # 回收旧任务
            while len(self._order) > KEEP_TASKS:
                old = self._order.pop()
                if old != t.id:
                    self._tasks.pop(old, None)
        return t

    def get(self, tid: str) -> Optional[Task]:
        with self._lock:
            return self._tasks.get(tid)

    def list(self, limit: int = 20) -> List[dict]:
        with self._lock:
            return [self._tasks[i].to_dict() for i in self._order[:limit]
                    if i in self._tasks]


tasks = TaskManager()


# ---------------------------------------------------------------- SSE 辅助

def sse_format(event: str, data: Any) -> bytes:
    """SSE 帧。data 里换行要拆成多行 data: 否则会被截断。"""
    payload = json.dumps(data, ensure_ascii=False)
    lines = payload.split("\n")
    body = "".join("data: %s\n" % l for l in lines)
    return ("event: %s\n%s\n" % (event, body)).encode("utf-8")


def run_in_thread(fn, *args, **kwargs) -> threading.Thread:
    """把耗时工作丢到后台线程（HTTP handler 要尽快返回 taskId）。"""
    t = threading.Thread(target=_safe_run, args=(fn, args, kwargs), daemon=True)
    t.start()
    return t


def _safe_run(fn, args, kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except Exception:
        import traceback
        traceback.print_exc()
