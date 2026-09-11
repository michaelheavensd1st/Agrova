"""Release 6.0.4 Sprint 4.1 Purchase Receipt domain tests."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.audit import AuditEvent
from app.models.business_partner import BusinessPartner
from app.models.farm import Farm
from app.models.inventory import (
    InventoryItem,
    InventoryItemCategory,
    InventoryLot,
    InventoryTransaction,
    InventoryTransactionType,
    StockUnit,
    Warehouse,
    WarehouseStatus,
)
from app.models.membership import FarmMembership, OrganizationMembership
from app.models.organization import Organization
from app.models.purchase_order import (
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderStatus,
    PurchaseOrderTransition,
)
from app.models.purchase_receipt import PurchaseReceipt, PurchaseReceiptLine
from app.models.role import Role
from app.models.role_assignment import RoleAssignment
from app.models.user import User
from app.repositories.purchase_receipt import PurchaseReceiptSequenceRepository
from app.schemas.purchase_receipt import PurchaseReceiptCommand
from app.security.permissions import ROLE_DEFINITIONS
from app.services.inventory import InventoryService
from app.services.purchase_receipt import PurchaseReceiptService

pytestmark = pytest.mark.asyncio

_postgres_only = pytest.mark.skipif(
    os.environ.get("DATABASE_URL", "").startswith("sqlite"),
    reason="PostgreSQL row locks, triggers, and concurrent sessions are required",
)


async def _seed(session, *, two_lines: bool = False, farm_warehouse: bool = True):
    org = Organization(name="Receipt Org", slug=f"receipt-{uuid4().hex}", is_active=True)
    actor = User(
        email=f"receipt-{uuid4().hex}@example.test",
        hashed_password="x",
        full_name="Receiver",
        is_active=True,
        is_verified=True,
        is_superuser=True,
    )
    session.add_all([org, actor])
    await session.flush()
    farm = Farm(organization_id=org.id, name="Farm", code=f"F-{uuid4().hex[:8]}", is_active=True)
    partner = BusinessPartner(
        organization_id=org.id,
        code=f"SUP-{uuid4().hex[:8]}",
        legal_name="Supplier",
        is_active=True,
    )
    session.add_all([farm, partner])
    await session.flush()
    items = [
        InventoryItem(
            organization_id=org.id,
            code=f"ITEM-{uuid4().hex[:8]}",
            name="Feed",
            category=InventoryItemCategory.FEED,
            canonical_unit=StockUnit.KG,
            is_active=True,
        )
    ]
    if two_lines:
        items.append(
            InventoryItem(
                organization_id=org.id,
                code=f"ITEM-{uuid4().hex[:8]}",
                name="Second feed",
                category=InventoryItemCategory.FEED,
                canonical_unit=StockUnit.KG,
                is_active=True,
            )
        )
    warehouse = Warehouse(
        organization_id=org.id,
        farm_id=farm.id if farm_warehouse else None,
        name="Main",
        code=f"WH-{uuid4().hex[:8]}",
        status=WarehouseStatus.ACTIVE,
    )
    session.add_all([*items, warehouse])
    await session.flush()
    po = PurchaseOrder(
        organization_id=org.id,
        farm_id=farm.id,
        business_partner_id=partner.id,
        po_number=f"PO-2026-{uuid4().int % 999999:06d}",
        status=PurchaseOrderStatus.APPROVED,
        currency_code="USD",
        order_date=date(2026, 8, 1),
        supplier_code=partner.code,
        supplier_legal_name=partner.legal_name,
        created_by_id=actor.id,
        approved_by_id=actor.id,
        approved_at=datetime.now(UTC),
        version=3,
    )
    session.add(po)
    await session.flush()
    lines = []
    for number, item in enumerate(items, 1):
        line = PurchaseOrderLine(
            purchase_order_id=po.id,
            line_number=number,
            inventory_item_id=item.id,
            item_code=item.code,
            item_name=item.name,
            description=item.name,
            ordered_quantity=Decimal("10.000000"),
            ordered_unit="kg",
            canonical_unit="kg",
            ordered_quantity_canonical=Decimal("10.000000"),
            received_quantity=Decimal("0.000000"),
            received_quantity_canonical=Decimal("0.000000"),
            unit_price=Decimal("2.500000"),
        )
        lines.append(line)
    session.add_all(lines)
    await session.commit()
    return org, actor, farm, warehouse, po, lines, items


def _command(warehouse, lines, quantities, *, lots=None):
    lots = lots or [f"LOT-{index}" for index in range(len(quantities))]
    return PurchaseReceiptCommand.model_validate(
        {
            "warehouse_id": warehouse.id,
            "received_at": "2026-08-10T12:00:00Z",
            "notes": "Dock receipt",
            "lines": [
                {
                    "purchase_order_line_id": lines[index].id,
                    "lot_code": lots[index],
                    "quantity": quantity,
                }
                for index, quantity in enumerate(quantities)
            ],
        }
    )


async def test_partial_complete_and_exact_replay(db_session):
    org, actor, _farm, warehouse, po, lines, _items = await _seed(db_session)
    service = PurchaseReceiptService(db_session)
    first, replay = await service.post(
        actor=actor,
        organization_id=org.id,
        purchase_order_id=po.id,
        command=_command(warehouse, lines, ["4.000000"]),
        idempotency_key="receipt-key-1",
        request_ctx={},
    )
    assert replay is False
    assert first.grn == "GRN-2026-000001"
    assert po.status == PurchaseOrderStatus.PARTIALLY_RECEIVED
    assert Decimal(lines[0].received_quantity) == Decimal("4.000000")
    transaction_count = await db_session.scalar(
        select(func.count()).select_from(InventoryTransaction)
    )

    replayed, replay = await service.post(
        actor=actor,
        organization_id=org.id,
        purchase_order_id=po.id,
        command=_command(warehouse, lines, ["4.000000"]),
        idempotency_key="receipt-key-1",
        request_ctx={},
    )
    assert replay is True
    assert replayed.id == first.id
    assert (
        await db_session.scalar(select(func.count()).select_from(InventoryTransaction))
        == transaction_count
    )

    second, replay = await service.post(
        actor=actor,
        organization_id=org.id,
        purchase_order_id=po.id,
        command=_command(warehouse, lines, ["6.000000"], lots=["LOT-FINAL"]),
        idempotency_key="receipt-key-2",
        request_ctx={},
    )
    assert replay is False
    assert second.grn == "GRN-2026-000002"
    assert po.status == PurchaseOrderStatus.RECEIVED
    assert Decimal(lines[0].received_quantity_canonical) == Decimal("10.000000")


async def test_multi_line_multi_lot_traceability_and_audit(db_session):
    org, actor, _farm, warehouse, po, lines, _items = await _seed(db_session, two_lines=True)
    command = PurchaseReceiptCommand.model_validate(
        {
            "warehouse_id": warehouse.id,
            "lines": [
                {"purchase_order_line_id": lines[0].id, "lot_code": "A", "quantity": "2.000000"},
                {"purchase_order_line_id": lines[0].id, "lot_code": "B", "quantity": "3.000000"},
                {"purchase_order_line_id": lines[1].id, "lot_code": "C", "quantity": "10.000000"},
            ],
        }
    )
    receipt, _ = await PurchaseReceiptService(db_session).post(
        actor=actor,
        organization_id=org.id,
        purchase_order_id=po.id,
        command=command,
        idempotency_key="multi-lot",
        request_ctx={"request_id": "request-1"},
    )
    assert len(receipt.lines) == 3
    assert len({line.inventory_transaction_id for line in receipt.lines}) == 3
    for line in receipt.lines:
        transaction = await db_session.get(InventoryTransaction, line.inventory_transaction_id)
        assert transaction is not None
        assert transaction.reference_type == "purchase_receipt_line"
        assert transaction.reference_id == line.id
        assert transaction.transaction_type == InventoryTransactionType.RECEIPT
    assert Decimal(lines[0].received_quantity) == Decimal("5.000000")
    assert Decimal(lines[1].received_quantity) == Decimal("10.000000")
    assert po.status == PurchaseOrderStatus.PARTIALLY_RECEIVED
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(AuditEvent.action == "purchase_receipt.post")
        )
        == 1
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(PurchaseOrderTransition)
            .where(PurchaseOrderTransition.purchase_order_id == po.id)
        )
        == 1
    )


async def test_over_receipt_conflict_and_key_payload_conflict(db_session):
    org, actor, _farm, warehouse, po, lines, _items = await _seed(db_session)
    service = PurchaseReceiptService(db_session)
    await service.post(
        actor=actor,
        organization_id=org.id,
        purchase_order_id=po.id,
        command=_command(warehouse, lines, ["8.000000"]),
        idempotency_key="fixed-key",
        request_ctx={},
    )
    with pytest.raises(HTTPException) as conflict:
        await service.post(
            actor=actor,
            organization_id=org.id,
            purchase_order_id=po.id,
            command=_command(warehouse, lines, ["9.000000"]),
            idempotency_key="fixed-key",
            request_ctx={},
        )
    assert conflict.value.detail["code"] == "idempotency_key_payload_conflict"
    with pytest.raises(HTTPException) as over:
        await service.post(
            actor=actor,
            organization_id=org.id,
            purchase_order_id=po.id,
            command=_command(warehouse, lines, ["3.000000"], lots=["OTHER"]),
            idempotency_key="another-key",
            request_ctx={},
        )
    assert over.value.detail["code"] == "purchase_order_over_receipt"
    assert Decimal(lines[0].received_quantity) == Decimal("8.000000")


async def test_warehouse_lifecycle_and_farm_contract(db_session):
    org, actor, farm, warehouse, po, lines, _items = await _seed(db_session)
    warehouse.status = WarehouseStatus.MAINTENANCE
    receipt, _ = await PurchaseReceiptService(db_session).post(
        actor=actor,
        organization_id=org.id,
        purchase_order_id=po.id,
        command=_command(warehouse, lines, ["1.000000"]),
        idempotency_key="maintenance",
        request_ctx={},
    )
    assert receipt.farm_id == farm.id

    other_farm = Farm(
        organization_id=org.id, name="Other", code=f"O-{uuid4().hex[:8]}", is_active=True
    )
    db_session.add(other_farm)
    await db_session.flush()
    warehouse.farm_id = other_farm.id
    with pytest.raises(HTTPException) as mismatch:
        await PurchaseReceiptService(db_session).post(
            actor=actor,
            organization_id=org.id,
            purchase_order_id=po.id,
            command=_command(warehouse, lines, ["1.000000"], lots=["MISMATCH"]),
            idempotency_key="mismatch",
            request_ctx={},
        )
    assert mismatch.value.detail["code"] == "warehouse_farm_scope_mismatch"
    warehouse.farm_id = farm.id
    warehouse.status = WarehouseStatus.CLOSED
    with pytest.raises(HTTPException) as closed:
        await PurchaseReceiptService(db_session).post(
            actor=actor,
            organization_id=org.id,
            purchase_order_id=po.id,
            command=_command(warehouse, lines, ["1.000000"], lots=["CLOSED"]),
            idempotency_key="closed",
            request_ctx={},
        )
    assert closed.value.detail["code"] == "warehouse_unavailable"


async def test_farm_scoped_receiving_hides_inaccessible_warehouses(db_session):
    org, actor, farm, warehouse, po, lines, _items = await _seed(db_session)
    actor.is_superuser = False
    role = (await db_session.execute(select(Role).where(Role.name == "storekeeper"))).scalar_one()
    db_session.add_all(
        [
            OrganizationMembership(user_id=actor.id, organization_id=org.id, is_active=True),
            FarmMembership(user_id=actor.id, farm_id=farm.id, is_active=True),
            RoleAssignment(
                user_id=actor.id,
                role_id=role.id,
                organization_id=org.id,
                farm_id=farm.id,
                granted_by_id=actor.id,
            ),
        ]
    )
    other_farm = Farm(
        organization_id=org.id,
        name="Hidden farm",
        code=f"H-{uuid4().hex[:8]}",
        is_active=True,
    )
    foreign_org = Organization(
        name="Foreign receipt org", slug=f"foreign-receipt-{uuid4().hex}", is_active=True
    )
    db_session.add_all([other_farm, foreign_org])
    await db_session.flush()
    cross_farm = Warehouse(
        organization_id=org.id,
        farm_id=other_farm.id,
        name="Hidden warehouse",
        code=f"HW-{uuid4().hex[:8]}",
        status=WarehouseStatus.ACTIVE,
    )
    foreign = Warehouse(
        organization_id=foreign_org.id,
        name="Foreign warehouse",
        code=f"FW-{uuid4().hex[:8]}",
        status=WarehouseStatus.ACTIVE,
    )
    deleted = Warehouse(
        organization_id=org.id,
        farm_id=farm.id,
        name="Deleted warehouse",
        code=f"DW-{uuid4().hex[:8]}",
        status=WarehouseStatus.ACTIVE,
        deleted_at=datetime.now(UTC),
    )
    shared = Warehouse(
        organization_id=org.id,
        farm_id=None,
        name="Shared warehouse",
        code=f"SW-{uuid4().hex[:8]}",
        status=WarehouseStatus.ACTIVE,
    )
    db_session.add_all([cross_farm, foreign, deleted, shared])
    await db_session.flush()

    hidden_ids = [uuid4(), deleted.id, foreign.id, cross_farm.id]
    hidden_responses = []
    for index, warehouse_id in enumerate(hidden_ids):
        command = _command(warehouse, lines, ["1.000000"], lots=[f"HIDDEN-{index}"])
        command.warehouse_id = warehouse_id
        with pytest.raises(HTTPException) as exc:
            await PurchaseReceiptService(db_session).post(
                actor=actor,
                organization_id=org.id,
                purchase_order_id=po.id,
                command=command,
                idempotency_key=f"hidden-{index}",
                request_ctx={},
            )
        hidden_responses.append((exc.value.status_code, exc.value.detail))
    assert all(response == hidden_responses[0] for response in hidden_responses)

    same_farm_receipt, _ = await PurchaseReceiptService(db_session).post(
        actor=actor,
        organization_id=org.id,
        purchase_order_id=po.id,
        command=_command(warehouse, lines, ["1.000000"], lots=["SAME-FARM"]),
        idempotency_key="same-farm",
        request_ctx={},
    )
    assert same_farm_receipt.warehouse_id == warehouse.id
    shared_receipt, _ = await PurchaseReceiptService(db_session).post(
        actor=actor,
        organization_id=org.id,
        purchase_order_id=po.id,
        command=_command(shared, lines, ["1.000000"], lots=["SHARED"]),
        idempotency_key="shared",
        request_ctx={},
    )
    assert shared_receipt.warehouse_id == shared.id


async def test_decimal_conversion_lot_reuse_and_positive_schema(db_session):
    org, actor, _farm, warehouse, po, lines, items = await _seed(db_session)
    lines[0].ordered_quantity = Decimal("1000.000000")
    lines[0].ordered_unit = "g"
    lines[0].ordered_quantity_canonical = Decimal("1.000000")
    command = _command(warehouse, lines, ["500.000000"], lots=["REUSED"])
    first, _ = await PurchaseReceiptService(db_session).post(
        actor=actor,
        organization_id=org.id,
        purchase_order_id=po.id,
        command=command,
        idempotency_key="grams-1",
        request_ctx={},
    )
    assert Decimal(first.lines[0].quantity_canonical) == Decimal("0.500000")
    second, _ = await PurchaseReceiptService(db_session).post(
        actor=actor,
        organization_id=org.id,
        purchase_order_id=po.id,
        command=_command(warehouse, lines, ["500.000000"], lots=["REUSED"]),
        idempotency_key="grams-2",
        request_ctx={},
    )
    assert second.lines[0].inventory_lot_id == first.lines[0].inventory_lot_id
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(InventoryLot)
            .where(InventoryLot.item_id == items[0].id)
        )
        == 1
    )
    with pytest.raises(ValidationError):
        _command(warehouse, lines, ["0"])
    with pytest.raises(ValidationError):
        _command(warehouse, lines, [-1.0])


async def test_lot_code_is_normalized_and_cannot_be_blank(db_session):
    _org, _actor, _farm, warehouse, _po, lines, _items = await _seed(db_session)
    with pytest.raises(ValidationError):
        _command(warehouse, lines, ["1.000000"], lots=[" "])

    ordinary = _command(warehouse, lines, ["1.000000"], lots=["LOT-ORDINARY"])
    padded = _command(warehouse, lines, ["1.000000"], lots=["  LOT-PADDED  "])
    assert ordinary.lines[0].lot_code == "LOT-ORDINARY"
    assert padded.lines[0].lot_code == "LOT-PADDED"


async def test_canonical_conversion_is_lossless_and_split_residual_is_exact(db_session):
    org, actor, _farm, warehouse, po, lines, _items = await _seed(db_session)
    line = lines[0]
    line.ordered_quantity = Decimal("0.002000")
    line.ordered_unit = "g"
    line.ordered_quantity_canonical = Decimal("0.000002")
    await db_session.flush()
    service = PurchaseReceiptService(db_session)

    for key, quantity in (
        ("round-up", "0.001500"),
        ("round-down", "0.000500"),
        ("round-zero", "0.000001"),
    ):
        with pytest.raises(HTTPException) as exc:
            await service.post(
                actor=actor,
                organization_id=org.id,
                purchase_order_id=po.id,
                command=_command(warehouse, lines, [quantity], lots=[key]),
                idempotency_key=key,
                request_ctx={},
            )
        assert exc.value.detail["code"] == "canonical_quantity_not_representable"

    first, _ = await service.post(
        actor=actor,
        organization_id=org.id,
        purchase_order_id=po.id,
        command=_command(warehouse, lines, ["0.001000"], lots=["exact-a"]),
        idempotency_key="exact-a",
        request_ctx={},
    )
    assert first.lines[0].quantity_canonical == Decimal("0.000001")
    first_tx = await db_session.get(InventoryTransaction, first.lines[0].inventory_transaction_id)
    assert Decimal(first_tx.quantity) == Decimal("0.000001")
    assert Decimal(line.received_quantity) == Decimal("0.001000")
    assert Decimal(line.received_quantity_canonical) == Decimal("0.000001")
    assert po.status == PurchaseOrderStatus.PARTIALLY_RECEIVED

    await service.post(
        actor=actor,
        organization_id=org.id,
        purchase_order_id=po.id,
        command=_command(warehouse, lines, ["0.001000"], lots=["exact-b"]),
        idempotency_key="exact-b",
        request_ctx={},
    )
    assert Decimal(line.received_quantity) == Decimal("0.002000")
    assert Decimal(line.received_quantity_canonical) == Decimal("0.000002")
    assert po.status == PurchaseOrderStatus.RECEIVED


@_postgres_only
async def test_whole_request_rolls_back_after_second_ledger_failure(
    db_session, _engine, monkeypatch
):
    org, actor, _farm, warehouse, po, lines, _items = await _seed(db_session, two_lines=True)
    po_id = po.id
    identifiers = (org.id, actor.id, warehouse.id, po.id, *(line.id for line in lines))
    factory = async_sessionmaker(_engine, expire_on_commit=False)
    with pytest.raises(RuntimeError, match="injected"):
        async with factory() as failing_session, failing_session.begin():
            org_id, actor_id, warehouse_id, po_id, *line_ids = identifiers
            service = PurchaseReceiptService(failing_session)
            original = service.inventory.post_receipt_under_locks
            calls = 0

            async def fail_second(**kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise RuntimeError("injected ledger failure")
                return await original(**kwargs)

            monkeypatch.setattr(service.inventory, "post_receipt_under_locks", fail_second)
            await service.post(
                actor=await failing_session.get(User, actor_id),
                organization_id=org_id,
                purchase_order_id=po_id,
                command=PurchaseReceiptCommand.model_validate(
                    {
                        "warehouse_id": warehouse_id,
                        "lines": [
                            {
                                "purchase_order_line_id": line_id,
                                "lot_code": f"ROLLBACK-{index}",
                                "quantity": "1.000000",
                            }
                            for index, line_id in enumerate(line_ids)
                        ],
                    }
                ),
                idempotency_key="rollback",
                request_ctx={},
            )

    async with factory() as verification:
        assert (
            await verification.scalar(
                select(func.count())
                .select_from(PurchaseReceipt)
                .where(PurchaseReceipt.purchase_order_id == identifiers[3])
            )
            == 0
        )
        assert (
            await verification.scalar(
                select(func.count())
                .select_from(PurchaseReceiptLine)
                .where(PurchaseReceiptLine.purchase_order_line_id.in_(identifiers[4:]))
            )
            == 0
        )
        assert (
            await verification.scalar(
                select(func.count())
                .select_from(InventoryLot)
                .join(Warehouse, Warehouse.id == InventoryLot.warehouse_id)
                .where(
                    InventoryLot.lot_code.like("ROLLBACK-%"),
                    Warehouse.organization_id == identifiers[0],
                )
            )
            == 0
        )
        assert (
            await verification.scalar(
                select(func.count())
                .select_from(InventoryTransaction)
                .where(
                    InventoryTransaction.organization_id == identifiers[0],
                    InventoryTransaction.reference_type == "purchase_receipt_line",
                )
            )
            == 0
        )
        persisted_po = await verification.get(PurchaseOrder, identifiers[3])
        assert persisted_po.status == PurchaseOrderStatus.APPROVED
        assert persisted_po.version == 3
        persisted_lines = list(
            (
                await verification.execute(
                    select(PurchaseOrderLine).where(
                        PurchaseOrderLine.purchase_order_id == identifiers[3]
                    )
                )
            ).scalars()
        )
        assert all(Decimal(line.received_quantity) == 0 for line in persisted_lines)
        assert (
            await verification.scalar(
                select(func.count())
                .select_from(PurchaseOrderTransition)
                .where(PurchaseOrderTransition.purchase_order_id == identifiers[3])
            )
            == 0
        )
        assert (
            await verification.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(
                    AuditEvent.action == "purchase_receipt.post",
                    AuditEvent.organization_id == identifiers[0],
                )
            )
            == 0
        )


@_postgres_only
async def test_release_reconciliation_repeated_mixed_receipts_from_independent_session(
    db_session, _engine
):
    org, actor, _farm, warehouse, po, lines, _items = await _seed(db_session, two_lines=True)
    identifiers = (org.id, actor.id, warehouse.id, po.id, lines[0].id, lines[1].id)
    await db_session.commit()
    factory = async_sessionmaker(_engine, expire_on_commit=False)

    async with factory() as first_session, first_session.begin():
        org_id, actor_id, warehouse_id, po_id, first_line_id, second_line_id = identifiers
        first_receipt, replay = await PurchaseReceiptService(first_session).post(
            actor=await first_session.get(User, actor_id),
            organization_id=org_id,
            purchase_order_id=po_id,
            command=PurchaseReceiptCommand.model_validate(
                {
                    "warehouse_id": warehouse_id,
                    "received_at": "2026-08-10T12:00:00Z",
                    "lines": [
                        {
                            "purchase_order_line_id": first_line_id,
                            "lot_code": "MIXED-FIRST",
                            "quantity": "4.000000",
                        },
                        {
                            "purchase_order_line_id": second_line_id,
                            "lot_code": "MIXED-COMPLETE",
                            "quantity": "10.000000",
                        },
                    ],
                }
            ),
            idempotency_key="release-mixed-first",
            request_ctx={"request_id": "release-mixed-first-request"},
        )
        assert replay is False
        first_receipt_id = first_receipt.id

    async with factory() as second_session, second_session.begin():
        org_id, actor_id, warehouse_id, po_id, first_line_id, _second_line_id = identifiers
        second_receipt, replay = await PurchaseReceiptService(second_session).post(
            actor=await second_session.get(User, actor_id),
            organization_id=org_id,
            purchase_order_id=po_id,
            command=PurchaseReceiptCommand.model_validate(
                {
                    "warehouse_id": warehouse_id,
                    "received_at": "2026-08-10T13:00:00Z",
                    "lines": [
                        {
                            "purchase_order_line_id": first_line_id,
                            "lot_code": "MIXED-FINAL",
                            "quantity": "6.000000",
                        }
                    ],
                }
            ),
            idempotency_key="release-mixed-final",
            request_ctx={"request_id": "release-mixed-final-request"},
        )
        assert replay is False
        second_receipt_id = second_receipt.id

    async with factory() as verification:
        persisted_po = await verification.get(PurchaseOrder, identifiers[3])
        persisted_lines = list(
            (
                await verification.execute(
                    select(PurchaseOrderLine)
                    .where(PurchaseOrderLine.purchase_order_id == identifiers[3])
                    .order_by(PurchaseOrderLine.line_number)
                )
            ).scalars()
        )
        receipts = list(
            (
                await verification.execute(
                    select(PurchaseReceipt)
                    .where(PurchaseReceipt.purchase_order_id == identifiers[3])
                    .order_by(PurchaseReceipt.received_at)
                )
            ).scalars()
        )
        receipt_lines = list(
            (
                await verification.execute(
                    select(PurchaseReceiptLine)
                    .join(PurchaseReceipt)
                    .where(PurchaseReceipt.purchase_order_id == identifiers[3])
                    .order_by(PurchaseReceipt.received_at, PurchaseReceiptLine.line_number)
                )
            ).scalars()
        )
        transactions = list(
            (
                await verification.execute(
                    select(InventoryTransaction)
                    .where(
                        InventoryTransaction.reference_type == "purchase_receipt_line",
                        InventoryTransaction.reference_id.in_([line.id for line in receipt_lines]),
                    )
                    .order_by(InventoryTransaction.created_at, InventoryTransaction.id)
                )
            ).scalars()
        )
        transitions = list(
            (
                await verification.execute(
                    select(PurchaseOrderTransition)
                    .where(PurchaseOrderTransition.purchase_order_id == identifiers[3])
                    .order_by(PurchaseOrderTransition.occurred_at)
                )
            ).scalars()
        )
        audits = list(
            (
                await verification.execute(
                    select(AuditEvent)
                    .where(
                        AuditEvent.action == "purchase_receipt.post",
                        AuditEvent.organization_id == identifiers[0],
                    )
                    .order_by(AuditEvent.created_at)
                )
            ).scalars()
        )

        assert [receipt.id for receipt in receipts] == [first_receipt_id, second_receipt_id]
        assert [receipt.grn for receipt in receipts] == ["GRN-2026-000001", "GRN-2026-000002"]
        assert len(receipt_lines) == len(transactions) == 3
        assert len({line.inventory_lot_id for line in receipt_lines}) == 3
        assert {tx.reference_id for tx in transactions} == {line.id for line in receipt_lines}
        assert all(tx.transaction_type == InventoryTransactionType.RECEIPT for tx in transactions)
        assert all(
            Decimal(tx.quantity)
            == next(
                Decimal(line.quantity_canonical)
                for line in receipt_lines
                if line.id == tx.reference_id
            )
            for tx in transactions
        )
        for line in persisted_lines:
            matching = [entry for entry in receipt_lines if entry.purchase_order_line_id == line.id]
            assert sum((Decimal(entry.quantity) for entry in matching), Decimal(0)) == Decimal(
                line.received_quantity
            )
            assert sum(
                (Decimal(entry.quantity_canonical) for entry in matching), Decimal(0)
            ) == Decimal(line.received_quantity_canonical)
            assert Decimal(line.received_quantity) == Decimal(line.ordered_quantity)
            assert Decimal(line.received_quantity_canonical) == Decimal(
                line.ordered_quantity_canonical
            )
        assert persisted_po.status == PurchaseOrderStatus.RECEIVED
        assert persisted_po.version == 5
        assert [(row.from_status, row.to_status) for row in transitions] == [
            (PurchaseOrderStatus.APPROVED, PurchaseOrderStatus.PARTIALLY_RECEIVED),
            (PurchaseOrderStatus.PARTIALLY_RECEIVED, PurchaseOrderStatus.RECEIVED),
        ]
        assert [row.metadata_json["purchase_receipt_id"] for row in transitions] == [
            str(first_receipt_id),
            str(second_receipt_id),
        ]
        assert len(audits) == 2
        assert {audit.entity_id for audit in audits} == {
            str(first_receipt_id),
            str(second_receipt_id),
        }


@_postgres_only
async def test_real_lot_conflict_rolls_back_prior_lot_and_all_receipt_state(db_session, _engine):
    org, actor, _farm, warehouse, po, lines, items = await _seed(db_session)
    conflicting_lot = InventoryLot(
        item_id=items[0].id,
        warehouse_id=warehouse.id,
        lot_code="Z-ROLLBACK-CONFLICT",
        expiry_date=date(2026, 12, 31),
    )
    db_session.add(conflicting_lot)
    await db_session.commit()
    identifiers = (org.id, actor.id, warehouse.id, po.id, lines[0].id)
    factory = async_sessionmaker(_engine, expire_on_commit=False)

    with pytest.raises(HTTPException) as exc:
        async with factory() as failing_session, failing_session.begin():
            org_id, actor_id, warehouse_id, po_id, line_id = identifiers
            await PurchaseReceiptService(failing_session).post(
                actor=await failing_session.get(User, actor_id),
                organization_id=org_id,
                purchase_order_id=po_id,
                command=PurchaseReceiptCommand.model_validate(
                    {
                        "warehouse_id": warehouse_id,
                        "lines": [
                            {
                                "purchase_order_line_id": line_id,
                                "lot_code": "A-ROLLBACK-CREATED",
                                "quantity": "1.000000",
                            },
                            {
                                "purchase_order_line_id": line_id,
                                "lot_code": "Z-ROLLBACK-CONFLICT",
                                "quantity": "1.000000",
                            },
                        ],
                    }
                ),
                idempotency_key="real-domain-rollback",
                request_ctx={"request_id": "real-domain-rollback-request"},
            )
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "lot_attribute_conflict"

    async with factory() as verification:
        assert (
            await verification.scalar(
                select(func.count())
                .select_from(PurchaseReceipt)
                .where(PurchaseReceipt.purchase_order_id == identifiers[3])
            )
            == 0
        )
        assert (
            await verification.scalar(
                select(func.count())
                .select_from(PurchaseReceiptLine)
                .where(PurchaseReceiptLine.purchase_order_line_id == identifiers[4])
            )
            == 0
        )
        assert (
            await verification.scalar(
                select(func.count())
                .select_from(InventoryLot)
                .where(
                    InventoryLot.warehouse_id == identifiers[2],
                    InventoryLot.lot_code == "A-ROLLBACK-CREATED",
                )
            )
            == 0
        )
        assert (
            await verification.scalar(
                select(func.count())
                .select_from(InventoryTransaction)
                .where(
                    InventoryTransaction.organization_id == identifiers[0],
                    InventoryTransaction.reference_type == "purchase_receipt_line",
                )
            )
            == 0
        )
        persisted_po = await verification.get(PurchaseOrder, identifiers[3])
        persisted_line = await verification.get(PurchaseOrderLine, identifiers[4])
        assert persisted_po.status == PurchaseOrderStatus.APPROVED
        assert persisted_po.version == 3
        assert Decimal(persisted_line.received_quantity) == Decimal("0.000000")
        assert Decimal(persisted_line.received_quantity_canonical) == Decimal("0.000000")
        assert (
            await verification.scalar(
                select(func.count())
                .select_from(PurchaseOrderTransition)
                .where(PurchaseOrderTransition.purchase_order_id == identifiers[3])
            )
            == 0
        )
        assert (
            await verification.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(
                    AuditEvent.action == "purchase_receipt.post",
                    AuditEvent.organization_id == identifiers[0],
                )
            )
            == 0
        )


async def test_receipt_role_catalogue_contract():
    roles = {role.name: set(role.permissions) for role in ROLE_DEFINITIONS}
    assert {"purchase_receipt.create", "purchase_receipt.read"} <= roles["organization_owner"]
    assert {"purchase_receipt.create", "purchase_receipt.read"} <= roles["farm_director"]
    assert {"purchase_receipt.create", "purchase_receipt.read"} <= roles["farm_manager"]
    assert {"purchase_receipt.create", "purchase_receipt.read"} <= roles["storekeeper"]
    for role in ("supervisor", "accountant", "viewer"):
        assert "purchase_receipt.read" in roles[role]
        assert "purchase_receipt.create" not in roles[role]


async def test_posted_receipt_is_orm_immutable(db_session):
    org, actor, _farm, warehouse, po, lines, _items = await _seed(db_session)
    receipt, _ = await PurchaseReceiptService(db_session).post(
        actor=actor,
        organization_id=org.id,
        purchase_order_id=po.id,
        command=_command(warehouse, lines, ["1.000000"]),
        idempotency_key="immutable",
        request_ctx={},
    )
    receipt.notes = "attempted rewrite"
    with pytest.raises(ValueError, match="immutable posted record"):
        await db_session.flush()
    await db_session.rollback()


@_postgres_only
async def test_posted_receipt_is_database_closed_after_commit(db_session, _engine):
    org, actor, _farm, warehouse, po, lines, _items = await _seed(db_session)
    receipt, _ = await PurchaseReceiptService(db_session).post(
        actor=actor,
        organization_id=org.id,
        purchase_order_id=po.id,
        command=_command(warehouse, lines, ["1.000000"]),
        idempotency_key="database-immutable",
        request_ctx={},
    )
    receipt_id = receipt.id
    line_id = receipt.lines[0].id
    unused_transactions = [
        InventoryTransaction(
            organization_id=org.id,
            farm_id=receipt.farm_id,
            warehouse_id=warehouse.id,
            item_id=receipt.lines[0].inventory_item_id,
            lot_id=receipt.lines[0].inventory_lot_id,
            transaction_type=InventoryTransactionType.RECEIPT,
            quantity=Decimal("1.000000"),
            unit=StockUnit.KG,
            performed_by_id=actor.id,
            reason=f"unused append proof {index}",
        )
        for index in range(2)
    ]
    db_session.add_all(unused_transactions)
    await db_session.flush()
    unused_transaction_ids = [transaction.id for transaction in unused_transactions]
    await db_session.commit()
    factory = async_sessionmaker(_engine, expire_on_commit=False)

    statements = [
        ("UPDATE purchase_receipts SET notes = 'rewrite' WHERE id = :id", receipt_id),
        ("DELETE FROM purchase_receipts WHERE id = :id", receipt_id),
        ("UPDATE purchase_receipt_lines SET lot_code = 'rewrite' WHERE id = :id", line_id),
        ("DELETE FROM purchase_receipt_lines WHERE id = :id", line_id),
    ]
    for statement, target_id in statements:
        with pytest.raises(DBAPIError):
            async with factory() as attempted, attempted.begin():
                await attempted.execute(text(statement), {"id": target_id})

    append_statement = text("""
        INSERT INTO public.purchase_receipt_lines
          (id, purchase_receipt_id, line_number, purchase_order_line_id,
           inventory_item_id, warehouse_id, storage_location_id, inventory_lot_id,
           inventory_transaction_id, lot_code, expiry_date, quantity, ordered_unit,
           quantity_canonical, canonical_unit, unit_price, currency_code, created_at)
        SELECT gen_random_uuid(), purchase_receipt_id, line_number + :offset,
               purchase_order_line_id, inventory_item_id, warehouse_id,
               storage_location_id, inventory_lot_id, :transaction_id,
               lot_code, expiry_date, quantity, ordered_unit, quantity_canonical,
               canonical_unit, unit_price, currency_code, now()
        FROM public.purchase_receipt_lines WHERE id = :line_id
        """)
    for shadow, transaction_id, offset in (
        (False, unused_transaction_ids[0], 100),
        (True, unused_transaction_ids[1], 200),
    ):
        with pytest.raises(DBAPIError) as exc:
            async with factory() as attempted, attempted.begin():
                if shadow:
                    await attempted.execute(
                        text(
                            "CREATE TEMPORARY TABLE purchase_receipts "
                            "(id uuid PRIMARY KEY) ON COMMIT DROP"
                        )
                    )
                await attempted.execute(
                    append_statement,
                    {
                        "line_id": line_id,
                        "transaction_id": transaction_id,
                        "offset": offset,
                    },
                )
        assert getattr(exc.value.orig, "sqlstate", None) == "P0001"
        assert "purchase_receipt_lines is an immutable posted record" in str(exc.value.orig)


@_postgres_only
async def test_payload_hash_database_constraint_is_lowercase_sha256(db_session, _engine):
    org, actor, _farm, warehouse, po, lines, _items = await _seed(db_session)
    receipt, _ = await PurchaseReceiptService(db_session).post(
        actor=actor,
        organization_id=org.id,
        purchase_order_id=po.id,
        command=_command(warehouse, lines, ["1.000000"]),
        idempotency_key="valid-sha",
        request_ctx={},
    )
    assert len(receipt.payload_hash) == 64
    assert receipt.payload_hash == receipt.payload_hash.lower()
    assert set(receipt.payload_hash) <= set("0123456789abcdef")
    await db_session.commit()
    factory = async_sessionmaker(_engine, expire_on_commit=False)

    for suffix, invalid_hash in (("upper", "A" * 64), ("nonhex", "g" * 64), ("short", "a" * 63)):
        with pytest.raises(IntegrityError):
            async with factory() as attempted, attempted.begin():
                await attempted.execute(
                    text("""
                        INSERT INTO purchase_receipts
                          (id, organization_id, farm_id, purchase_order_id, warehouse_id,
                           grn, supplier_delivery_reference, received_at, received_by_id,
                           notes, idempotency_key, payload_hash, created_at)
                        SELECT gen_random_uuid(), organization_id, farm_id, purchase_order_id,
                               warehouse_id, grn || :suffix, supplier_delivery_reference,
                               received_at, received_by_id, notes,
                               idempotency_key || :suffix, :payload_hash, now()
                        FROM purchase_receipts WHERE id = :receipt_id
                        """),
                    {
                        "suffix": f"-{suffix}",
                        "payload_hash": invalid_hash,
                        "receipt_id": receipt.id,
                    },
                )


@_postgres_only
async def test_concurrent_grn_allocation_is_unique(db_session, _engine):
    org = Organization(name="GRN Org", slug=f"grn-{uuid4().hex}", is_active=True)
    db_session.add(org)
    await db_session.flush()
    org_id = org.id
    await db_session.commit()
    factory = async_sessionmaker(_engine, expire_on_commit=False)

    async def allocate() -> str:
        async with factory() as session, session.begin():
            return await PurchaseReceiptSequenceRepository(session).allocate(org_id, 2026)

    first_holds_lock = asyncio.Event()
    release_first = asyncio.Event()
    competitors_attempted = asyncio.Event()
    PurchaseReceiptSequenceRepository._after_allocate_lock_signal = first_holds_lock
    PurchaseReceiptSequenceRepository._hold_after_allocate_lock_gate = release_first
    try:
        first_task = asyncio.create_task(allocate())
        await asyncio.wait_for(first_holds_lock.wait(), timeout=5)
        PurchaseReceiptSequenceRepository._before_allocate_lock_signal = competitors_attempted
        competitor_tasks = [asyncio.create_task(allocate()) for _ in range(7)]
        await asyncio.wait_for(competitors_attempted.wait(), timeout=5)
        await asyncio.sleep(0.05)
        assert all(not task.done() for task in competitor_tasks)
        release_first.set()
        values = await asyncio.wait_for(asyncio.gather(first_task, *competitor_tasks), timeout=10)
    finally:
        release_first.set()
        PurchaseReceiptSequenceRepository._before_allocate_lock_signal = None
        PurchaseReceiptSequenceRepository._after_allocate_lock_signal = None
        PurchaseReceiptSequenceRepository._hold_after_allocate_lock_gate = None
    assert len(set(values)) == 8
    assert sorted(values) == [f"GRN-2026-{value:06d}" for value in range(1, 9)]


@_postgres_only
async def test_concurrent_receipts_cannot_over_receive(db_session, _engine):
    org, actor, _farm, warehouse, po, lines, _items = await _seed(db_session)
    identifiers = (org.id, actor.id, warehouse.id, po.id, lines[0].id)
    await db_session.commit()
    factory = async_sessionmaker(_engine, expire_on_commit=False)

    async def post(key: str) -> str:
        async with factory() as session, session.begin():
            org_id, actor_id, warehouse_id, po_id, line_id = identifiers
            locked_actor = await session.get(User, actor_id)
            command = PurchaseReceiptCommand.model_validate(
                {
                    "warehouse_id": warehouse_id,
                    "lines": [
                        {
                            "purchase_order_line_id": line_id,
                            "lot_code": key,
                            "quantity": "6.000000",
                        }
                    ],
                }
            )
            try:
                await PurchaseReceiptService(session).post(
                    actor=locked_actor,
                    organization_id=org_id,
                    purchase_order_id=po_id,
                    command=command,
                    idempotency_key=key,
                    request_ctx={},
                )
            except HTTPException as exc:
                return exc.detail["code"]
            return "posted"

    first_holds_lock = asyncio.Event()
    release_first = asyncio.Event()
    competitor_attempted = asyncio.Event()
    PurchaseReceiptService._after_authorization_lock_signal = first_holds_lock
    PurchaseReceiptService._hold_after_authorization_lock_gate = release_first
    try:
        first_task = asyncio.create_task(post("race-a"))
        await asyncio.wait_for(first_holds_lock.wait(), timeout=5)
        PurchaseReceiptService._before_authorization_lock_signal = competitor_attempted
        second_task = asyncio.create_task(post("race-b"))
        await asyncio.wait_for(competitor_attempted.wait(), timeout=5)
        await asyncio.sleep(0.05)
        assert not second_task.done()
        release_first.set()
        results = await asyncio.wait_for(asyncio.gather(first_task, second_task), timeout=10)
    finally:
        release_first.set()
        PurchaseReceiptService._before_authorization_lock_signal = None
        PurchaseReceiptService._after_authorization_lock_signal = None
        PurchaseReceiptService._hold_after_authorization_lock_gate = None
    assert sorted(results) == ["posted", "purchase_order_over_receipt"]
    db_session.expire_all()
    persisted_line = await db_session.get(PurchaseOrderLine, identifiers[4])
    assert Decimal(persisted_line.received_quantity) == Decimal("6.000000")
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(PurchaseReceipt)
            .where(PurchaseReceipt.purchase_order_id == identifiers[3])
        )
        == 1
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(InventoryTransaction)
            .join(
                PurchaseReceiptLine,
                PurchaseReceiptLine.id == InventoryTransaction.reference_id,
            )
            .join(PurchaseReceipt, PurchaseReceipt.id == PurchaseReceiptLine.purchase_receipt_id)
            .where(PurchaseReceipt.purchase_order_id == identifiers[3])
        )
        == 1
    )


@_postgres_only
async def test_concurrent_exact_replay_posts_stock_once(db_session, _engine):
    org, actor, _farm, warehouse, po, lines, _items = await _seed(db_session)
    identifiers = (org.id, actor.id, warehouse.id, po.id, lines[0].id)
    await db_session.commit()
    factory = async_sessionmaker(_engine, expire_on_commit=False)

    async def post() -> tuple[str, bool]:
        async with factory() as session, session.begin():
            org_id, actor_id, warehouse_id, po_id, line_id = identifiers
            locked_actor = await session.get(User, actor_id)
            command = PurchaseReceiptCommand.model_validate(
                {
                    "warehouse_id": warehouse_id,
                    "lines": [
                        {
                            "purchase_order_line_id": line_id,
                            "lot_code": "REPLAY-RACE",
                            "quantity": "2.000000",
                        }
                    ],
                }
            )
            receipt, replay = await PurchaseReceiptService(session).post(
                actor=locked_actor,
                organization_id=org_id,
                purchase_order_id=po_id,
                command=command,
                idempotency_key="same-race-key",
                request_ctx={},
            )
            return receipt.grn, replay

    first_holds_lock = asyncio.Event()
    release_first = asyncio.Event()
    replay_attempted = asyncio.Event()
    PurchaseReceiptService._after_authorization_lock_signal = first_holds_lock
    PurchaseReceiptService._hold_after_authorization_lock_gate = release_first
    try:
        first_task = asyncio.create_task(post())
        await asyncio.wait_for(first_holds_lock.wait(), timeout=5)
        PurchaseReceiptService._before_authorization_lock_signal = replay_attempted
        replay_task = asyncio.create_task(post())
        await asyncio.wait_for(replay_attempted.wait(), timeout=5)
        await asyncio.sleep(0.05)
        assert not replay_task.done()
        release_first.set()
        results = await asyncio.wait_for(asyncio.gather(first_task, replay_task), timeout=10)
    finally:
        release_first.set()
        PurchaseReceiptService._before_authorization_lock_signal = None
        PurchaseReceiptService._after_authorization_lock_signal = None
        PurchaseReceiptService._hold_after_authorization_lock_gate = None
    assert {result[0] for result in results} == {"GRN-2026-000001"}
    assert sorted(result[1] for result in results) == [False, True]
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(PurchaseReceipt)
            .where(PurchaseReceipt.purchase_order_id == identifiers[3])
        )
        == 1
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(PurchaseReceiptLine)
            .join(PurchaseReceipt)
            .where(PurchaseReceipt.purchase_order_id == identifiers[3])
        )
        == 1
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(InventoryLot)
            .join(PurchaseReceiptLine, PurchaseReceiptLine.inventory_lot_id == InventoryLot.id)
            .join(PurchaseReceipt)
            .where(PurchaseReceipt.purchase_order_id == identifiers[3])
        )
        == 1
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(PurchaseOrderTransition)
            .where(PurchaseOrderTransition.purchase_order_id == identifiers[3])
        )
        == 1
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.action == "purchase_receipt.post",
                AuditEvent.organization_id == identifiers[0],
            )
        )
        == 1
    )
    db_session.expire_all()
    persisted_line = await db_session.get(PurchaseOrderLine, identifiers[4])
    assert Decimal(persisted_line.received_quantity) == Decimal("2.000000")
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(InventoryTransaction)
            .join(
                PurchaseReceiptLine,
                PurchaseReceiptLine.id == InventoryTransaction.reference_id,
            )
            .join(PurchaseReceipt, PurchaseReceipt.id == PurchaseReceiptLine.purchase_receipt_id)
            .where(PurchaseReceipt.purchase_order_id == identifiers[3])
        )
        == 1
    )


@_postgres_only
async def test_receipt_and_transfer_share_deadlock_free_authorization_lock_order(
    db_session, _engine
):
    org, actor, farm, warehouse, po, lines, items = await _seed(db_session)
    destination = Warehouse(
        organization_id=org.id,
        farm_id=farm.id,
        name="Transfer destination",
        code=f"TD-{uuid4().hex[:8]}",
        status=WarehouseStatus.ACTIVE,
    )
    source_lot = InventoryLot(
        item_id=items[0].id,
        warehouse_id=warehouse.id,
        lot_code="TRANSFER-SOURCE",
    )
    db_session.add_all([destination, source_lot])
    await db_session.flush()
    db_session.add(
        InventoryTransaction(
            organization_id=org.id,
            farm_id=farm.id,
            warehouse_id=warehouse.id,
            item_id=items[0].id,
            lot_id=source_lot.id,
            transaction_type=InventoryTransactionType.RECEIPT,
            quantity=Decimal("5.000000"),
            unit=StockUnit.KG,
            performed_by_id=actor.id,
            reason="concurrency seed",
        )
    )
    identifiers = (
        org.id,
        actor.id,
        warehouse.id,
        destination.id,
        po.id,
        lines[0].id,
        source_lot.id,
    )
    await db_session.commit()
    factory = async_sessionmaker(_engine, expire_on_commit=False)
    receipt_locked = asyncio.Event()
    release_receipt = asyncio.Event()
    transfer_attempted = asyncio.Event()
    PurchaseReceiptService._after_authorization_lock_signal = receipt_locked
    PurchaseReceiptService._hold_after_authorization_lock_gate = release_receipt
    InventoryService._transfer_before_authorization_lock_signal = transfer_attempted

    async def receive():
        async with factory() as session, session.begin():
            org_id, actor_id, warehouse_id, _dst_id, po_id, line_id, _lot_id = identifiers
            return await PurchaseReceiptService(session).post(
                actor=await session.get(User, actor_id),
                organization_id=org_id,
                purchase_order_id=po_id,
                command=PurchaseReceiptCommand.model_validate(
                    {
                        "warehouse_id": warehouse_id,
                        "lines": [
                            {
                                "purchase_order_line_id": line_id,
                                "lot_code": "CONCURRENT-RECEIPT",
                                "quantity": "1.000000",
                            }
                        ],
                    }
                ),
                idempotency_key="receipt-transfer-order",
                request_ctx={},
            )

    async def transfer():
        async with factory() as session, session.begin():
            _org_id, actor_id, source_id, dst_id, _po_id, _line_id, lot_id = identifiers
            service = PurchaseReceiptService(session).inventory
            return await service.transfer(
                actor=await session.get(User, actor_id),
                warehouse_id=source_id,
                payload={
                    "destination_warehouse_id": dst_id,
                    "lot_id": lot_id,
                    "quantity": Decimal("1.000000"),
                    "unit": StockUnit.KG,
                    "reason": "lock-order regression",
                },
                request_ctx={},
                idempotency_key="receipt-transfer-lock-order",
            )

    try:
        receipt_task = asyncio.create_task(receive())
        await asyncio.wait_for(receipt_locked.wait(), timeout=5)
        transfer_task = asyncio.create_task(transfer())
        await asyncio.wait_for(transfer_attempted.wait(), timeout=5)
        await asyncio.sleep(0.05)
        assert not transfer_task.done()
        release_receipt.set()
        receipt_result, transfer_result = await asyncio.wait_for(
            asyncio.gather(receipt_task, transfer_task), timeout=10
        )
        assert receipt_result[1] is False
        assert transfer_result[2] is False
    finally:
        release_receipt.set()
        PurchaseReceiptService._after_authorization_lock_signal = None
        PurchaseReceiptService._hold_after_authorization_lock_gate = None
        InventoryService._transfer_before_authorization_lock_signal = None
