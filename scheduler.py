"""
定时推送调度器
包含：parse_interval / Subscription / SubscriptionManager / Scheduler
"""

from __future__ import annotations

import asyncio
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Awaitable

if TYPE_CHECKING:
    from astrbot.api import AstrBotConfig

# 最小推送间隔（秒）
_MIN_INTERVAL = 600  # 10 分钟

# 调度器轮询间隔（秒）
_POLL_INTERVAL = 30


def parse_interval(s: str) -> int:
    """
    将间隔字符串解析为秒数。
    支持单位：m（分钟）、h（小时）、d（天）、w（周）
    示例：30m -> 1800, 2h -> 7200, 1d -> 86400, 2w -> 1209600

    Raises:
        ValueError: 格式不合法或低于最小间隔
    """
    s = s.strip().lower()
    m = re.fullmatch(r"(\d+)(m|h|d|w)", s)
    if not m:
        raise ValueError(f"无法解析间隔时长: '{s}'，格式应为 30m / 2h / 1d / 7d / 2w")

    value, unit = int(m.group(1)), m.group(2)
    multipliers = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
    seconds = value * multipliers[unit]

    if seconds < _MIN_INTERVAL:
        raise ValueError(f"推送间隔不能小于 10 分钟，当前设置: {s}")

    return seconds


def _short_id() -> str:
    """生成 6 位短 ID"""
    return uuid.uuid4().hex[:6]


@dataclass
class Subscription:
    sub_id: str
    unified_msg_origin: str
    interval: str           # 原始字符串，如 "6h"
    interval_seconds: int
    tags: list[str]
    exclude_tags: list[str]
    author_id: int | None
    count: int
    cooldown: str
    enabled: bool

    @staticmethod
    def from_config_item(item: dict) -> "Subscription":
        """从 config template_list 条目构建 Subscription。"""
        interval = item.get("interval", "6h")
        try:
            interval_seconds = parse_interval(interval)
        except ValueError:
            interval_seconds = 21600  # 解析失败默认 6h

        tags_raw = item.get("tags", "")
        exclude_raw = item.get("exclude_tags", "")
        author_raw = item.get("author_id", "")

        return Subscription(
            sub_id=item.get("sub_id") or _short_id(),
            unified_msg_origin=item.get("unified_msg_origin", ""),
            interval=interval,
            interval_seconds=interval_seconds,
            tags=[t for t in tags_raw.split() if t],
            exclude_tags=[t for t in exclude_raw.split() if t],
            author_id=int(author_raw) if str(author_raw).strip().isdigit() else None,
            count=max(1, min(int(item.get("count", 1)), 10)),
            cooldown=item.get("cooldown", "1d"),
            enabled=bool(item.get("enabled", True)),
        )

    def to_config_item(self) -> dict:
        """序列化为 config template_list 条目格式。"""
        return {
            "__template_key": "subscription",
            "sub_id": self.sub_id,
            "unified_msg_origin": self.unified_msg_origin,
            "interval": self.interval,
            "tags": " ".join(self.tags),
            "exclude_tags": " ".join(self.exclude_tags),
            "author_id": str(self.author_id) if self.author_id is not None else "",
            "count": self.count,
            "cooldown": self.cooldown,
            "enabled": self.enabled,
        }

    def describe(self, next_trigger: float | None = None) -> str:
        """生成人类可读的订阅描述，用于 /pixiv sub list 回复。"""
        parts = [f"[{self.sub_id}] 间隔: {self.interval}"]
        if self.tags:
            parts.append(f"标签: {', '.join(self.tags)}")
        if self.exclude_tags:
            parts.append(f"排除: {', '.join(self.exclude_tags)}")
        if self.author_id is not None:
            parts.append(f"画师: {self.author_id}")
        parts.append(f"数量: {self.count}")
        if self.cooldown:
            parts.append(f"冷却: {self.cooldown}")
        if not self.enabled:
            parts.append("【已暂停】")
        if next_trigger is not None:
            remaining = next_trigger - time.time()
            if remaining > 0:
                parts.append(f"下次推送: 约 {_format_remaining(remaining)}")
            else:
                parts.append("下次推送: 即将触发")
        return " | ".join(parts)


