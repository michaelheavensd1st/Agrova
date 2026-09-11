"""Production Engine ORM models.

The Production Engine is the species-agnostic core of Agrovix. It
generalises what was previously a set of species-specific tables
(hatcheries, ponds, batches, feed logs, mortality logs, sampling logs)
into a small, uniform hierarchy:

    Organization
        ↓
      Farm
        ↓
      ProductionSite      (physical operating location)
        ↓
      ProductionUnit      (tank / pond / cage / raceway / biofloc …)
        ↓
      ProductionBatch     (a stocking cycle, with a typed lifecycle)
        ↓
      ProductionEvent     (append-only operational activity)

Design principles:

* **One engine, many species** — aquaculture unit types are seeded as
  system defaults; organizations can extend with their own custom types.
* **Every operational activity is an event** — feeding, mortality,
  sampling, water-quality, medication, transfer, harvest, inspection.
  Events are append-only and each payload is validated against a
  catalog-registered Pydantic schema (see ``app.production.event_catalog``).
* **State machine on the batch** — transitions happen only through
  ``ProductionBatchService`` and are recorded in an append-only
  ``ProductionBatchTransition`` history.
* **Denormalised tenant fields on events** — ``organization_id``,
  ``farm_id`` and ``site_id`` are copied onto every event so tenant
  isolation, filtering and (future) partitioning do not require joins.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy import (
    Enum as SQLEnum,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.farm import Farm
    from app.models.user import User


# --------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------- #
class ProductionSiteStatus(enum.StrEnum):
    ACTIVE = "active"
    MAINTENANCE = "maintenance"
    CLOSED = "closed"


class ProductionUnitStatus(enum.StrEnum):
    ACTIVE = "active"
    MAINTENANCE = "maintenance"
    CLOSED = "closed"


class ProductionTransferRole(enum.StrEnum):
    OUT = "out"
    IN = "in"


class ProductionBatchState(enum.StrEnum):
    PLANNED = "planned"
    STOCKED = "stocked"
    ACTIVE = "active"
    HARVESTED = "harvested"
    CLOSED = "closed"
    SUSPENDED = "suspended"
    CANCELLED = "cancelled"
    FAILED = "failed"


# JSONB on Postgres, plain JSON on SQLite tests.
_JSON = JSON().with_variant(JSONB(), "postgresql")


# --------------------------------------------------------------------- #
# ProductionUnitType — reference data (system + org-custom)
# --------------------------------------------------------------------- #
class ProductionUnitType(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """System-seeded + org-scoped unit types.

    * ``is_system=True`` rows have ``organization_id IS NULL`` — those are
      seeded by :func:`app.seed.seed_permissions_and_roles` and are
      immutable to organizations (rename / delete forbidden).
    * Custom types belong to a single organization and share a unique
      ``code`` with system types allowed (different scope).
    """

    __tablename__ = "production_unit_types"
    __table_args__ = (
        # Per-org codes are unique; system-owned codes are policed
        # separately (see the partial unique index below on Postgres).
        UniqueConstraint("organization_id", "code", name="uq_unit_type_org_code"),
        Index(
            "uq_unit_type_system_code",
            "code",
            unique=True,
            postgresql_where=text("organization_id IS NULL"),
            sqlite_where=text("organization_id IS NULL"),
        ),
    )

    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        default="",
        doc="User-facing singular label (e.g. 'Pond'). Never expose 'Production Unit' in the UI.",
    )
    plural_name: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        doc="User-facing plural label (e.g. 'Ponds'). Falls back to display_name + 's'.",
    )
    vertical: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        index=True,
        doc="Optional vertical tag ('aquaculture', 'livestock', 'crop', …).",
    )
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    metadata_json: Mapped[dict | None] = mapped_column("metadata", _JSON, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )


# --------------------------------------------------------------------- #
# ProductionSite — a physical operating location under a Farm
# --------------------------------------------------------------------- #
class ProductionSite(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "production_sites"
    __table_args__ = (UniqueConstraint("farm_id", "code", name="uq_site_farm_code"),)

    farm_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("farms.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    latitude: Mapped[float | None] = mapped_column(Numeric(10, 7), nullable=True)
    longitude: Mapped[float | None] = mapped_column(Numeric(10, 7), nullable=True)
    timezone: Mapped[str | None] = mapped_column(String(80), nullable=True)
    manager_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    capacity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[ProductionSiteStatus] = mapped_column(
        SQLEnum(
            ProductionSiteStatus,
            name="production_site_status",
            values_callable=lambda enum: [m.value for m in enum],
        ),
        nullable=False,
        default=ProductionSiteStatus.ACTIVE,
    )
    metadata_json: Mapped[dict | None] = mapped_column("metadata", _JSON, nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    farm: Mapped[Farm] = relationship("Farm")
    manager: Mapped[User | None] = relationship("User", foreign_keys=[manager_id])


# --------------------------------------------------------------------- #
# ProductionUnit — a single tank / pond / cage inside a site
# --------------------------------------------------------------------- #
class ProductionUnit(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "production_units"
    __table_args__ = (UniqueConstraint("site_id", "code", name="uq_unit_site_code"),)

    site_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("production_sites.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    unit_type_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("production_unit_types.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    capacity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[ProductionUnitStatus] = mapped_column(
        SQLEnum(
            ProductionUnitStatus,
            name="production_unit_status",
            values_callable=lambda enum: [m.value for m in enum],
        ),
        nullable=False,
        default=ProductionUnitStatus.ACTIVE,
    )
    metadata_json: Mapped[dict | None] = mapped_column("metadata", _JSON, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    site: Mapped[ProductionSite] = relationship("ProductionSite")
    unit_type: Mapped[ProductionUnitType] = relationship("ProductionUnitType")


# --------------------------------------------------------------------- #
# ProductionBatch — a stocking cycle in a single unit
# --------------------------------------------------------------------- #
class ProductionBatch(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "production_batches"
    __table_args__ = (
        UniqueConstraint("unit_id", "code", name="uq_batch_unit_code"),
        Index("ix_batches_state", "state"),
    )

    unit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("production_units.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[ProductionBatchState] = mapped_column(
        SQLEnum(
            ProductionBatchState,
            name="production_batch_state",
            values_callable=lambda enum: [m.value for m in enum],
        ),
        nullable=False,
        default=ProductionBatchState.PLANNED,
    )
    species: Mapped[str | None] = mapped_column(String(255), nullable=True)
    planned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    harvested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expected_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actual_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column("metadata", _JSON, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    unit: Mapped[ProductionUnit] = relationship("ProductionUnit")


# --------------------------------------------------------------------- #
# ProductionBatchTransition — append-only lifecycle history
# --------------------------------------------------------------------- #
class ProductionBatchTransition(Base, UUIDPrimaryKeyMixin):
    """Append-only record of every batch state change.

    Serves as an audit trail specifically for lifecycle transitions and
    complements the general ``audit_events`` log.
    """

    __tablename__ = "production_batch_transitions"
    __table_args__ = (Index("ix_batch_transitions_batch_occurred", "batch_id", "occurred_at"),)

    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("production_batches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    from_state: Mapped[ProductionBatchState | None] = mapped_column(
        SQLEnum(
            ProductionBatchState,
            name="production_batch_state",
            create_type=False,
            values_callable=lambda enum: [m.value for m in enum],
        ),
        nullable=True,  # NULL when creating the batch (into PLANNED)
    )
    to_state: Mapped[ProductionBatchState] = mapped_column(
        SQLEnum(
            ProductionBatchState,
            name="production_batch_state",
            create_type=False,
            values_callable=lambda enum: [m.value for m in enum],
        ),
        nullable=False,
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("production_events.id", ondelete="SET NULL"), nullable=True
    )
    reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    metadata_json: Mapped[dict | None] = mapped_column("metadata", _JSON, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# --------------------------------------------------------------------- #
# ProductionTransfer — durable identity for one atomic biological movement
# --------------------------------------------------------------------- #
class ProductionTransfer(Base, UUIDPrimaryKeyMixin):
    __tablename__ = "production_transfers"
    __table_args__ = (
        CheckConstraint(
            "source_batch_id <> destination_batch_id", name="ck_prod_transfer_batches_distinct"
        ),
        CheckConstraint(
            "source_unit_id <> destination_unit_id", name="ck_prod_transfer_units_distinct"
        ),
        CheckConstraint("quantity > 0", name="ck_prod_transfer_quantity_positive"),
        CheckConstraint("transfer_loss >= 0", name="ck_prod_transfer_loss_nonnegative"),
        Index(
            "uq_production_transfer_source_idempotency",
            "source_batch_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
            sqlite_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    farm_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("production_batches.id"), nullable=False
    )
    destination_batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("production_batches.id"), nullable=False
    )
    source_unit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("production_units.id"), nullable=False
    )
    destination_unit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("production_units.id"), nullable=False
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    transfer_loss: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# --------------------------------------------------------------------- #
# ProductionEvent — the append-only operational event log
# --------------------------------------------------------------------- #
class ProductionEvent(Base, UUIDPrimaryKeyMixin):
    """Append-only operational activity log.

    ``organization_id`` / ``farm_id`` / ``site_id`` are denormalised
    from the batch's parent chain so tenant filtering does not require
    joins — and so this table remains partition-ready without a
    disruptive redesign later.
    """

    __tablename__ = "production_events"
    __table_args__ = (
        # Composite index tuned for the cursor pagination pattern
        # ``ORDER BY performed_at DESC, id DESC`` scoped to a batch.
        Index("ix_events_batch_performed", "batch_id", "performed_at", "id"),
        Index("ix_events_unit_performed", "unit_id", "performed_at"),
        Index("ix_events_type_performed", "event_type", "performed_at"),
        Index("ix_events_org_performed", "organization_id", "performed_at"),
        Index("ix_events_farm_performed", "farm_id", "performed_at"),
        Index(
            "uq_production_event_transfer_role",
            "transfer_id",
            "transfer_role",
            unique=True,
            postgresql_where=text("transfer_id IS NOT NULL"),
            sqlite_where=text("transfer_id IS NOT NULL"),
        ),
        # Idempotency (Codex Review Gate 01, finding CRG01-2):
        # ``(batch_id, idempotency_key)`` must be unique when the key is
        # supplied by the client. The partial unique index guarantees at
        # most one row per key per batch even under concurrent retries.
        Index(
            "uq_events_batch_idempotency_key",
            "batch_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
            sqlite_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    farm_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("farms.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    site_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("production_sites.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    unit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("production_units.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("production_batches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    event_type_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    transfer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("production_transfers.id"), nullable=True, index=True
    )
    transfer_role: Mapped[ProductionTransferRole | None] = mapped_column(
        SQLEnum(
            ProductionTransferRole,
            name="production_transfer_role",
            create_type=False,
            values_callable=lambda enum: [m.value for m in enum],
        ),
        nullable=True,
    )
    performed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Partition-key candidate; also drives cursor pagination ordering.
    performed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    data: Mapped[dict] = mapped_column(_JSON, nullable=False)
    attachments: Mapped[list | None] = mapped_column(_JSON, nullable=True)
    is_final: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notes: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    # Idempotency support — see ``ProductionEventService.create``.
    # ``idempotency_key`` is optional; when supplied, the tuple
    # ``(batch_id, idempotency_key)`` is unique (partial index above).
    # ``payload_hash`` is a stable SHA-256 hex of the validated data
    # + event_type so that replaying the SAME key with a DIFFERENT
    # semantic request can be rejected as a conflict rather than silently
    # accepted.
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    audit_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("audit_events.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
