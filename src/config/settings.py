"""Application settings — static config from env/YAML, dynamic from DB."""

from pathlib import Path
from pydantic_settings import BaseSettings
from pydantic import Field


class DatabaseSettings(BaseSettings):
    host: str = Field(default="localhost", alias="DB_HOST")
    port: int = Field(default=5432, alias="DB_PORT")
    name: str = Field(default="photo_intel", alias="DB_NAME")
    user: str = Field(default="photo_intel", alias="DB_USER")
    password: str = Field(default="photo_intel", alias="DB_PASSWORD")

    @property
    def dsn(self) -> str:
        return f"postgresql+asyncpg://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"

    @property
    def sync_dsn(self) -> str:
        return f"postgresql+psycopg2://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"


class VLMWrapperSettings(BaseSettings):
    base_url: str = Field(default="http://gpu-node:8100", alias="VLM_WRAPPER_URL")
    timeout_seconds: int = Field(default=120, alias="VLM_TIMEOUT")
    readiness_poll_interval: int = Field(default=30, alias="VLM_READINESS_POLL")


class IngestSettings(BaseSettings):
    watch_dirs: list[str] = Field(default_factory=lambda: ["/data/photos"], alias="INGEST_WATCH_DIRS")
    batch_size: int = Field(default=500, alias="INGEST_BATCH_SIZE")
    poll_interval_seconds: int = Field(default=60, alias="INGEST_POLL_INTERVAL")
    supported_extensions: set[str] = Field(
        default_factory=lambda: {".jpg", ".jpeg", ".png", ".webp", ".heic", ".tiff", ".bmp", ".gif"}
    )


class WorkerSettings(BaseSettings):
    worker_id: str = Field(default="worker-1", alias="WORKER_ID")
    lease_duration_seconds: int = Field(default=300, alias="WORKER_LEASE_DURATION")
    poll_interval_seconds: int = Field(default=5, alias="WORKER_POLL_INTERVAL")
    max_concurrent_tasks: int = Field(default=4, alias="WORKER_MAX_CONCURRENT")
    vlm_concurrency: int = Field(default=1, alias="WORKER_VLM_CONCURRENCY")
    vlm_batch_size: int = Field(default=5, alias="WORKER_VLM_BATCH_SIZE")
    ocr_concurrency: int = Field(default=3, alias="WORKER_OCR_CONCURRENCY")


class ThumbnailSettings(BaseSettings):
    output_dir: str = Field(default="/data/thumbnails", alias="THUMBNAIL_DIR")
    sizes: list[int] = Field(default_factory=lambda: [256, 512])
    format: str = "webp"
    quality: int = 80


class Settings(BaseSettings):
    """Root settings — aggregates all sub-settings."""

    app_name: str = "photo-intel"
    debug: bool = Field(default=False, alias="DEBUG")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    api_port: int = Field(default=8000, alias="API_PORT")
    data_dir: Path = Field(default=Path("/data"), alias="DATA_DIR")

    db: DatabaseSettings = Field(default_factory=DatabaseSettings)
    vlm: VLMWrapperSettings = Field(default_factory=VLMWrapperSettings)
    ingest: IngestSettings = Field(default_factory=IngestSettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    thumbnail: ThumbnailSettings = Field(default_factory=ThumbnailSettings)

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