def _format_remaining(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 3600:
        return f"{seconds // 60} 分钟后"
    if seconds < 86400:
        return f"{seconds // 3600} 小时后"
    return f"{seconds // 86400} 天后"


class SubscriptionManager:
    """
    订阅的 CRUD，统一读写 config["subscriptions"]。
    next_trigger 运行时状态存 KV，避免频繁写 config。
    """

    # KV key 前缀
    _KV_PREFIX = "sub_next_trigger:"

    def __init__(self, config: "AstrBotConfig", kv_get: Callable, kv_put: Callable) -> None:
        self._config = config
        self._kv_get = kv_get
        self._kv_put = kv_put

    def load_all(self) -> list[Subscription]:
        """从 config 加载所有订阅。"""
        raw: list[dict] = self._config.get("subscriptions") or []
        subs = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                subs.append(Subscription.from_config_item(item))
            except Exception:
                pass
        return subs

    def _save_all(self, subs: list[Subscription]) -> None:
        self._config["subscriptions"] = [s.to_config_item() for s in subs]
        self._config.save_config()

    def add(self, sub: Subscription) -> None:
        subs = self.load_all()
        subs.append(sub)
        self._save_all(subs)

    def remove(self, sub_id: str) -> bool:
        """删除指定 ID 的订阅，返回是否找到并删除。"""
        subs = self.load_all()
        new_subs = [s for s in subs if s.sub_id != sub_id]
        if len(new_subs) == len(subs):
            return False
        self._save_all(new_subs)
        return True

    def clear_by_origin(self, unified_msg_origin: str) -> int:
        """清空指定会话的所有订阅，返回删除数量。"""
        subs = self.load_all()
        new_subs = [s for s in subs if s.unified_msg_origin != unified_msg_origin]
        removed = len(subs) - len(new_subs)
        if removed:
            self._save_all(new_subs)
        return removed

    def list_by_origin(self, unified_msg_origin: str) -> list[Subscription]:
        return [s for s in self.load_all() if s.unified_msg_origin == unified_msg_origin]

    async def get_next_trigger(self, sub_id: str) -> float | None:
        val = await self._kv_get(f"{self._KV_PREFIX}{sub_id}", None)
        return float(val) if val is not None else None

    async def set_next_trigger(self, sub_id: str, ts: float) -> None:
        await self._kv_put(f"{self._KV_PREFIX}{sub_id}", ts)

    async def init_next_trigger(self, sub: Subscription) -> float:
        """首次初始化 next_trigger：在 [0, interval] 内随机，避免重启后立即全量触发。"""
        existing = await self.get_next_trigger(sub.sub_id)
        if existing is not None:
            return existing
        ts = time.time() + random.uniform(0, sub.interval_seconds)
        await self.set_next_trigger(sub.sub_id, ts)
        return ts


# 推送回调类型：接收 Subscription，执行推送
PushCallback = Callable[["Subscription"], Awaitable[None]]


class Scheduler:
    """
    asyncio 后台调度器，每 _POLL_INTERVAL 秒轮询一次所有订阅。
    到期则触发推送，并重新随机计算下次触发时间。
    """

    def __init__(self, manager: SubscriptionManager, push_cb: PushCallback) -> None:
        self._manager = manager
        self._push_cb = push_cb
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()

    async def _loop(self) -> None:
        from astrbot.api import logger

        # 初始化所有订阅的 next_trigger
        for sub in self._manager.load_all():
            if sub.enabled and sub.unified_msg_origin:
                await self._manager.init_next_trigger(sub)

        while True:
            await asyncio.sleep(_POLL_INTERVAL)
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[pixiv scheduler] 轮询异常: {e}")

    async def _tick(self) -> None:
        from astrbot.api import logger

        now = time.time()
        subs = self._manager.load_all()

        for sub in subs:
            if not sub.enabled or not sub.unified_msg_origin:
                continue

            next_trigger = await self._manager.get_next_trigger(sub.sub_id)
            if next_trigger is None:
                next_trigger = await self._manager.init_next_trigger(sub)

            if now < next_trigger:
                continue

            # 先更新下次触发时间，再推送，避免推送失败导致反复触发
            new_trigger = now + random.uniform(0, sub.interval_seconds)
            await self._manager.set_next_trigger(sub.sub_id, new_trigger)

            try:
                await self._push_cb(sub)
            except Exception as e:
                logger.error(f"[pixiv scheduler] 订阅 {sub.sub_id} 推送失败: {e}")
