from collections import deque
from threading import Lock
import time
from typing import Deque, Dict, List, Optional

DEFAULT_MAX_LINES_PER_ROOM = 500

LOG_SCOPE_RUNTIME = "runtime"
LOG_SCOPE_SYSTEM = "system"
LOG_SCOPE_CONFIG = "config"
LOG_SCOPE_HTTP = "http"
LOG_SCOPE_DIAGNOSTIC = "diagnostic"

_LOG_SCOPES = {
    LOG_SCOPE_RUNTIME,
    LOG_SCOPE_SYSTEM,
    LOG_SCOPE_CONFIG,
    LOG_SCOPE_HTTP,
    LOG_SCOPE_DIAGNOSTIC,
}


def normalize_log_scope(scope: object) -> str:
    value = str(scope or LOG_SCOPE_RUNTIME).strip().lower()
    return value if value in _LOG_SCOPES else LOG_SCOPE_DIAGNOSTIC


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

    def append(
        self,
        room_id: str,
        message: str,
        *,
        level: str = "INFO",
        scope: str = LOG_SCOPE_RUNTIME,
    ) -> None:
        rid = (room_id or "").strip().lower()
        if not rid:
            return
        log_scope = normalize_log_scope(scope)
        with self._lock:
            if rid not in self._logs:
                self._logs[rid] = deque(maxlen=self._max_lines_per_room)
            self._logs[rid].append(
                {
                    "ts": int(time.time() * 1000),
                    "room_id": rid,
                    "scope": log_scope,
                    "level": str(level or "INFO").upper(),
                    "message": str(message),
                }
            )

    def get_logs(
        self,
        room_id: str,
        limit: int = 200,
        *,
        scope: Optional[str] = LOG_SCOPE_RUNTIME,
    ) -> List[dict]:
        rid = (room_id or "").strip().lower()
        with self._lock:
            cap = max(1, min(int(limit), self._max_lines_per_room))
            if rid not in self._logs:
                return []
            logs = list(self._logs[rid])
            if scope is not None:
                log_scope = normalize_log_scope(scope)
                logs = [
                    entry
                    for entry in logs
                    if (entry.get("scope") or LOG_SCOPE_RUNTIME) == log_scope
                ]
            return logs[-cap:]

    def latest_key(
        self,
        room_id: str,
        *,
        scope: Optional[str] = LOG_SCOPE_RUNTIME,
    ) -> Optional[str]:
        rid = (room_id or "").strip().lower()
        with self._lock:
            if rid not in self._logs:
                return None
            logs = list(self._logs[rid])
            if scope is not None:
                log_scope = normalize_log_scope(scope)
                logs = [
                    entry
                    for entry in logs
                    if (entry.get("scope") or LOG_SCOPE_RUNTIME) == log_scope
                ]
            if not logs:
                return None
            last = logs[-1]
            scope_value = last.get("scope") or LOG_SCOPE_RUNTIME
            return (
                f"{last.get('ts')}-{scope_value}-"
                f"{last.get('level')}-{last.get('message')}"
            )

    def clear(self, room_id: str) -> None:
        rid = (room_id or "").strip().lower()
        if not rid:
            return
        with self._lock:
            self._logs.pop(rid, None)


room_log_store = RoomLogStore()
