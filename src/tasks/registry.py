"""Task registry — discovers and manages task handlers.

New task types are registered by:
1. Creating a handler class implementing TaskHandler protocol
2. Decorating it with @register_task or calling registry.register()
"""

import structlog

from src.tasks import TaskHandler

logger = structlog.get_logger()


class TaskRegistry:
    """Central registry of task handlers."""

    def __init__(self):
        self._handlers: dict[str, TaskHandler] = {}

    def register(self, handler: TaskHandler) -> None:
        """Register a task handler."""
        if handler.task_type in self._handlers:
            logger.warning(
                "task_registry.overwrite",
                task_type=handler.task_type,
            )
        self._handlers[handler.task_type] = handler
        logger.info("task_registry.registered", task_type=handler.task_type)

    def get(self, task_type: str) -> TaskHandler | None:
        """Get handler for task type."""
        return self._handlers.get(task_type)

    def list_types(self) -> list[str]:
        """List all registered task types."""
        return list(self._handlers.keys())

    @property
    def handlers(self) -> dict[str, TaskHandler]:
        return dict(self._handlers)


# Global registry instance
registry = TaskRegistry()


def register_task(cls):
    """Decorator to auto-register a task handler class."""
    instance = cls()
    registry.register(instance)
    return cls
