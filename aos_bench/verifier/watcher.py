from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

EventCallback = Callable[[dict], Awaitable[None]]


class EventsWatcher:
    def __init__(self, events_path: Path, poll_interval_sec: float = 0.2) -> None:
        self._events_path = events_path
        self._poll_interval_sec = poll_interval_sec
        self._stop_event = asyncio.Event()
        self._offset = 0

    async def replay(self, callback: EventCallback) -> None:
        if not self._events_path.exists():
            self._offset = 0
            return
        for event in self._read_from_offset(0):
            await self._dispatch(event, callback)
        self._offset = self._events_path.stat().st_size

    async def watch(self, callback: EventCallback) -> None:
        while not self._stop_event.is_set():
            if self._events_path.exists():
                current_size = self._events_path.stat().st_size
                if current_size < self._offset:
                    self._offset = 0
                if current_size > self._offset:
                    for event in self._read_from_offset(self._offset):
                        await self._dispatch(event, callback)
                    self._offset = self._events_path.stat().st_size
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._poll_interval_sec)
            except asyncio.TimeoutError:
                continue

    def stop(self) -> None:
        self._stop_event.set()

    def _read_from_offset(self, offset: int) -> list[dict]:
        if not self._events_path.exists():
            return []
        events: list[dict] = []
        with self._events_path.open() as handle:
            handle.seek(offset)
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    logger.warning("verification: ignoring malformed events.jsonl line: %r", line)
        return events

    async def _dispatch(self, event: dict, callback: EventCallback) -> None:
        if event.get("event") != "complete":
            return
        await callback(event)
