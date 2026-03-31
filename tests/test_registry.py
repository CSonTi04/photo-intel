"""Tests for TaskRegistry and the @register_task decorator."""

from src.tasks.registry import TaskRegistry


class FakeHandler:
    task_type = "fake_task"

    def compute_input_hash(self, media_item_id, config):
        return "hash"

    async def execute(self, media_item_id, task_config, input_hash):
        return {}


class AnotherHandler:
    task_type = "another_task"

    def compute_input_hash(self, media_item_id, config):
        return "hash2"

    async def execute(self, media_item_id, task_config, input_hash):
        return {}


class TestTaskRegistry:
    def test_register_and_get(self):
        reg = TaskRegistry()
        handler = FakeHandler()
        reg.register(handler)
        assert reg.get("fake_task") is handler

    def test_list_types(self):
        reg = TaskRegistry()
        reg.register(FakeHandler())
        reg.register(AnotherHandler())
        types = reg.list_types()
        assert "fake_task" in types
        assert "another_task" in types

    def test_get_unknown_returns_none(self):
        reg = TaskRegistry()
        assert reg.get("nonexistent") is None

    def test_overwrite_replaces_handler(self):
        reg = TaskRegistry()
        h1 = FakeHandler()
        h2 = FakeHandler()
        reg.register(h1)
        reg.register(h2)
        assert reg.get("fake_task") is h2

    def test_handlers_property_returns_copy(self):
        reg = TaskRegistry()
        reg.register(FakeHandler())
        handlers = reg.handlers
        assert "fake_task" in handlers
        # Mutating the copy shouldn't affect the registry
        handlers.pop("fake_task")
        assert reg.get("fake_task") is not None
