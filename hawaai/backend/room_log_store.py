from collections import deque
from threading import Lock
import time
from typing import Deque, Dict, List, Optional

DEFAULT_MAX_LINES_PER_ROOM = 500


class RoomLogStore:
    def __init__(self) -> None:
        self._logs: Dict[str, Deque[dict]] = {}
        self._lock = Lock()
        self._max_lines_per_room = DEFAULT_MAX_LINES_PER_ROOM

    def set_max_lines_per_room(self, value: int) -> None:
        cap = max(50, min(int(value), 2000))
        with self._lock:
            if cap == self._max_lines_per_room:
                return
            self._max_lines_per_room = cap
            for rid, dq in list(self._logs.items()):
                self._logs[rid] = deque(dq, maxlen=cap)

    def append(self, room_id: str, message: str, *, level: str = "INFO") -> None:
        rid = (room_id or "").strip().lower()
        if not rid:
            return
        with self._lock:
            if rid not in self._logs:
                self._logs[rid] = deque(maxlen=self._max_lines_per_room)
            self._logs[rid].append(
                {
                    "ts": int(time.time() * 1000),
                    "level": str(level or "INFO").upper(),
                    "message": str(message),
                }
            )

    def get_logs(self, room_id: str, limit: int = 200) -> List[dict]:
        rid = (room_id or "").strip().lower()
        with self._lock:
            cap = max(1, min(int(limit), self._max_lines_per_room))
            if rid not in self._logs:
                return []
            return list(self._logs[rid])[-cap:]

    def latest_key(self, room_id: str) -> Optional[str]:
        rid = (room_id or "").strip().lower()
        with self._lock:
            if rid not in self._logs:
                return None
            if not self._logs[rid]:
                return None
            last = self._logs[rid][-1]
            return f"{last.get('ts')}-{last.get('level')}-{last.get('message')}"

    def clear(self, room_id: str) -> None:
        rid = (room_id or "").strip().lower()
        if not rid:
            return
        with self._lock:
            self._logs.pop(rid, None)


room_log_store = RoomLogStore()
