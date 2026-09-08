import os
import json
import time
import logging
import redis.asyncio as redis
from typing import Optional, Dict, Any

from observability.tracer import AgentTracer

logger = logging.getLogger("redis.broker")

def _json_log(event_type, message, data=None, level="INFO"):
    logger.log(getattr(logging, level), json.dumps({
        "timestamp": time.time(),
        "level": level,
        "component": "notifier",
        "event_type": event_type,
        "message": message,
        "data": data or {}
    }, ensure_ascii=False))

class RedisTaskBroker:
    def __init__(self) -> None:
        self.password: Optional[str] = self._load_secret()
        self.url: str = os.getenv("REDIS_URL", "redis://redis:6379/0")
        self.client: Optional[redis.Redis] = None

    def _load_secret(self) -> Optional[str]:
        secret_path = os.getenv("REDIS_PASS_FILE", "/run/secrets/redis_pass")
        try:
            if os.path.exists(secret_path):
                with open(secret_path, "r", encoding="utf-8") as f:
                    lines = f.read().splitlines()
                    return lines[0] if lines else None
        except Exception as e:
            _json_log("broker_secret_error", "Error reading Redis secret", {"error": str(e)}, "ERROR")
        return os.getenv("REDIS_PASS")

    async def connect(self) -> None:
        if not self.client:
            try:
                self.client = redis.Redis(
                    host="redis",
                    port=6379,
                    password=self.password,
                    decode_responses=True,
                    socket_timeout=None,
                    socket_keepalive=True,
                    retry_on_timeout=True,
                    health_check_interval=30
                )
                await self.client.ping()
                _json_log("broker_connect", "Redis connection established", {"status": "connected"})
            except Exception as e:
                _json_log("broker_connect_error", "Failed to connect to Redis", {"error": str(e)}, "ERROR")
                raise

    async def push_task(self, queue_name: str, task_data: Dict[str, Any]) -> None:
        if not self.client:
            await self.connect()
        payload = json.dumps(task_data, ensure_ascii=False)
        await self.client.lpush(queue_name, payload)
        try:
            qlen = await self.client.llen(queue_name)
            _json_log("tactical", "Task pushed to queue", {
                "action": "queue_push",
                "queue": queue_name,
                "queue_length": qlen,
                "task_type": task_data.get("action", task_data.get("type", "unknown"))
            })
            AgentTracer._emit({
                "timestamp": time.time(),
                "level": "INFO",
                "component": "brain",
                "event_type": "tactical",
                "message": "Task pushed to queue",
                "data": {
                    "action": "queue_push",
                    "queue": queue_name,
                    "queue_length": qlen,
                    "task_type": task_data.get("action", task_data.get("type", "unknown"))
                }
            })
        except Exception:
            pass

    async def publish(self, queue_name: str, task_data: Dict[str, Any]) -> None:
        await self.push_task(queue_name, task_data)

    async def get_task(self, queue_name: str) -> Optional[Dict[str, Any]]:
        if not self.client:
            await self.connect()
        result = await self.client.brpop(queue_name, timeout=5)
        if not result:
            return None
        try:
            task = json.loads(result[1])
            try:
                qlen = await self.client.llen(queue_name)
                _json_log("tactical", "Task popped from queue", {
                    "action": "queue_pop",
                    "queue": queue_name,
                    "queue_length": qlen,
                    "task_type": task.get("action", task.get("type", "unknown"))
                })
                AgentTracer._emit({
                    "timestamp": time.time(),
                    "level": "INFO",
                    "component": "brain",
                    "event_type": "tactical",
                    "message": "Task popped from queue",
                    "data": {
                        "action": "queue_pop",
                        "queue": queue_name,
                        "queue_length": qlen,
                        "task_type": task.get("action", task.get("type", "unknown"))
                    }
                })
            except Exception:
                pass
            return task
        except json.JSONDecodeError:
            _json_log("broker_invalid_payload", "Invalid JSON payload in queue", {"queue": queue_name}, "ERROR")
            AgentTracer._emit({
                "timestamp": time.time(),
                "level": "ERROR",
                "component": "brain",
                "event_type": "tactical",
                "message": "Invalid JSON payload in queue",
                "data": {
                    "action": "queue_push_error",
                    "queue": queue_name,
                    "error": "json_decode_error"
                }
            })
            return None

    async def acquire_lock(self, task_id: str, owner_id: str, timeout: int = 300) -> bool:
        if not self.client:
            await self.connect()
        lock_key = f"lock:mail:{task_id}"
        return await self.client.set(lock_key, owner_id, nx=True, ex=timeout)

    async def release_lock(self, task_id: str) -> None:
        if not self.client:
            await self.connect()
        await self.client.delete(f"lock:mail:{task_id}")
