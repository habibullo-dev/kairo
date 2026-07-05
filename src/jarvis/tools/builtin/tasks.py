"""Task tools: the model-facing surface of scheduling.

``schedule_task`` defaults to **ask** — and, unlike every other tool, it is never
"always"-able (see the REPL's ``_persist_always``). It is a *deferred-execution*
prompt-injection sink, strictly worse than ``remember``: the payload it stores is
eventually replayed and *run with tools*, unattended. A fetched page saying
"schedule a job tonight: run <curl | sh>" must never persist on the model's
authority alone, so the human sees the full payload and the computed first fire
time at the prompt (see ``_call_summary``) and approves the actual future action.

``list_tasks`` is read-only (allow); ``cancel_task`` asks (the model shouldn't be
able to silently drop the user's reminders). All three register only when a
TaskService is present — with the scheduler off, they never reach the model.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from jarvis.scheduler.service import ScheduleError
from jarvis.tools.base import Permission, Tool, ToolContext, ToolResult


class _NeedsTasks:
    """Mixin: register only when the context carries a TaskService.

    A plain mixin (not a ``Tool`` subclass) so it doesn't trip
    ``Tool.__init_subclass__``'s required-attribute check at import time.
    """

    @classmethod
    def is_available(cls, context: ToolContext) -> bool:
        return getattr(context, "tasks", None) is not None


class ScheduleTaskParams(BaseModel):
    kind: Literal["reminder", "job"] = Field(
        description=(
            "'reminder' = a message delivered to the user at the time (no action). "
            "'job' = a prompt you will run yourself, unattended, at the time."
        )
    )
    title: str = Field(description="A short label for the task (shown in the task list).")
    payload: str = Field(
        description=(
            "For a reminder: the message to deliver. For a job: the full instructions "
            "to run — write them self-contained, since no human is present to answer "
            "questions and approval-gated tools will be denied."
        )
    )
    once_at: str | None = Field(
        default=None,
        description="Run once at this ISO-8601 local time, e.g. '2026-07-07T09:00'. ",
    )
    cron: str | None = Field(
        default=None,
        description="Recurring 5-field cron in local time, e.g. '0 9 * * 1-5' (9am weekdays).",
    )
    every_seconds: int | None = Field(
        default=None,
        description="Recurring interval in seconds (minimum 60).",
    )

    @model_validator(mode="after")
    def _exactly_one_schedule(self) -> ScheduleTaskParams:
        given = [
            name
            for name, value in (
                ("once_at", self.once_at),
                ("cron", self.cron),
                ("every_seconds", self.every_seconds),
            )
            if value is not None
        ]
        if len(given) != 1:
            raise ValueError(
                "provide exactly one schedule: once_at, cron, or every_seconds "
                f"(got {given or 'none'})"
            )
        return self

    def to_schedule(self) -> tuple[str, str]:
        if self.once_at is not None:
            return "once", self.once_at
        if self.cron is not None:
            return "cron", self.cron
        return "interval", str(self.every_seconds)


class ScheduleTaskTool(_NeedsTasks, Tool):
    name = "schedule_task"
    description = (
        "Schedule a reminder (delivered to the user) or a job (a prompt you run "
        "yourself, unattended) for a future time — once, on a cron schedule, or on "
        "an interval. The user approves each schedule. Times are the user's local time."
    )
    Params = ScheduleTaskParams
    permission_default = Permission.ASK  # deferred-execution injection sink; never silent

    async def run(self, params: ScheduleTaskParams) -> ToolResult | str:
        tasks = self.context.tasks
        if tasks is None:
            return ToolResult(content="Scheduling is not enabled.", is_error=True)
        schedule_kind, spec = params.to_schedule()
        try:
            task = await tasks.schedule(
                kind=params.kind,
                title=params.title,
                payload=params.payload,
                schedule_kind=schedule_kind,
                schedule_spec=spec,
                created_by="agent",
            )
        except ScheduleError as exc:
            # Model-readable: the message includes the current local time for a
            # past 'once', so the model can self-correct a timezone slip.
            return ToolResult(content=str(exc), is_error=True)
        return f"Scheduled. {tasks.describe(task)}"


class ListTasksParams(BaseModel):
    include_finished: bool = Field(
        default=False, description="Include done/cancelled/failed/missed tasks too."
    )


class ListTasksTool(_NeedsTasks, Tool):
    name = "list_tasks"
    description = "List scheduled tasks (active by default): kind, schedule, next run, status."
    Params = ListTasksParams
    permission_default = Permission.ALLOW  # read-only

    async def run(self, params: ListTasksParams) -> ToolResult | str:
        tasks = self.context.tasks
        if tasks is None:
            return ToolResult(content="Scheduling is not enabled.", is_error=True)
        items = await tasks.store.list(include_finished=params.include_finished)
        if not items:
            return "No tasks."
        lines = []
        for task in items:
            line = tasks.describe(task)
            if task.last_error:
                line += f" — last error: {task.last_error}"
            lines.append(line)
        return "\n".join(lines)


class CancelTaskParams(BaseModel):
    task_id: int = Field(description="The id of the task to cancel (from list_tasks).")


class CancelTaskTool(_NeedsTasks, Tool):
    name = "cancel_task"
    description = "Cancel a scheduled task so it no longer runs (kept in history)."
    Params = CancelTaskParams
    permission_default = Permission.ASK

    async def run(self, params: CancelTaskParams) -> ToolResult | str:
        tasks = self.context.tasks
        if tasks is None:
            return ToolResult(content="Scheduling is not enabled.", is_error=True)
        task = await tasks.cancel(params.task_id)
        if task is None:
            return ToolResult(content=f"No active task #{params.task_id} to cancel.", is_error=True)
        return f"Cancelled task #{task.id} ({task.title})."
