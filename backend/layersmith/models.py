"""Database models. SQLite by default; nothing here is SQLite-specific.

A project holds the editable definition. A build is an immutable record of
one attempt: it snapshots the Containerfile, the resolved packages, the base
digest, file checksums and the log, so build #42 can still be explained
months later. A version is built once and never overwritten.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, create_engine, func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

ACTIVE_STATES = ("queued", "preparing", "pulling", "building", "testing", "exporting")
BUILD_STATES = ACTIVE_STATES + ("ready", "failed", "cancelled")


def _uuid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(63), unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    repository: Mapped[str] = mapped_column(String(200))
    template: Mapped[str] = mapped_column(String(80), default="Custom")
    mode: Mapped[str] = mapped_column(String(16), default="gui")  # gui | advanced
    spec: Mapped[dict] = mapped_column(JSON, default=dict)
    containerfile: Mapped[str] = mapped_column(Text, default="")
    next_version: Mapped[str] = mapped_column(String(32), default="1.0.0")
    # Base image pinned by digest so rebuilds do not silently follow a moved tag.
    base_image: Mapped[str | None] = mapped_column(String(255))
    base_digest: Mapped[str | None] = mapped_column(String(80))
    base_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    base_upstream_digest: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    builds: Mapped[list["Build"]] = relationship(back_populates="project", cascade="all, delete-orphan",
                                                 order_by="Build.number.desc()")

    @property
    def base_update_available(self) -> bool:
        return bool(self.base_digest and self.base_upstream_digest and self.base_digest != self.base_upstream_digest)


class Build(Base):
    __tablename__ = "builds"
    __table_args__ = (UniqueConstraint("project_id", "version", name="uq_build_project_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # Allocated by next_build_number(): SQLite only auto-increments integer primary keys,
    # and the primary key here is a UUID so builds can be referenced before insert.
    number: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    version: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(String(32), default="build")  # build | rebuild | base_update | clone
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    error: Mapped[str | None] = mapped_column(Text)
    log: Mapped[str] = mapped_column(Text, default="")

    # Immutable snapshot of exactly what was built.
    mode: Mapped[str] = mapped_column(String(16), default="gui")
    containerfile: Mapped[str] = mapped_column(Text, default="")
    spec: Mapped[dict] = mapped_column(JSON, default=dict)
    packages: Mapped[list] = mapped_column(JSON, default=list)
    files: Mapped[list] = mapped_column(JSON, default=list)
    architecture: Mapped[str] = mapped_column(String(16), default="amd64")
    base_image: Mapped[str | None] = mapped_column(String(255))
    base_digest: Mapped[str | None] = mapped_column(String(80))

    # Result.
    image_ref: Mapped[str | None] = mapped_column(String(255))
    image_id: Mapped[str | None] = mapped_column(String(80))
    image_digest: Mapped[str | None] = mapped_column(String(80))
    image_size: Mapped[int | None] = mapped_column(BigInteger)
    export_path: Mapped[str | None] = mapped_column(String(500))
    export_sha256: Mapped[str | None] = mapped_column(String(64))
    export_size: Mapped[int | None] = mapped_column(BigInteger)
    airgap_path: Mapped[str | None] = mapped_column(String(500))
    airgap_sha256: Mapped[str | None] = mapped_column(String(64))
    airgap_size: Mapped[int | None] = mapped_column(BigInteger)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    project: Mapped[Project] = relationship(back_populates="builds")

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at and self.finished_at:
            return (self.finished_at - self.started_at).total_seconds()
        return None


class Blob(Base):
    """An uploaded file or fetched tool binary, stored once per checksum."""

    __tablename__ = "blobs"

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    filename: Mapped[str] = mapped_column(String(255))
    size: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(16), default="file")  # file | tool
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Setting(Base):
    """Operator-editable settings that override the environment defaults."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())


def next_build_number(session) -> int:
    """Next global build number. Call inside the transaction that inserts the build."""
    highest = session.query(func.max(Build.number)).scalar()
    return int(highest or 0) + 1


def make_engine(database_url: str):
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    return create_engine(database_url, future=True, connect_args=connect_args)


def make_session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def create_all(engine) -> None:
    Base.metadata.create_all(engine)
