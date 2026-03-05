import asyncio
from collections import defaultdict
from typing import Dict, Set, Any

from fastapi import WebSocket


class WSHub:
    def __init__(self):
        self._log_connections: Set[WebSocket] = set()
        self._market_connections: Set[WebSocket] = set()
        self._admin_connections: Set[WebSocket] = set()
        self._notify_connections: Dict[str, Set[WebSocket]] = defaultdict(set)
        self._client_connections: Dict[str, Set[WebSocket]] = defaultdict(set)
        self._send_queues: Dict[WebSocket, asyncio.Queue] = {}
        self._send_tasks: Dict[WebSocket, asyncio.Task] = {}
        self._send_queue_maxsize = 200
        self._lock = asyncio.Lock()

    def _start_sender_locked(self, websocket: WebSocket) -> None:
        if websocket in self._send_tasks:
            return
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._send_queue_maxsize)
        self._send_queues[websocket] = queue
        self._send_tasks[websocket] = asyncio.create_task(self._sender_loop(websocket, queue))

    async def _sender_loop(self, websocket: WebSocket, queue: asyncio.Queue) -> None:
        try:
            while True:
                kind, payload = await queue.get()
                if kind == "text":
                    await websocket.send_text(str(payload))
                elif kind == "json":
                    await websocket.send_json(payload)
                elif kind == "close":
                    break
        except Exception:
            pass
        finally:
            await self._cleanup_socket(websocket)

    def _enqueue_send_locked(self, websocket: WebSocket, kind: str, payload: Any) -> bool:
        queue = self._send_queues.get(websocket)
        if queue is None:
            return False
        try:
            queue.put_nowait((kind, payload))
            return True
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
            except Exception:
                return False
            try:
                queue.put_nowait((kind, payload))
                return True
            except Exception:
                return False

    async def _cleanup_socket(self, websocket: WebSocket) -> None:
        task_to_cancel = None
        async with self._lock:
            self._log_connections.discard(websocket)
            self._market_connections.discard(websocket)
            self._admin_connections.discard(websocket)

            for device_id, conns in list(self._notify_connections.items()):
                if websocket in conns:
                    conns.discard(websocket)
                if not conns:
                    self._notify_connections.pop(device_id, None)
            for device_id, conns in list(self._client_connections.items()):
                if websocket in conns:
                    conns.discard(websocket)
                if not conns:
                    self._client_connections.pop(device_id, None)

            queue = self._send_queues.pop(websocket, None)
            if queue is not None:
                try:
                    while not queue.empty():
                        queue.get_nowait()
                except Exception:
                    pass

            task_to_cancel = self._send_tasks.pop(websocket, None)

        current_task = asyncio.current_task()
        if task_to_cancel and task_to_cancel is not current_task and not task_to_cancel.done():
            task_to_cancel.cancel()
        try:
            await websocket.close()
        except Exception:
            pass

    async def register(self, websocket: WebSocket, channel: str = "logs", device_id: str = "") -> None:
        await websocket.accept()
        async with self._lock:
            if channel == "market":
                self._market_connections.add(websocket)
            elif channel == "admin":
                self._admin_connections.add(websocket)
            elif channel == "client" and device_id:
                self._client_connections[device_id].add(websocket)
            elif channel == "notify" and device_id:
                self._notify_connections[device_id].add(websocket)
            else:
                self._log_connections.add(websocket)
            self._start_sender_locked(websocket)

    async def unregister(self, websocket: WebSocket, channel: str = "logs", device_id: str = "") -> None:
        await self._cleanup_socket(websocket)

    async def broadcast_log(self, message: str) -> int:
        dead: Set[WebSocket] = set()
        sent = 0
        async with self._lock:
            targets = list(self._log_connections)
            for ws in targets:
                if self._enqueue_send_locked(ws, "text", str(message)):
                    sent += 1
                else:
                    dead.add(ws)
            client_targets: Set[WebSocket] = set()
            for conns in self._client_connections.values():
                client_targets.update(conns)
            payload = {"event": "log_line", "line": str(message)}
            for ws in client_targets:
                if self._enqueue_send_locked(ws, "json", payload):
                    sent += 1
                else:
                    dead.add(ws)
        for ws in dead:
            await self._cleanup_socket(ws)
        return sent

    async def push_device_event(self, device_id: str, payload: Any) -> int:
        if not device_id:
            return 0
        dead: Set[WebSocket] = set()
        sent = 0
        async with self._lock:
            targets = set(self._notify_connections.get(device_id, set()))
            targets.update(self._client_connections.get(device_id, set()))
            for ws in targets:
                if self._enqueue_send_locked(ws, "json", payload):
                    sent += 1
                else:
                    dead.add(ws)
        for ws in dead:
            await self._cleanup_socket(ws)
        return sent

    async def broadcast_market_event(self, payload: Any) -> int:
        dead: Set[WebSocket] = set()
        sent = 0
        async with self._lock:
            targets = set(self._market_connections)
            for conns in self._client_connections.values():
                targets.update(conns)
            for ws in targets:
                if self._enqueue_send_locked(ws, "json", payload):
                    sent += 1
                else:
                    dead.add(ws)
        for ws in dead:
            await self._cleanup_socket(ws)
        return sent

    async def has_market_subscribers(self) -> bool:
        async with self._lock:
            return len(self._market_connections) > 0 or any(self._client_connections.values())

    async def has_admin_subscribers(self) -> bool:
        async with self._lock:
            return len(self._admin_connections) > 0

    async def broadcast_admin_event(self, payload: Any) -> int:
        dead: Set[WebSocket] = set()
        sent = 0
        async with self._lock:
            targets = list(self._admin_connections)
            for ws in targets:
                if self._enqueue_send_locked(ws, "json", payload):
                    sent += 1
                else:
                    dead.add(ws)
        for ws in dead:
            await self._cleanup_socket(ws)
        return sent

    async def snapshot_stats(self) -> Dict[str, int]:
        async with self._lock:
            log_count = len(self._log_connections)
            market_count = len(self._market_connections)
            admin_count = len(self._admin_connections)
            notify_count = sum(len(v) for v in self._notify_connections.values())
            client_count = sum(len(v) for v in self._client_connections.values())
            active_devices_keys = {
                k for k, v in self._notify_connections.items() if v
            }
            active_devices_keys.update({
                k for k, v in self._client_connections.items() if v
            })
        return {
            "log_connections": int(log_count),
            "market_connections": int(market_count),
            "admin_connections": int(admin_count),
            "notify_connections": int(notify_count),
            "client_connections": int(client_count),
            "active_devices": int(len(active_devices_keys)),
            "total_connections": int(log_count + market_count + admin_count + notify_count + client_count),
        }

    async def snapshot_active_devices(self) -> Dict[str, int]:
        async with self._lock:
            merged: Dict[str, int] = {}
            for device_id, conns in self._notify_connections.items():
                if not device_id or not conns:
                    continue
                merged[device_id] = int(merged.get(device_id, 0)) + len(conns)
            for device_id, conns in self._client_connections.items():
                if not device_id or not conns:
                    continue
                merged[device_id] = int(merged.get(device_id, 0)) + len(conns)
            return merged


ws_hub = WSHub()
