"""PostgreSQL durability checks for normalized production transfers."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import create_engine, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from alembic import command
from alembic.config import Config
from app.db import session as db_session_module
from app.models.farm import Farm
from app.models.production import (
    ProductionBatch,
    ProductionEvent,
    ProductionSite,
    ProductionTransfer,
    ProductionTransferRole,
    ProductionUnit,
)
from tests.test_codex_review_gate_02 import _prepare_active_batch, _prepare_receiving_batch
from tests.test_production_engine import _create_unit

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        "postgresql" not in os.environ.get("DATABASE_URL", ""),
        reason="Requires PostgreSQL deferred constraint triggers.",
    ),
]


async def _topology(client: AsyncClient) -> dict[str, UUID]:
    source = await _prepare_active_batch(client, quantity=1000)
    site_response = await client.post(
        f"/api/v1/farms/{source['farm_id']}/sites",
        json={"name": "Transfer destination", "code": f"DST-{uuid4().hex[:8]}"},
    )
    assert site_response.status_code == 201, site_response.text
    destination_site_id = site_response.json()["id"]
    destination_unit_id = await _create_unit(client, destination_site_id, source["unit_type_id"])
    destination_batch_id = await _prepare_receiving_batch(client, destination_unit_id)
    async with db_session_module.AsyncSessionLocal() as session:
        source_batch = await session.get(ProductionBatch, UUID(source["batch_id"]))
        destination_batch = await session.get(ProductionBatch, UUID(destination_batch_id))
        assert source_batch is not None and destination_batch is not None
        source_unit = await session.get(ProductionUnit, source_batch.unit_id)
        destination_unit = await session.get(ProductionUnit, destination_batch.unit_id)
        assert source_unit is not None and destination_unit is not None
        source_site = await session.get(ProductionSite, source_unit.site_id)
        destination_site = await session.get(ProductionSite, destination_unit.site_id)
        assert source_site is not None and destination_site is not None
        farm = await session.get(Farm, source_site.farm_id)
        assert farm is not None
    return {
        "organization_id": farm.organization_id,
        "farm_id": farm.id,
        "source_site_id": source_site.id,
        "source_unit_id": source_unit.id,
        "source_batch_id": source_batch.id,
        "destination_site_id": destination_site.id,
        "destination_unit_id": destination_unit.id,
        "destination_batch_id": destination_batch.id,
    }


def _transfer(topology: dict[str, UUID], *, quantity: int = 100, loss: int = 5):
    return ProductionTransfer(
        id=uuid4(),
        organization_id=topology["organization_id"],
        farm_id=topology["farm_id"],
        source_batch_id=topology["source_batch_id"],
        destination_batch_id=topology["destination_batch_id"],
        source_unit_id=topology["source_unit_id"],
        destination_unit_id=topology["destination_unit_id"],
        quantity=quantity,
        transfer_loss=loss,
        created_at=datetime.now(UTC),
    )


def _event(
    topology: dict[str, UUID],
    transfer: ProductionTransfer,
    role: ProductionTransferRole,
    *,
    quantity: int | None = None,
    loss: int | None = None,
    site_id: UUID | None = None,
) -> ProductionEvent:
    outgoing = role == ProductionTransferRole.OUT
    return ProductionEvent(
        organization_id=topology["organization_id"],
        farm_id=topology["farm_id"],
        site_id=site_id or topology["source_site_id" if outgoing else "destination_site_id"],
        unit_id=topology["source_unit_id" if outgoing else "destination_unit_id"],
        batch_id=topology["source_batch_id" if outgoing else "destination_batch_id"],
        event_type="TRANSFER",
        event_type_version=3,
        transfer_id=transfer.id,
        transfer_role=role,
        performed_at=datetime.now(UTC),
        data={
            "quantity": transfer.quantity if quantity is None else quantity,
            "transfer_loss": transfer.transfer_loss if loss is None else loss,
        },
        is_final=False,
        created_at=datetime.now(UTC),
    )


async def _expect_commit_rejected(*rows: object) -> None:
    async with db_session_module.AsyncSessionLocal() as session:
        with pytest.raises(IntegrityError):
            if rows and isinstance(rows[0], ProductionTransfer):
                session.add(rows[0])
                await session.flush()
                session.add_all(list(rows[1:]))
            else:
                session.add_all(list(rows))
            await session.commit()
        await session.rollback()


@pytest.mark.parametrize("roles", [(), (ProductionTransferRole.OUT,), (ProductionTransferRole.IN,)])
async def test_incomplete_normalized_transfer_cannot_commit(
    client: AsyncClient, roles: tuple[ProductionTransferRole, ...]
) -> None:
    topology = await _topology(client)
    transfer = _transfer(topology)
    await _expect_commit_rejected(transfer, *(_event(topology, transfer, role) for role in roles))


@pytest.mark.parametrize("role", [ProductionTransferRole.OUT, ProductionTransferRole.IN])
async def test_duplicate_transfer_role_cannot_commit(
    client: AsyncClient, role: ProductionTransferRole
) -> None:
    topology = await _topology(client)
    transfer = _transfer(topology)
    await _expect_commit_rejected(
        transfer,
        _event(topology, transfer, role),
        _event(topology, transfer, role),
    )


async def _commit_valid_pair(client: AsyncClient):
    topology = await _topology(client)
    transfer = _transfer(topology)
    outgoing = _event(topology, transfer, ProductionTransferRole.OUT)
    incoming = _event(topology, transfer, ProductionTransferRole.IN)
    async with db_session_module.AsyncSessionLocal() as session:
        session.add(transfer)
        await session.flush()
        session.add_all([outgoing, incoming])
        await session.commit()
    return topology, transfer, outgoing, incoming


async def test_exact_out_in_pair_commits(client: AsyncClient) -> None:
    await _commit_valid_pair(client)


@pytest.mark.parametrize(
    ("role", "quantity", "loss"),
    [
        (ProductionTransferRole.OUT, 99, None),
        (ProductionTransferRole.IN, 99, None),
        (ProductionTransferRole.OUT, None, 4),
        (ProductionTransferRole.IN, None, 4),
    ],
)
async def test_normalized_payload_disagreement_is_rejected(
    client: AsyncClient,
    role: ProductionTransferRole,
    quantity: int | None,
    loss: int | None,
) -> None:
    topology = await _topology(client)
    transfer = _transfer(topology)
    other_role = (
        ProductionTransferRole.IN
        if role == ProductionTransferRole.OUT
        else ProductionTransferRole.OUT
    )
    await _expect_commit_rejected(
        transfer,
        _event(topology, transfer, role, quantity=quantity, loss=loss),
        _event(topology, transfer, other_role),
    )


async def test_normalized_event_site_mismatch_is_rejected(client: AsyncClient) -> None:
    topology = await _topology(client)
    transfer = _transfer(topology)
    await _expect_commit_rejected(
        transfer,
        _event(
            topology,
            transfer,
            ProductionTransferRole.OUT,
            site_id=topology["destination_site_id"],
        ),
        _event(topology, transfer, ProductionTransferRole.IN),
    )


async def test_new_null_topology_transfer_is_rejected(client: AsyncClient) -> None:
    topology = await _topology(client)
    legacy_style = ProductionEvent(
        organization_id=topology["organization_id"],
        farm_id=topology["farm_id"],
        site_id=topology["source_site_id"],
        unit_id=topology["source_unit_id"],
        batch_id=topology["source_batch_id"],
        event_type="TRANSFER",
        event_type_version=2,
        transfer_id=None,
        transfer_role=None,
        performed_at=datetime.now(UTC),
        data={"quantity": 1, "transfer_loss": 0},
        is_final=False,
        created_at=datetime.now(UTC),
    )
    await _expect_commit_rejected(legacy_style)


async def test_update_cannot_create_new_null_topology_transfer(client: AsyncClient) -> None:
    topology = await _topology(client)
    event = ProductionEvent(
        organization_id=topology["organization_id"],
        farm_id=topology["farm_id"],
        site_id=topology["source_site_id"],
        unit_id=topology["source_unit_id"],
        batch_id=topology["source_batch_id"],
        event_type="MORTALITY",
        event_type_version=1,
        transfer_id=None,
        transfer_role=None,
        performed_at=datetime.now(UTC),
        data={"count": 1},
        is_final=False,
        created_at=datetime.now(UTC),
    )
    async with db_session_module.AsyncSessionLocal() as session:
        session.add(event)
        await session.commit()
    async with db_session_module.AsyncSessionLocal() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                update(ProductionEvent)
                .where(ProductionEvent.id == event.id)
                .values(event_type="TRANSFER", data={"quantity": 1, "transfer_loss": 0})
            )
            await session.commit()
        await session.rollback()


async def test_transfer_and_event_topology_remain_immutable(client: AsyncClient) -> None:
    _topology_data, transfer, outgoing, _incoming = await _commit_valid_pair(client)
    async with db_session_module.AsyncSessionLocal() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                update(ProductionTransfer)
                .where(ProductionTransfer.id == transfer.id)
                .values(quantity=transfer.quantity + 1)
            )
            await session.commit()
        await session.rollback()
    async with db_session_module.AsyncSessionLocal() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                update(ProductionEvent)
                .where(ProductionEvent.id == outgoing.id)
                .values(transfer_role=ProductionTransferRole.IN)
            )
            await session.commit()
        await session.rollback()


@pytest.mark.parametrize("field", ["organization_id", "farm_id"])
async def test_authoritative_tenant_topology_rejects_corruption(
    client: AsyncClient, field: str
) -> None:
    topology = await _topology(client)
    transfer = _transfer(topology)
    setattr(transfer, field, uuid4())
    await _expect_commit_rejected(transfer)


async def test_pre_0015_legacy_transfer_survives_upgrade(
    client: AsyncClient, _engine: AsyncEngine
) -> None:
    """0015 leaves an existing source-only event readable with null topology."""
    topology = await _topology(client)
    legacy_id = uuid4()
    await _engine.dispose()
    api_root = Path(__file__).resolve().parents[1]
    config = Config(str(api_root / "alembic.ini"))
    config.set_main_option("script_location", str(api_root / "alembic"))
    sync_engine = create_engine(os.environ["DATABASE_URL_SYNC"], future=True)
    try:
        command.downgrade(config, "0014_password_recovery")
        with sync_engine.begin() as connection:
            connection.execute(
                text("""
                    INSERT INTO production_events (
                      id, organization_id, farm_id, site_id, unit_id, batch_id,
                      event_type, event_type_version, performed_at, data, attachments,
                      is_final, notes, idempotency_key, payload_hash, created_at
                    ) VALUES (
                      :id, :organization_id, :farm_id, :site_id, :unit_id, :batch_id,
                      'TRANSFER', 2, :performed_at,
                      CAST(:data AS jsonb), NULL, false, NULL, NULL, NULL, :created_at
                    )
                    """),
                {
                    "id": legacy_id,
                    "organization_id": topology["organization_id"],
                    "farm_id": topology["farm_id"],
                    "site_id": topology["source_site_id"],
                    "unit_id": topology["source_unit_id"],
                    "batch_id": topology["source_batch_id"],
                    "performed_at": datetime.now(UTC),
                    "data": '{"quantity": 7, "transfer_loss": 1}',
                    "created_at": datetime.now(UTC),
                },
            )
        command.upgrade(config, "head")
        with sync_engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT transfer_id, transfer_role, data ->> 'quantity' "
                    "FROM production_events WHERE id=:id"
                ),
                {"id": legacy_id},
            ).one()
        assert row == (None, None, "7")
    finally:
        command.upgrade(config, "head")
        sync_engine.dispose()
