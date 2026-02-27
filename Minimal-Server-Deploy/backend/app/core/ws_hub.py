import asyncio
from collections import defaultdict
from typing import Dict, Set, Any

from fastapi import WebSocket


class WSHub:
    def __init__(self):
        self._log_connections: Set[WebSocket] = set()
        self._notify_connections: Dict[str, Set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def register(self, websocket: WebSocket, channel: str = "logs", device_id: str = "") -> None:
        await websocket.accept()
        async with self._lock:
            if channel == "notify" and device_id:
                self._notify_connections[device_id].add(websocket)
            else:
                self._log_connections.add(websocket)

    async def unregister(self, websocket: WebSocket, channel: str = "logs", device_id: str = "") -> None:
        async with self._lock:
            if channel == "notify" and device_id:
                conns = self._notify_connections.get(device_id)
                if conns and websocket in conns:
                    conns.remove(websocket)
                if conns is not None and len(conns) == 0:
                    self._notify_connections.pop(device_id, None)
            else:
                self._log_connections.discard(websocket)

    async def broadcast_log(self, message: str) -> int:
        async with self._lock:
            targets = list(self._log_connections)
        if not targets:
            return 0
        dead: Set[WebSocket] = set()
        sent = 0
        for ws in targets:
            try:
                await ws.send_text(str(message))
                sent += 1
            except Exception:
                dead.add(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._log_connections.discard(ws)
        return sent

    async def push_device_event(self, device_id: str, payload: Any) -> int:
        if not device_id:
            return 0
        async with self._lock:
            targets = list(self._notify_connections.get(device_id, set()))
        if not targets:
            return 0

        dead: Set[WebSocket] = set()
        sent = 0
        for ws in targets:
            try:
                await ws.send_json(payload)
                sent += 1
            except Exception:
                dead.add(ws)

        if dead:
            async with self._lock:
                conns = self._notify_connections.get(device_id, set())
                for ws in dead:
                    conns.discard(ws)
                if len(conns) == 0 and device_id in self._notify_connections:
                    self._notify_connections.pop(device_id, None)
        return sent


ws_hub = WSHub()

