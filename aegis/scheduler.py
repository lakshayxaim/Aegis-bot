"""Fires reminders and scheduled requests. They live in the database, so they survive restarts."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger("aegis.scheduler")
MISSED_AFTER = 120


class Scheduler:
    def __init__(self, store: Any, bus: Any, agent: Any, shell: Any = None) -> None:
        self.store, self.bus, self.agent, self.shell = store, bus, agent, shell
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="aegis-scheduler")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:
                logger.exception("scheduler tick failed")
            await asyncio.sleep(1)

    def tick(self, now: float | None = None) -> None:
        now = now or time.time()
        for reminder in self.store.due_reminders(now):
            late = now - reminder["due"]
            if reminder["kind"] == "run":
                self._run(reminder, late)
            else:
                self._notify(reminder, late)
            if reminder["repeat_seconds"]:
                # Skip any occurrences that passed while Aegis was closed.
                due = reminder["due"]
                while due <= now:
                    due += reminder["repeat_seconds"]
                self.store.set_reminder(reminder["id"], due=due, fired_at=now)
            else:
                self.store.set_reminder(reminder["id"], status="missed" if late > MISSED_AFTER else "fired", fired_at=now)
            self.bus.publish({"type": "reminders_changed"})

    def _notify(self, reminder: dict[str, Any], late: float) -> None:
        title = "Missed reminder" if late > MISSED_AFTER else "Reminder"
        self.bus.publish({"type": "notification", "title": title, "message": reminder["message"], "reminder_id": reminder["id"]})
        if self.shell:
            self.shell.notify(title, reminder["message"])

    def _run(self, reminder: dict[str, Any], late: float) -> None:
        thread = self.store.create_thread(f"Scheduled: {reminder['message'][:50]}", source="scheduled")
        self.bus.publish({"type": "thread_created", "thread": thread})
        self.agent.submit(thread["id"], reminder["message"], auto=True)
        self.bus.publish({"type": "notification", "title": "Running a scheduled task", "message": reminder["message"]})
        if self.shell:
            self.shell.notify("Running a scheduled task", reminder["message"])
