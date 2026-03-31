"""Integration tests for the FastAPI application using mocked DB sessions."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

# ── Helpers ────────────────────────────────────────────────────


def _setup_app():
    """Import the app with DB dependencies mocked."""
    # We need to patch the database module before the app is imported
    # to prevent real DB connections at module import time.
    with patch("src.models.database.create_async_engine"), \
         patch("src.models.database.async_sessionmaker"):
        from src.api.app import app
        return app


def _make_test_client(app, session_mock):
    """Create a TestClient with get_session overridden."""
    import src.api.app
    from src.models.database import get_session

    async def override_get_session():
        yield session_mock

    app.dependency_overrides[get_session] = override_get_session

    # Patch the module-level async_session used by endpoints
    mock_maker = MagicMock()
    mock_maker.return_value.__aenter__ = AsyncMock(return_value=session_mock)
    mock_maker.return_value.__aexit__ = AsyncMock(return_value=False)
    src.api.app.async_session = mock_maker

    client = TestClient(app, raise_server_exceptions=False)
    return client


# ── Tests ──────────────────────────────────────────────────────


class TestHealthEndpoint:
    def test_health_returns_200(self):
        app = _setup_app()
        with TestClient(app) as client:
            resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "status" in data


class TestStatsEndpoint:
    def test_stats_returns_200(self):
        app = _setup_app()
        session = AsyncMock()

        # Mock the multiple DB queries that get_stats makes
        count_result = MagicMock()
        count_result.scalar.return_value = 42
        session.execute.return_value = count_result

        client = _make_test_client(app, session)

        with patch("src.api.app.queue") as mock_queue:
            mock_queue.get_queue_stats = AsyncMock(return_value={
                "pending": 10, "leased": 2, "completed": 100,
            })
            resp = client.get("/stats")

        assert resp.status_code == 200


class TestMediaEndpoints:
    def test_list_media_returns_200(self):
        app = _setup_app()
        session = AsyncMock()

        scalars_mock = MagicMock()
        scalars_mock.all.return_value = []
        result_mock = MagicMock()
        result_mock.scalars.return_value = scalars_mock
        session.execute.return_value = result_mock

        client = _make_test_client(app, session)
        resp = client.get("/media")
        assert resp.status_code == 200

    def test_media_detail_404_for_unknown(self):
        app = _setup_app()
        session = AsyncMock()

        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None
        session.execute.return_value = result_mock

        client = _make_test_client(app, session)
        resp = client.get(f"/media/{uuid.uuid4()}")
        assert resp.status_code == 404


class TestTaskDefinitionsEndpoint:
    def test_list_definitions_returns_200(self):
        app = _setup_app()
        session = AsyncMock()

        scalars_mock = MagicMock()
        scalars_mock.all.return_value = []
        result_mock = MagicMock()
        result_mock.scalars.return_value = scalars_mock
        session.execute.return_value = result_mock

        client = _make_test_client(app, session)
        resp = client.get("/tasks/definitions")
        assert resp.status_code == 200


class TestDLQEndpoint:
    def test_list_dlq_returns_200(self):
        app = _setup_app()
        session = AsyncMock()

        scalars_mock = MagicMock()
        scalars_mock.all.return_value = []
        result_mock = MagicMock()
        result_mock.scalars.return_value = scalars_mock
        session.execute.return_value = result_mock

        client = _make_test_client(app, session)
        resp = client.get("/dlq")
        assert resp.status_code == 200


class TestMetricsEndpoint:
    def test_processing_metrics_returns_200(self):
        app = _setup_app()
        session = AsyncMock()

        result_mock = MagicMock()
        result_mock.fetchall.return_value = []
        session.execute.return_value = result_mock

        client = _make_test_client(app, session)
        resp = client.get("/metrics/processing")
        assert resp.status_code == 200
