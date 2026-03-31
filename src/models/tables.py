"""SQLAlchemy ORM models matching the database schema."""

# ── Enums ──────────────────────────────────────────────────────
import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from src.models.database import Base


class MediaKind(enum.StrEnum):
    photo = "photo"
    screenshot = "screenshot"
    unknown = "unknown"


class TaskState(enum.StrEnum):
    discovered = "discovered"
    pending = "pending"
    leased = "leased"
    completed = "completed"
    failed = "failed"
    dead_letter = "dead_letter"


class DigestType(enum.StrEnum):
    daily = "daily"
    resurface = "resurface"


# ── Media ──────────────────────────────────────────────────────


class MediaItem(Base):
    __tablename__ = "media_item"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    content_hash = Column(String(64), unique=True, nullable=False)
    file_path = Column(Text, nullable=False)
    source = Column(String(128), nullable=False, default="filesystem")
    captured_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)
    mime_type = Column(String(64))
    width = Column(Integer)
    height = Column(Integer)
    file_size = Column(BigInteger)
    media_kind = Column(Enum(MediaKind, name="media_kind", create_type=False), default=MediaKind.unknown)
    metadata_json = Column(JSONB, default={})

    # Relationships
    exif = relationship("MediaExif", back_populates="media_item", uselist=False, cascade="all, delete-orphan")
    ocr_results = relationship("MediaOCR", back_populates="media_item", cascade="all, delete-orphan")
    task_instances = relationship("TaskInstance", back_populates="media_item", cascade="all, delete-orphan")


class MediaExif(Base):
    __tablename__ = "media_exif"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    media_item_id = Column(
        UUID(as_uuid=True), ForeignKey("media_item.id", ondelete="CASCADE"), unique=True, nullable=False
    )  # noqa: E501
    exif_json = Column(JSONB, default={})
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    media_item = relationship("MediaItem", back_populates="exif")


class MediaOCR(Base):
    __tablename__ = "media_ocr"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    media_item_id = Column(UUID(as_uuid=True), ForeignKey("media_item.id", ondelete="CASCADE"), nullable=False)
    engine = Column(String(64), nullable=False)
    engine_version = Column(String(32), nullable=False)
    full_text = Column(Text)
    structured_blocks_json = Column(JSONB, default=[])
    confidence = Column(Float)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("media_item_id", "engine", "engine_version"),)
    media_item = relationship("MediaItem", back_populates="ocr_results")


# ── Tasks ──────────────────────────────────────────────────────


class TaskDefinition(Base):
    __tablename__ = "task_definition"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_type = Column(String(64), nullable=False)
    version = Column(Integer, nullable=False, default=1)
    config_json = Column(JSONB, default={})
    prompt_template = Column(Text)
    prompt_version = Column(Integer, default=1)
    output_schema = Column(JSONB)
    prerequisites = Column(JSONB, default=[])
    applies_to = Column(JSONB, default=["photo", "screenshot"])
    enabled = Column(Boolean, default=True)
    priority = Column(Integer, default=100)
    max_retries = Column(Integer, default=3)
    timeout_seconds = Column(Integer, default=300)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (UniqueConstraint("task_type", "version"),)


class TaskInstance(Base):
    __tablename__ = "task_instance"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    media_item_id = Column(UUID(as_uuid=True), ForeignKey("media_item.id", ondelete="CASCADE"), nullable=False)
    task_type = Column(String(64), nullable=False)
    task_version = Column(Integer, nullable=False)
    state = Column(Enum(TaskState, name="task_state", create_type=False), default=TaskState.discovered)
    priority = Column(Integer, default=100)
    available_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    lease_until = Column(DateTime(timezone=True))
    leased_by = Column(String(128))
    attempts = Column(Integer, default=0)
    max_attempts = Column(Integer, default=3)
    input_hash = Column(String(64), nullable=False)
    error_message = Column(Text)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)
    completed_at = Column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("media_item_id", "task_type", "task_version", "input_hash"),
        Index("idx_task_instance_queue", "state", "priority", "available_at"),
    )

    media_item = relationship("MediaItem", back_populates="task_instances")
    output = relationship("TaskOutput", back_populates="task_instance", uselist=False, cascade="all, delete-orphan")
    dead_letter = relationship(
        "DeadLetterTask", back_populates="task_instance", uselist=False, cascade="all, delete-orphan"
    )  # noqa: E501


class TaskOutput(Base):
    __tablename__ = "task_output"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_instance_id = Column(
        UUID(as_uuid=True), ForeignKey("task_instance.id", ondelete="CASCADE"), unique=True, nullable=False
    )  # noqa: E501
    output_json = Column(JSONB, default={})
    summary_text = Column(Text)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    task_instance = relationship("TaskInstance", back_populates="output")


class DeadLetterTask(Base):
    __tablename__ = "dead_letter_task"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_instance_id = Column(
        UUID(as_uuid=True), ForeignKey("task_instance.id", ondelete="CASCADE"), unique=True, nullable=False
    )  # noqa: E501
    error_type = Column(String(128))
    error_message = Column(Text)
    payload_json = Column(JSONB, default={})
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    task_instance = relationship("TaskInstance", back_populates="dead_letter")


# ── Digest ─────────────────────────────────────────────────────


class DigestRun(Base):
    __tablename__ = "digest_run"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    digest_type = Column(Enum(DigestType, name="digest_type", create_type=False), nullable=False)
    target_date = Column(Date, nullable=False)
    config_snapshot_json = Column(JSONB, default={})
    status = Column(String(32), default="pending")
    total_items = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
    completed_at = Column(DateTime(timezone=True))

    __table_args__ = (UniqueConstraint("digest_type", "target_date"),)
    items = relationship("DigestItem", back_populates="digest_run", cascade="all, delete-orphan")


class DigestItem(Base):
    __tablename__ = "digest_item"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    digest_run_id = Column(UUID(as_uuid=True), ForeignKey("digest_run.id", ondelete="CASCADE"), nullable=False)
    media_item_id = Column(UUID(as_uuid=True), ForeignKey("media_item.id", ondelete="CASCADE"), nullable=False)
    section = Column(String(64), nullable=False)
    rank_score = Column(Float, default=0.0)
    summary_text = Column(Text)
    metadata_json = Column(JSONB, default={})
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)

    digest_run = relationship("DigestRun", back_populates="items")


# ── Metrics ────────────────────────────────────────────────────


class ProcessingMetric(Base):
    __tablename__ = "processing_metric"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_instance_id = Column(UUID(as_uuid=True), ForeignKey("task_instance.id", ondelete="SET NULL"))
    worker_id = Column(String(128))
    task_type = Column(String(64))
    duration_ms = Column(Integer)
    success = Column(Boolean)
    metadata_json = Column(JSONB, default={})
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow)
