"""Repositories for Sprint 4 inventory."""

from __future__ import annotations

import base64
import binascii
import uuid
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.inventory import (
    InventoryItem,
    InventoryLot,
    InventoryTransaction,
    InventoryTransactionType,
    StorageLocation,
    Warehouse,
    WarehouseStatus,
)


def _encode_cursor(performed_at: datetime, tx_id: uuid.UUID) -> str:
    """Sprint 4.1 P2 Task 2 — opaque composite cursor.

    Encodes ``(performed_at, id)`` as ``base64("<iso>|<uuid>")`` so
    callers cannot introspect the position and we can evolve the
    format later without breaking clients.
    """
    raw = f"{performed_at.isoformat()}|{tx_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    """Inverse of :func:`_encode_cursor`. Any decoding failure → HTTP 400.

    Sprint 4.1 P2 Code Review (Medium) — every client-controlled failure
    mode must funnel into the documented ``400 invalid_cursor`` response
    so the endpoint never returns 500 for a garbage query parameter.
    Failure modes explicitly covered:

    * non-ASCII characters (``UnicodeEncodeError`` from ``encode('ascii')``),
    * invalid base64 padding / alphabet (``binascii.Error``),
    * base64 payload that is not valid UTF-8 (``UnicodeDecodeError``),
    * missing ``|`` delimiter (``ValueError`` from tuple unpacking),
    * malformed ISO-8601 timestamp (``ValueError`` from ``fromisoformat``),
    * malformed UUID (``ValueError`` from ``uuid.UUID``),
    * unexpected input type or unknown encoding (``TypeError`` /
      ``LookupError``).

    The exception message is NOT echoed back to the client so we do not
    leak internal parser details.
    """
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        ts_str, id_str = raw.split("|", 1)
        return datetime.fromisoformat(ts_str), uuid.UUID(id_str)
    except (ValueError, TypeError, LookupError, binascii.Error) as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            {"code": "invalid_cursor", "message": "Malformed pagination cursor."},
        ) from exc


# --------------------------------------------------------------------- #
# Sign map — INCREASE types add to the balance, DECREASE types subtract.
# REVERSAL is handled specially in the service (flip the referenced row).
# --------------------------------------------------------------------- #
_INCREASE = {
    InventoryTransactionType.RECEIPT,
    InventoryTransactionType.TRANSFER_IN,
    InventoryTransactionType.ADJUSTMENT_INCREASE,
}
_DECREASE = {
    InventoryTransactionType.ISSUE,
    InventoryTransactionType.CONSUMPTION,
    InventoryTransactionType.TRANSFER_OUT,
    InventoryTransactionType.ADJUSTMENT_DECREASE,
}


class WarehouseRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, wh_id: uuid.UUID) -> Warehouse | None:
        stmt = select(Warehouse).where(Warehouse.id == wh_id, Warehouse.deleted_at.is_(None))
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_by_ids_for_update(self, ids: Sequence[uuid.UUID]) -> list[Warehouse]:
        """Row-lock a set of warehouses in a single deterministic query.

        Sprint 5.4.6 — the transfer-reversal locking sequence must
        acquire warehouse locks in ascending id order to avoid
        cross-caller deadlocks. Using one query with
        ``ORDER BY id ASC FOR UPDATE`` guarantees the DB acquires
        the row locks in that order.
        """
        if not ids:
            return []
        stmt = (
            select(Warehouse)
            .where(Warehouse.id.in_(list(ids)))
            .order_by(Warehouse.id.asc())
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_for_org(
        self, org_id: uuid.UUID, *, operational_only: bool = False
    ) -> Sequence[Warehouse]:
        predicates = [Warehouse.organization_id == org_id, Warehouse.deleted_at.is_(None)]
        if operational_only:
            predicates.append(Warehouse.status != WarehouseStatus.CLOSED)
        stmt = select(Warehouse).where(*predicates).order_by(Warehouse.name)
        return list((await self.session.execute(stmt)).scalars().all())


class StorageLocationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, sl_id: uuid.UUID) -> StorageLocation | None:
        stmt = select(StorageLocation).where(
            StorageLocation.id == sl_id, StorageLocation.deleted_at.is_(None)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_warehouse(self, wh_id: uuid.UUID) -> Sequence[StorageLocation]:
        stmt = (
            select(StorageLocation)
            .where(StorageLocation.warehouse_id == wh_id, StorageLocation.deleted_at.is_(None))
            .order_by(StorageLocation.name)
        )
        return list((await self.session.execute(stmt)).scalars().all())


class InventoryItemRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, item_id: uuid.UUID) -> InventoryItem | None:
        stmt = select(InventoryItem).where(
            InventoryItem.id == item_id, InventoryItem.deleted_at.is_(None)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_by_ids_for_update(self, ids: Sequence[uuid.UUID]) -> list[InventoryItem]:
        """Row-lock a set of items in a single deterministic query.

        Sprint 5.4.6 — items are referenced by both sides of a
        transfer pair; the reversal locking sequence acquires them
        in ascending id order so concurrent reversals cannot
        deadlock over item locks.
        """
        if not ids:
            return []
        stmt = (
            select(InventoryItem)
            .where(InventoryItem.id.in_(list(ids)))
            .order_by(InventoryItem.id.asc())
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_for_org(
        self, org_id: uuid.UUID, *, operational_only: bool = False
    ) -> Sequence[InventoryItem]:
        predicates = [
            InventoryItem.organization_id == org_id,
            InventoryItem.deleted_at.is_(None),
        ]
        if operational_only:
            predicates.append(InventoryItem.is_active.is_(True))
        stmt = select(InventoryItem).where(*predicates).order_by(InventoryItem.name)
        return list((await self.session.execute(stmt)).scalars().all())


class InventoryLotRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, lot_id: uuid.UUID) -> InventoryLot | None:
        stmt = select(InventoryLot).where(
            InventoryLot.id == lot_id, InventoryLot.deleted_at.is_(None)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_id_for_update(self, lot_id: uuid.UUID) -> InventoryLot | None:
        """Row-lock the lot inside the current transaction.

        Every stock-affecting operation MUST call this before reading
        the balance. Postgres emits ``SELECT ... FOR UPDATE``; SQLite
        silently no-ops (StaticPool serialises writers).
        """
        stmt = (
            select(InventoryLot)
            .where(InventoryLot.id == lot_id, InventoryLot.deleted_at.is_(None))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_by_ids_for_update(self, ids: Sequence[uuid.UUID]) -> list[InventoryLot]:
        """Row-lock a set of lots in a single deterministic query.

        Sprint 5.4.6 — the reversal write phase must acquire both
        transfer lots in ascending id order to avoid AB / BA
        deadlocks between concurrent reversal attempts.
        """
        if not ids:
            return []
        stmt = (
            select(InventoryLot)
            .where(
                InventoryLot.id.in_(list(ids)),
                InventoryLot.deleted_at.is_(None),
            )
            .order_by(InventoryLot.id.asc())
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def find_or_none(
        self, *, warehouse_id: uuid.UUID, item_id: uuid.UUID, lot_code: str
    ) -> InventoryLot | None:
        stmt = select(InventoryLot).where(
            InventoryLot.warehouse_id == warehouse_id,
            InventoryLot.item_id == item_id,
            InventoryLot.lot_code == lot_code,
            InventoryLot.deleted_at.is_(None),
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_warehouse(self, wh_id: uuid.UUID) -> Sequence[InventoryLot]:
        stmt = (
            select(InventoryLot)
            .where(
                InventoryLot.warehouse_id == wh_id,
                InventoryLot.deleted_at.is_(None),
            )
            .order_by(InventoryLot.created_at.desc())
        )
        return list((await self.session.execute(stmt)).scalars().all())


class InventoryTransactionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, tx_id: uuid.UUID) -> InventoryTransaction | None:
        stmt = select(InventoryTransaction).where(InventoryTransaction.id == tx_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_id_for_update(self, tx_id: uuid.UUID) -> InventoryTransaction | None:
        """Row-lock a ledger row inside the current transaction.

        Sprint 5.4.5 — paired-transfer reversal MUST hold locks on
        BOTH participating transaction rows before trusting their
        relationship fields, otherwise a concurrent update to
        ``farm_id`` / ``item_id`` / ``reference_id`` between our
        unlocked validation and the write phase would leak a
        partial reversal past the invariants.

        Postgres emits ``SELECT ... FOR UPDATE``; SQLite silently
        no-ops (StaticPool serialises writers). ``populate_existing``
        forces the identity map to refresh from the DB even if the
        row was previously loaded — so callers always see the
        authoritative locked state.
        """
        stmt = (
            select(InventoryTransaction)
            .where(InventoryTransaction.id == tx_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_by_ids_for_update(self, ids: Sequence[uuid.UUID]) -> list[InventoryTransaction]:
        """Row-lock a set of ledger rows in a single deterministic query.

        Sprint 5.4.6 — the ONLY safe way to lock the two participating
        transactions of a transfer pair. The caller sorts the ids
        ascending; the DB acquires the locks in that order, so two
        concurrent reversal attempts on the same pair — targeting
        opposite sides — never deadlock. Postgres executes
        ``SELECT ... WHERE id IN (:sorted_ids) ORDER BY id ASC
        FOR UPDATE``; SQLite silently no-ops (StaticPool already
        serialises writers).
        """
        if not ids:
            return []
        stmt = (
            select(InventoryTransaction)
            .where(InventoryTransaction.id.in_(list(ids)))
            .order_by(InventoryTransaction.id.asc())
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_by_lot_and_key(
        self, lot_id: uuid.UUID, idempotency_key: str
    ) -> InventoryTransaction | None:
        stmt = select(InventoryTransaction).where(
            InventoryTransaction.lot_id == lot_id,
            InventoryTransaction.idempotency_key == idempotency_key,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_balance_in_canonical(self, lot_id: uuid.UUID) -> Decimal:
        """Return the current balance of ``lot_id`` in its item's
        canonical unit.

        This is the *authoritative* balance projection: every ledger
        row is normalised into the item's canonical unit at write
        time (see :class:`InventoryService`), so the balance is just
        a signed sum. Callers must hold the FOR UPDATE lock on the
        lot before invoking this for a decision.
        """
        stmt = select(
            func.coalesce(
                func.sum(
                    # Note: the DB stores unsigned quantities; sign is
                    # derived from the transaction type on read.
                    _signed_sql_expression()
                ),
                0,
            )
        ).where(InventoryTransaction.lot_id == lot_id)
        result = (await self.session.execute(stmt)).scalar_one()
        return Decimal(str(result))

    async def list_for_lot(
        self,
        lot_id: uuid.UUID,
        *,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[Sequence[InventoryTransaction], str | None]:
        """List a lot's ledger ordered ``performed_at DESC, id DESC``.

        Sprint 4.1 P2 Task 2 — the cursor is an opaque composite
        ``base64("<performed_at_iso>|<id>")``. It is applied as a
        strict tuple inequality so pagination is stable even when
        several rows share the same ``performed_at`` (common for
        transfers, which write two ledger rows at the same instant).
        """
        stmt = select(InventoryTransaction).where(InventoryTransaction.lot_id == lot_id)
        if cursor is not None:
            cursor_ts, cursor_id = _decode_cursor(cursor)
            stmt = stmt.where(
                tuple_(InventoryTransaction.performed_at, InventoryTransaction.id)
                < tuple_(cursor_ts, cursor_id)
            )
        stmt = stmt.order_by(
            InventoryTransaction.performed_at.desc(),
            InventoryTransaction.id.desc(),
        ).limit(limit + 1)
        rows = list((await self.session.execute(stmt)).scalars().all())
        next_cursor = (
            _encode_cursor(rows[limit - 1].performed_at, rows[limit - 1].id)
            if len(rows) > limit
            else None
        )
        return rows[:limit], next_cursor

    async def list_by_reference(
        self, reference_type: str, reference_id: uuid.UUID
    ) -> Sequence[InventoryTransaction]:
        stmt = select(InventoryTransaction).where(
            InventoryTransaction.reference_type == reference_type,
            InventoryTransaction.reference_id == reference_id,
        )
        return list((await self.session.execute(stmt)).scalars().all())


# --------------------------------------------------------------------- #
# SQL-side signed-sum expression
# --------------------------------------------------------------------- #
def _signed_sql_expression():
    """Return a SQLAlchemy CASE expression: +qty for increases, -qty for
    decreases, 0 for REVERSAL rows (their effect is delivered by the
    paired inverse row inserted at reversal time — we do NOT flip on
    read to keep the projection simple).
    """
    from sqlalchemy import case

    return case(
        (
            InventoryTransaction.transaction_type.in_([t.value for t in _INCREASE]),
            InventoryTransaction.quantity,
        ),
        (
            InventoryTransaction.transaction_type.in_([t.value for t in _DECREASE]),
            -InventoryTransaction.quantity,
        ),
        else_=0,
    )


__all__ = [
    "InventoryItemRepository",
    "InventoryLotRepository",
    "InventoryTransactionRepository",
    "StorageLocationRepository",
    "WarehouseRepository",
]
