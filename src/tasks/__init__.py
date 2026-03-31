"""Task registry — pluggable task handlers."""

from typing import Protocol
from uuid import UUID


class TaskHandler(Protocol):
    """Interface every task handler must implement."""

    task_type: str

    async def execute(
        self,
        media_item_id: UUID,
        task_config: dict,
        input_hash: str,
    ) -> dict:
        """Execute the task. Returns output_json dict.

        Raises:
            TaskRetryableError: for transient failures (will be retried)
            TaskPermanentError: for permanent failures (goes to DLQ)
        """
        ...

    def compute_input_hash(self, media_item_id: UUID, config: dict) -> str:
        """Compute deterministic hash for idempotency."""
        ...


class TaskRetryableError(Exception):
    """Raised when a task fails but should be retried."""
    pass


class TaskPermanentError(Exception):
    """Raised when a task fails permanently (goes to DLQ)."""
    pass
