"""Production Engine repositories.

One file for the whole bounded context so the aggregate boundaries
stay obvious. Query helpers here are intentionally thin — business
rules live in ``app.services.production``.
"""

from __future__ import annotations

import base64
import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.production import (
    ProductionBatch,
    ProductionBatchState,
    ProductionBatchTransition,
    ProductionEvent,
    ProductionSite,
    ProductionSiteStatus,
    ProductionTransfer,
    ProductionUnit,
    ProductionUnitStatus,
    ProductionUnitType,
)


# --------------------------------------------------------------------- #
# ProductionSite
# --------------------------------------------------------------------- #
class ProductionSiteRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, **kwargs) -> ProductionSite:
        site = ProductionSite(**kwargs)
        self.session.add(site)
        await self.session.flush()
        return site

    async def get_by_id(self, site_id: uuid.UUID) -> ProductionSite | None:
        stmt = select(ProductionSite).where(
            ProductionSite.id == site_id,
            ProductionSite.deleted_at.is_(None),
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_id_including_deleted(self, site_id: uuid.UUID) -> ProductionSite | None:
        return (
            await self.session.execute(select(ProductionSite).where(ProductionSite.id == site_id))
        ).scalar_one_or_none()

    async def list_for_farm(self, farm_id: uuid.UUID) -> Sequence[ProductionSite]:
        stmt = (
            select(ProductionSite)
            .where(ProductionSite.farm_id == farm_id, ProductionSite.deleted_at.is_(None))
            .order_by(ProductionSite.is_default.desc(), ProductionSite.name.asc())
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def count_active_units(self, site_id: uuid.UUID) -> int:
        stmt = select(func.count(ProductionUnit.id)).where(
            ProductionUnit.site_id == site_id,
            ProductionUnit.deleted_at.is_(None),
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def soft_delete(self, site: ProductionSite) -> None:
        now = datetime.now(UTC)
        site.deleted_at = now
        site.updated_at = now
        self.session.add(site)
        await self.session.flush()

    async def restore(self, site: ProductionSite) -> None:
        now = datetime.now(UTC)
        site.deleted_at = None
        site.updated_at = now
        self.session.add(site)
        await self.session.flush()


# --------------------------------------------------------------------- #
# ProductionUnitType
# --------------------------------------------------------------------- #
class ProductionUnitTypeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, **kwargs) -> ProductionUnitType:
        row = ProductionUnitType(**kwargs)
        self.session.add(row)
        await self.session.flush()
        return row

    async def get_by_id(self, id_: uuid.UUID) -> ProductionUnitType | None:
        stmt = select(ProductionUnitType).where(
            ProductionUnitType.id == id_,
            ProductionUnitType.deleted_at.is_(None),
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_visible(
        self, id_: uuid.UUID, *, organization_id: uuid.UUID | None
    ) -> ProductionUnitType | None:
        """Look up a unit type that this org is allowed to see (system or their own)."""
        stmt = select(ProductionUnitType).where(
            ProductionUnitType.id == id_,
            ProductionUnitType.deleted_at.is_(None),
            or_(
                ProductionUnitType.is_system.is_(True),
                ProductionUnitType.organization_id == organization_id,
            ),
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_visible(
        self, *, organization_ids: list[uuid.UUID] | None = None
    ) -> Sequence[ProductionUnitType]:
        """List system + org-custom unit types visible to the caller.

        ``organization_ids`` is the SET of orgs the caller belongs to
        (empty list means "system only"). Only org-custom types owned
        by one of those orgs are returned. Never accept an unverified
        ``organization_id`` from the request URL — the caller MUST
        derive the list from the caller's memberships.

        This closes the cross-tenant leak documented in
        ``docs/audits/codex-review-gate-01.md`` (finding CRG01-1).
        """
        conditions = [ProductionUnitType.is_system.is_(True)]
        if organization_ids:
            conditions.append(ProductionUnitType.organization_id.in_(organization_ids))
        stmt = (
            select(ProductionUnitType)
            .where(ProductionUnitType.deleted_at.is_(None), or_(*conditions))
            .order_by(
                ProductionUnitType.is_system.desc(),
                ProductionUnitType.category.asc(),
                ProductionUnitType.name.asc(),
            )
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def system_code_exists(self, code: str) -> bool:
        stmt = select(func.count(ProductionUnitType.id)).where(
            ProductionUnitType.code == code,
            ProductionUnitType.is_system.is_(True),
            ProductionUnitType.deleted_at.is_(None),
        )
        return int((await self.session.execute(stmt)).scalar_one()) > 0

    async def soft_delete(self, row: ProductionUnitType) -> None:
        now = datetime.now(UTC)
        row.deleted_at = now
        row.updated_at = now
        self.session.add(row)
        await self.session.flush()


# --------------------------------------------------------------------- #
# ProductionUnit
# --------------------------------------------------------------------- #
class ProductionUnitRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, **kwargs) -> ProductionUnit:
        unit = ProductionUnit(**kwargs)
        self.session.add(unit)
        await self.session.flush()
        return unit

    async def get_by_id(self, unit_id: uuid.UUID) -> ProductionUnit | None:
        stmt = select(ProductionUnit).where(
            ProductionUnit.id == unit_id, ProductionUnit.deleted_at.is_(None)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_site_and_code(self, *, site_id: uuid.UUID, code: str) -> ProductionUnit | None:
        stmt = select(ProductionUnit).where(
            ProductionUnit.site_id == site_id,
            ProductionUnit.code == code,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_site(self, site_id: uuid.UUID) -> Sequence[ProductionUnit]:
        stmt = (
            select(ProductionUnit)
            .where(ProductionUnit.site_id == site_id, ProductionUnit.deleted_at.is_(None))
            .order_by(ProductionUnit.name.asc())
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def list_eligible_transfer_destinations(
        self, *, farm_id: uuid.UUID, exclude_unit_id: uuid.UUID
    ) -> Sequence[tuple[ProductionUnit, ProductionSite]]:
        """Return active transfer destinations inside an authoritative farm scope."""
        stmt = (
            select(ProductionUnit, ProductionSite)
            .join(ProductionSite, ProductionSite.id == ProductionUnit.site_id)
            .where(
                ProductionSite.farm_id == farm_id,
                ProductionSite.deleted_at.is_(None),
                ProductionSite.status == ProductionSiteStatus.ACTIVE,
                ProductionUnit.id != exclude_unit_id,
                ProductionUnit.deleted_at.is_(None),
                ProductionUnit.status == ProductionUnitStatus.ACTIVE,
            )
            .order_by(
                ProductionSite.code.asc(),
                ProductionUnit.code.asc(),
                ProductionUnit.name.asc(),
            )
        )
        return (await self.session.execute(stmt)).all()

    async def get_eligible_transfer_destination(
        self, *, unit_id: uuid.UUID, farm_id: uuid.UUID, exclude_unit_id: uuid.UUID
    ) -> tuple[ProductionUnit, ProductionSite] | None:
        """Resolve one destination without looking outside the authoritative farm."""
        stmt = (
            select(ProductionUnit, ProductionSite)
            .join(ProductionSite, ProductionSite.id == ProductionUnit.site_id)
            .where(
                ProductionUnit.id == unit_id,
                ProductionSite.farm_id == farm_id,
                ProductionSite.deleted_at.is_(None),
                ProductionSite.status == ProductionSiteStatus.ACTIVE,
                ProductionUnit.id != exclude_unit_id,
                ProductionUnit.deleted_at.is_(None),
                ProductionUnit.status == ProductionUnitStatus.ACTIVE,
            )
        )
        row = (await self.session.execute(stmt)).one_or_none()
        return (row[0], row[1]) if row is not None else None

    async def soft_delete(self, unit: ProductionUnit) -> None:
        now = datetime.now(UTC)
        unit.deleted_at = now
        unit.updated_at = now
        self.session.add(unit)
        await self.session.flush()

    async def count_active_batches(self, unit_id: uuid.UUID) -> int:
        stmt = select(func.count(ProductionBatch.id)).where(
            ProductionBatch.unit_id == unit_id,
            ProductionBatch.deleted_at.is_(None),
            ProductionBatch.state.notin_(
                [
                    ProductionBatchState.CLOSED,
                    ProductionBatchState.CANCELLED,
                    ProductionBatchState.FAILED,
                ]
            ),
        )
        return int((await self.session.execute(stmt)).scalar_one())


# --------------------------------------------------------------------- #
# ProductionBatch (state changes here are RAW; use service for transitions)
# --------------------------------------------------------------------- #
class ProductionBatchRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, **kwargs) -> ProductionBatch:
        batch = ProductionBatch(**kwargs)
        self.session.add(batch)
        await self.session.flush()
        return batch

    async def get_by_id(self, batch_id: uuid.UUID) -> ProductionBatch | None:
        stmt = select(ProductionBatch).where(
            ProductionBatch.id == batch_id, ProductionBatch.deleted_at.is_(None)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_id_for_update(self, batch_id: uuid.UUID) -> ProductionBatch | None:
        """Locked read of a batch row inside the current transaction.

        Emits ``SELECT ... FOR UPDATE`` on Postgres so any concurrent
        event insertion, mortality/transfer/harvest validation, and
        lifecycle transition on the same batch serialises behind this
        lock. On SQLite the ``with_for_update`` clause is a no-op — the
        driver already serialises writers, so the domain-level guards
        remain correct.

        Callers MUST use this INSIDE the request-scoped transaction
        (i.e. before any subsequent read of the batch state used for
        validation).
        """
        # ``populate_existing`` forces SQLAlchemy to refresh the ORM
        # attributes for the returned row even if the identity map
        # already contains it — otherwise the lock buys us serialisation
        # but the batch.state we validate against could be stale from
        # an earlier read in the same transaction.
        stmt = (
            select(ProductionBatch)
            .where(ProductionBatch.id == batch_id, ProductionBatch.deleted_at.is_(None))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def lock_pair(
        self, first: uuid.UUID, second: uuid.UUID
    ) -> dict[uuid.UUID, ProductionBatch]:
        """Lock both batches in UUID order so opposite transfers cannot deadlock."""
        ids = sorted((first, second), key=str)
        stmt = (
            select(ProductionBatch)
            .where(ProductionBatch.id.in_(ids), ProductionBatch.deleted_at.is_(None))
            .order_by(ProductionBatch.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return {row.id: row for row in rows}

    async def list_eligible_transfer_batches(
        self, *, farm_id: uuid.UUID, exclude_batch_id: uuid.UUID, exclude_unit_id: uuid.UUID
    ) -> Sequence[tuple[ProductionBatch, ProductionUnit, ProductionSite]]:
        stmt = (
            select(ProductionBatch, ProductionUnit, ProductionSite)
            .join(ProductionUnit, ProductionUnit.id == ProductionBatch.unit_id)
            .join(ProductionSite, ProductionSite.id == ProductionUnit.site_id)
            .where(
                ProductionSite.farm_id == farm_id,
                ProductionSite.deleted_at.is_(None),
                ProductionSite.status == ProductionSiteStatus.ACTIVE,
                ProductionUnit.id != exclude_unit_id,
                ProductionUnit.deleted_at.is_(None),
                ProductionUnit.status == ProductionUnitStatus.ACTIVE,
                ProductionBatch.id != exclude_batch_id,
                ProductionBatch.deleted_at.is_(None),
                ProductionBatch.state.in_(
                    [ProductionBatchState.STOCKED, ProductionBatchState.ACTIVE]
                ),
            )
            .order_by(ProductionSite.code, ProductionUnit.code, ProductionBatch.code)
        )
        rows = (await self.session.execute(stmt)).all()
        return [(row[0], row[1], row[2]) for row in rows]

    async def resolve_transfer_destination(
        self, *, batch_id: uuid.UUID, unit_id: uuid.UUID, farm_id: uuid.UUID
    ) -> tuple[ProductionBatch, ProductionUnit, ProductionSite] | None:
        stmt = (
            select(ProductionBatch, ProductionUnit, ProductionSite)
            .join(ProductionUnit, ProductionUnit.id == ProductionBatch.unit_id)
            .join(ProductionSite, ProductionSite.id == ProductionUnit.site_id)
            .where(
                ProductionBatch.id == batch_id,
                ProductionBatch.unit_id == unit_id,
                ProductionBatch.deleted_at.is_(None),
                ProductionSite.farm_id == farm_id,
            )
        )
        row = (await self.session.execute(stmt)).one_or_none()
        return tuple(row) if row else None

    async def list_for_unit(self, unit_id: uuid.UUID) -> Sequence[ProductionBatch]:
        stmt = (
            select(ProductionBatch)
            .where(ProductionBatch.unit_id == unit_id, ProductionBatch.deleted_at.is_(None))
            .order_by(ProductionBatch.created_at.desc())
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def compare_and_set_state(
        self,
        batch_id: uuid.UUID,
        *,
        from_state: ProductionBatchState,
        to_state: ProductionBatchState,
        timestamp_fields: dict[str, datetime] | None = None,
    ) -> bool:
        """CAS state transition.

        Returns ``True`` iff the update actually flipped the state — the
        safe primitive for concurrent transitions. ``timestamp_fields``
        lets callers set ``stocked_at`` / ``harvested_at`` / ``closed_at``
        atomically with the state change.
        """
        values: dict = {"state": to_state}
        if timestamp_fields:
            values.update(timestamp_fields)
        stmt = (
            update(ProductionBatch)
            .where(ProductionBatch.id == batch_id, ProductionBatch.state == from_state)
            .values(**values)
        )
        result = await self.session.execute(stmt)
        await self.session.flush()
        return (result.rowcount or 0) == 1


# --------------------------------------------------------------------- #
# ProductionBatchTransition (append-only)
# --------------------------------------------------------------------- #
class ProductionBatchTransitionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record(
        self,
        *,
        batch_id: uuid.UUID,
        from_state: ProductionBatchState | None,
        to_state: ProductionBatchState,
        actor_id: uuid.UUID | None,
        event_id: uuid.UUID | None = None,
        reason: str | None = None,
        metadata: dict | None = None,
    ) -> ProductionBatchTransition:
        now = datetime.now(UTC)
        row = ProductionBatchTransition(
            batch_id=batch_id,
            from_state=from_state,
            to_state=to_state,
            actor_id=actor_id,
            event_id=event_id,
            reason=reason,
            metadata_json=metadata,
            occurred_at=now,
            created_at=now,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def list_for_batch(self, batch_id: uuid.UUID) -> Sequence[ProductionBatchTransition]:
        stmt = (
            select(ProductionBatchTransition)
            .where(ProductionBatchTransition.batch_id == batch_id)
            .order_by(
                ProductionBatchTransition.occurred_at.asc(),
                ProductionBatchTransition.id.asc(),
            )
        )
        return (await self.session.execute(stmt)).scalars().all()


# --------------------------------------------------------------------- #
# ProductionEvent (append-only, cursor-paginated)
# --------------------------------------------------------------------- #
def _encode_cursor(performed_at: datetime, row_id: uuid.UUID) -> str:
    payload = json.dumps(
        {"t": performed_at.isoformat(), "i": str(row_id)},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    pad = "=" * (-len(cursor) % 4)
    payload = json.loads(base64.urlsafe_b64decode(cursor + pad).decode())
    return datetime.fromisoformat(payload["t"]), uuid.UUID(payload["i"])


class ProductionEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, **kwargs) -> ProductionEvent:
        now = datetime.now(UTC)
        kwargs.setdefault("created_at", now)
        kwargs.setdefault("performed_at", now)
        row = ProductionEvent(**kwargs)
        self.session.add(row)
        await self.session.flush()
        return row

    async def create_transfer(self, **kwargs: object) -> ProductionTransfer:
        row = ProductionTransfer(created_at=datetime.now(UTC), **kwargs)
        self.session.add(row)
        await self.session.flush()
        return row

    async def get_transfer_by_source_key(
        self, source_batch_id: uuid.UUID, idempotency_key: str
    ) -> ProductionTransfer | None:
        stmt = select(ProductionTransfer).where(
            ProductionTransfer.source_batch_id == source_batch_id,
            ProductionTransfer.idempotency_key == idempotency_key,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_transfer_event(self, transfer_id: uuid.UUID, role: str) -> ProductionEvent | None:
        stmt = select(ProductionEvent).where(
            ProductionEvent.transfer_id == transfer_id,
            ProductionEvent.transfer_role == role,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_id(self, event_id: uuid.UUID) -> ProductionEvent | None:
        return (
            await self.session.execute(
                select(ProductionEvent).where(ProductionEvent.id == event_id)
            )
        ).scalar_one_or_none()

    async def get_by_batch_and_key(
        self, batch_id: uuid.UUID, idempotency_key: str
    ) -> ProductionEvent | None:
        """Lookup used for idempotent replay handling.

        Paired with the partial unique index
        ``uq_events_batch_idempotency_key`` in the ORM schema.
        """
        stmt = select(ProductionEvent).where(
            ProductionEvent.batch_id == batch_id,
            ProductionEvent.idempotency_key == idempotency_key,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_batch(
        self,
        batch_id: uuid.UUID,
        *,
        limit: int,
        cursor: str | None,
        event_type: str | None = None,
    ) -> tuple[list[ProductionEvent], str | None]:
        """Cursor-paginated event listing.

        Ordering is ``(performed_at DESC, id DESC)`` so pagination is
        stable even when timestamps collide.
        """
        conditions = [ProductionEvent.batch_id == batch_id]
        if event_type:
            conditions.append(ProductionEvent.event_type == event_type.upper())
        if cursor:
            performed_at, row_id = _decode_cursor(cursor)
            conditions.append(
                or_(
                    ProductionEvent.performed_at < performed_at,
                    and_(
                        ProductionEvent.performed_at == performed_at,
                        ProductionEvent.id < row_id,
                    ),
                )
            )
        stmt = (
            select(ProductionEvent)
            .where(*conditions)
            .order_by(ProductionEvent.performed_at.desc(), ProductionEvent.id.desc())
            .limit(limit + 1)
        )
        rows = list((await self.session.execute(stmt)).scalars().all())
        next_cursor: str | None = None
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = _encode_cursor(last.performed_at, last.id)
            rows = rows[:limit]
        return rows, next_cursor

    async def list_all_for_batch_asc(self, batch_id: uuid.UUID) -> list[ProductionEvent]:
        """Return every event in authoritative projection order (asc).

        ``performed_at`` is operator chronology. Persisted, immutable
        ``created_at`` is insertion chronology for equal performed times;
        ``id`` is only a final deterministic tie-breaker.
        """
        stmt = (
            select(ProductionEvent)
            .where(ProductionEvent.batch_id == batch_id)
            .order_by(
                ProductionEvent.performed_at.asc(),
                ProductionEvent.created_at.asc(),
                ProductionEvent.id.asc(),
            )
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def count_by_type(self, batch_id: uuid.UUID, event_type: str) -> int:
        stmt = select(func.count(ProductionEvent.id)).where(
            ProductionEvent.batch_id == batch_id,
            ProductionEvent.event_type == event_type.upper(),
        )
        return int((await self.session.execute(stmt)).scalar_one())

    async def has_final_harvest(self, batch_id: uuid.UUID) -> bool:
        stmt = select(func.count(ProductionEvent.id)).where(
            ProductionEvent.batch_id == batch_id,
            ProductionEvent.event_type == "HARVEST",
            ProductionEvent.is_final.is_(True),
        )
        return int((await self.session.execute(stmt)).scalar_one()) > 0
