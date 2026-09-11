"""Canonical paired aquaculture transfer and checkpoint regressions."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.production import ProductionEvent, ProductionTransfer, ProductionTransferRole
from app.repositories.production import ProductionEventRepository
from tests._helpers import harvest_payload, mortality_payload, sampling_payload, transfer_payload
from tests.test_codex_review_gate_02 import _prepare_active_batch, _prepare_receiving_batch
from tests.test_production_engine import _create_unit

pytestmark = pytest.mark.asyncio

_postgres_only = pytest.mark.skipif(
    "postgresql" not in os.environ.get("DATABASE_URL", ""),
    reason="Requires PostgreSQL row locking and independent request transactions.",
)


async def _pair(client: AsyncClient, *, loss: int = 5):
    source = await _prepare_active_batch(client, quantity=1000)
    destination_unit = await _create_unit(client, source["site_id"], source["unit_type_id"])
    destination_batch = await _prepare_receiving_batch(client, destination_unit, quantity=100)
    body = {
        "event_type": "TRANSFER",
        "data": transfer_payload(
            source_unit_id=source["unit_id"],
            destination_unit_id=destination_unit,
            destination_batch_id=destination_batch,
            quantity=200,
            transfer_loss=loss,
        ),
    }
    return source, str(destination_unit), destination_batch, body


async def test_transfer_creates_atomic_out_in_pair_and_net_projections(
    client: AsyncClient,
) -> None:
    source, _unit, destination, body = await _pair(client)
    response = await client.post(
        f"/api/v1/batches/{source['batch_id']}/events",
        json=body,
        headers={"Idempotency-Key": "paired-transfer-1"},
    )
    assert response.status_code == 201, response.text
    out = response.json()
    assert out["transfer_role"] == "out"
    assert out["transfer_id"]

    source_events = (await client.get(f"/api/v1/batches/{source['batch_id']}/events")).json()[
        "items"
    ]
    destination_events = (await client.get(f"/api/v1/batches/{destination}/events")).json()["items"]
    source_transfer = next(event for event in source_events if event["event_type"] == "TRANSFER")
    destination_transfer = next(
        event for event in destination_events if event["event_type"] == "TRANSFER"
    )
    assert source_transfer["transfer_role"] == "out"
    assert destination_transfer["transfer_role"] == "in"
    assert source_transfer["transfer_id"] == destination_transfer["transfer_id"]

    source_projection = (
        await client.get(f"/api/v1/batches/{source['batch_id']}/projections")
    ).json()
    destination_projection = (await client.get(f"/api/v1/batches/{destination}/projections")).json()
    assert source_projection["estimated_remaining_population"] == 795
    assert source_projection["cumulative_transfer_out"] == 200
    assert source_projection["cumulative_mortality"] == 5
    assert source_projection["survival_rate"] == pytest.approx(0.795)
    assert destination_projection["estimated_remaining_population"] == 300
    assert destination_projection["cumulative_transfer_in"] == 200
    assert destination_projection["cumulative_mortality"] == 0
    assert destination_projection["estimated_biomass_kg"] == pytest.approx(0.84)
    assert destination_projection["survival_rate"] == pytest.approx(1.0)


async def test_transfer_idempotency_replays_complete_pair(client: AsyncClient) -> None:
    source, _unit, destination, body = await _pair(client, loss=0)
    path = f"/api/v1/batches/{source['batch_id']}/events"
    first = await client.post(path, json=body, headers={"Idempotency-Key": "paired-replay"})
    replay = await client.post(path, json=body, headers={"Idempotency-Key": "paired-replay"})
    assert first.status_code == 201
    assert replay.status_code == 200
    assert replay.json()["id"] == first.json()["id"]
    destination_events = (await client.get(f"/api/v1/batches/{destination}/events")).json()["items"]
    assert len([event for event in destination_events if event["event_type"] == "TRANSFER"]) == 1
    conflict_body = {"event_type": "TRANSFER", "data": {**body["data"], "quantity": 201}}
    conflict = await client.post(
        path, json=conflict_body, headers={"Idempotency-Key": "paired-replay"}
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "idempotency_key_payload_conflict"


async def test_transfer_uses_validated_chronology_for_both_pair_events(
    client: AsyncClient,
) -> None:
    source, _unit, destination, body = await _pair(client, loss=0)
    transferred_at = "2025-01-02T03:04:05-05:00"
    body["data"]["transferred_at"] = transferred_at
    response = await client.post(f"/api/v1/batches/{source['batch_id']}/events", json=body)
    assert response.status_code == 201, response.text

    source_events = (await client.get(f"/api/v1/batches/{source['batch_id']}/events")).json()[
        "items"
    ]
    destination_events = (await client.get(f"/api/v1/batches/{destination}/events")).json()["items"]
    outgoing = next(event for event in source_events if event["event_type"] == "TRANSFER")
    incoming = next(event for event in destination_events if event["event_type"] == "TRANSFER")
    expected = datetime.fromisoformat(transferred_at).astimezone(UTC)
    outgoing_at = datetime.fromisoformat(outgoing["performed_at"])
    incoming_at = datetime.fromisoformat(incoming["performed_at"])
    if outgoing_at.tzinfo is None:
        outgoing_at = outgoing_at.replace(tzinfo=UTC)
    if incoming_at.tzinfo is None:
        incoming_at = incoming_at.replace(tzinfo=UTC)
    assert outgoing_at == expected
    assert incoming_at == expected
    assert outgoing["performed_at"] == incoming["performed_at"]


async def test_explicit_top_level_transfer_chronology_takes_precedence(
    client: AsyncClient,
) -> None:
    source, _unit, _destination, body = await _pair(client, loss=0)
    body["data"]["transferred_at"] = "2025-01-01T00:00:00+00:00"
    body["performed_at"] = "2025-02-01T00:00:00+02:00"
    response = await client.post(f"/api/v1/batches/{source['batch_id']}/events", json=body)
    assert response.status_code == 201, response.text
    expected = datetime.fromisoformat(body["performed_at"]).astimezone(UTC)
    assert datetime.fromisoformat(response.json()["performed_at"]) == expected


async def test_backdated_transfer_before_sampling_does_not_fold_after_checkpoint(
    client: AsyncClient,
) -> None:
    source, _unit, _destination, body = await _pair(client, loss=5)
    sampling_at = datetime.now(UTC)
    body["data"]["transferred_at"] = (sampling_at - timedelta(days=365)).isoformat()
    sampling = await client.post(
        f"/api/v1/batches/{source['batch_id']}/events",
        json={
            "event_type": "SAMPLING",
            "performed_at": sampling_at.isoformat(),
            "data": sampling_payload(estimated_population=700),
        },
    )
    assert sampling.status_code == 201, sampling.text
    transfer = await client.post(f"/api/v1/batches/{source['batch_id']}/events", json=body)
    assert transfer.status_code == 201, transfer.text
    projection = (await client.get(f"/api/v1/batches/{source['batch_id']}/projections")).json()
    assert projection["estimated_remaining_population"] == 700


async def test_transfer_after_sampling_folds_after_checkpoint(client: AsyncClient) -> None:
    source, _unit, _destination, body = await _pair(client, loss=5)
    sampling_at = datetime.now(UTC)
    body["data"]["transferred_at"] = (sampling_at + timedelta(days=365)).isoformat()
    sampling = await client.post(
        f"/api/v1/batches/{source['batch_id']}/events",
        json={
            "event_type": "SAMPLING",
            "performed_at": sampling_at.isoformat(),
            "data": sampling_payload(estimated_population=700),
        },
    )
    assert sampling.status_code == 201, sampling.text
    transfer = await client.post(f"/api/v1/batches/{source['batch_id']}/events", json=body)
    assert transfer.status_code == 201, transfer.text
    projection = (await client.get(f"/api/v1/batches/{source['batch_id']}/projections")).json()
    assert projection["estimated_remaining_population"] == 495


async def test_transfer_idempotency_conflicts_when_transferred_at_changes(
    client: AsyncClient,
) -> None:
    source, _unit, _destination, body = await _pair(client, loss=0)
    path = f"/api/v1/batches/{source['batch_id']}/events"
    first = await client.post(path, json=body, headers={"Idempotency-Key": "chronology-replay"})
    assert first.status_code == 201, first.text
    changed = {
        **body,
        "data": {**body["data"], "transferred_at": "2032-01-01T00:00:00+00:00"},
    }
    conflict = await client.post(
        path,
        json=changed,
        headers={"Idempotency-Key": "chronology-replay"},
    )
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["detail"]["code"] == "idempotency_key_payload_conflict"


@_postgres_only
async def test_concurrent_transfer_replay_leaves_one_complete_pair(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    source, _unit, _destination, body = await _pair(client, loss=0)
    path = f"/api/v1/batches/{source['batch_id']}/events"
    first, second = await asyncio.gather(
        client.post(path, json=body, headers={"Idempotency-Key": "paired-concurrent"}),
        client.post(path, json=body, headers={"Idempotency-Key": "paired-concurrent"}),
    )
    assert sorted((first.status_code, second.status_code)) == [200, 201]
    assert first.json()["id"] == second.json()["id"]

    await db_session.rollback()
    transfer_count = await db_session.scalar(
        select(func.count(ProductionTransfer.id)).where(
            ProductionTransfer.source_batch_id == UUID(str(source["batch_id"])),
            ProductionTransfer.idempotency_key == "paired-concurrent",
        )
    )
    transfer_id = UUID(first.json()["transfer_id"])
    event_count = await db_session.scalar(
        select(func.count(ProductionEvent.id)).where(ProductionEvent.transfer_id == transfer_id)
    )
    assert transfer_count == 1
    assert event_count == 2


@_postgres_only
async def test_concurrent_transfers_cannot_overshoot_or_leave_orphans(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    source = await _prepare_active_batch(client, quantity=100)
    destination_unit = await _create_unit(client, source["site_id"], source["unit_type_id"])
    destination_batch = await _prepare_receiving_batch(client, destination_unit, quantity=100)
    body = {
        "event_type": "TRANSFER",
        "data": transfer_payload(
            source_unit_id=source["unit_id"],
            destination_unit_id=destination_unit,
            destination_batch_id=destination_batch,
            quantity=60,
        ),
    }
    path = f"/api/v1/batches/{source['batch_id']}/events"
    first, second = await asyncio.gather(client.post(path, json=body), client.post(path, json=body))
    assert sorted((first.status_code, second.status_code)) == [201, 409]

    await db_session.rollback()
    transfer_ids = list(
        (
            await db_session.scalars(
                select(ProductionTransfer.id).where(
                    ProductionTransfer.source_batch_id == UUID(str(source["batch_id"]))
                )
            )
        ).all()
    )
    assert len(transfer_ids) == 1
    event_count = await db_session.scalar(
        select(func.count(ProductionEvent.id)).where(ProductionEvent.transfer_id == transfer_ids[0])
    )
    orphan_count = await db_session.scalar(
        select(func.count(ProductionTransfer.id))
        .outerjoin(ProductionEvent, ProductionEvent.transfer_id == ProductionTransfer.id)
        .group_by(ProductionTransfer.id)
        .having(func.count(ProductionEvent.id) != 2)
    )
    assert event_count == 2
    assert orphan_count is None


async def test_destination_write_failure_rolls_back_both_sides(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _unit, _destination, body = await _pair(client)
    original = ProductionEventRepository.create

    async def fail_destination(self, **kwargs):
        if kwargs.get("transfer_role") == ProductionTransferRole.IN:
            raise RuntimeError("injected destination failure")
        return await original(self, **kwargs)

    monkeypatch.setattr(ProductionEventRepository, "create", fail_destination)
    with pytest.raises(RuntimeError, match="injected destination failure"):
        await client.post(f"/api/v1/batches/{source['batch_id']}/events", json=body)

    transfer_count = await db_session.scalar(
        select(func.count(ProductionTransfer.id)).where(
            ProductionTransfer.source_batch_id == UUID(str(source["batch_id"]))
        )
    )
    event_count = await db_session.scalar(
        select(func.count(ProductionEvent.id)).where(
            ProductionEvent.batch_id == UUID(str(source["batch_id"])),
            ProductionEvent.transfer_id.is_not(None),
        )
    )
    assert transfer_count == 0
    assert event_count == 0


async def test_sampling_is_checkpoint_then_later_population_events_fold(
    client: AsyncClient,
) -> None:
    ctx = await _prepare_active_batch(client, quantity=1000)
    sampling = await client.post(
        f"/api/v1/batches/{ctx['batch_id']}/events",
        json={
            "event_type": "SAMPLING",
            "data": sampling_payload(estimated_population=700),
        },
    )
    assert sampling.status_code == 201
    mortality = await client.post(
        f"/api/v1/batches/{ctx['batch_id']}/events",
        json={"event_type": "MORTALITY", "data": mortality_payload(count=20)},
    )
    assert mortality.status_code == 201
    projection = (await client.get(f"/api/v1/batches/{ctx['batch_id']}/projections")).json()
    assert projection["estimated_remaining_population"] == 680
    assert projection["cumulative_mortality"] == 20
    second_sampling = await client.post(
        f"/api/v1/batches/{ctx['batch_id']}/events",
        json={
            "event_type": "SAMPLING",
            "data": sampling_payload(estimated_population=650),
        },
    )
    assert second_sampling.status_code == 201
    harvest = await client.post(
        f"/api/v1/batches/{ctx['batch_id']}/events",
        json={
            "event_type": "HARVEST",
            "data": harvest_payload(
                quantity=50,
                total_weight=10,
                harvest_type="partial",
                is_final=False,
            ),
        },
    )
    assert harvest.status_code == 201
    projection = (await client.get(f"/api/v1/batches/{ctx['batch_id']}/projections")).json()
    assert projection["estimated_remaining_population"] == 600


async def test_latest_sampling_checkpoint_folds_later_transfer_on_both_sides(
    client: AsyncClient,
) -> None:
    source, _unit, destination, body = await _pair(client, loss=5)
    for batch_id, estimate in ((source["batch_id"], 900), (destination, 80)):
        response = await client.post(
            f"/api/v1/batches/{batch_id}/events",
            json={
                "event_type": "SAMPLING",
                "data": sampling_payload(estimated_population=estimate),
            },
        )
        assert response.status_code == 201
    body["data"]["transferred_at"] = datetime.now(UTC).isoformat()
    transfer = await client.post(f"/api/v1/batches/{source['batch_id']}/events", json=body)
    assert transfer.status_code == 201, transfer.text
    source_projection = (
        await client.get(f"/api/v1/batches/{source['batch_id']}/projections")
    ).json()
    destination_projection = (await client.get(f"/api/v1/batches/{destination}/projections")).json()
    assert source_projection["estimated_remaining_population"] == 695
    assert destination_projection["estimated_remaining_population"] == 280


async def test_equal_time_mortality_after_sampling_folds_after_checkpoint(
    client: AsyncClient,
) -> None:
    ctx = await _prepare_active_batch(client, quantity=1000)
    performed_at = datetime(2030, 1, 1, tzinfo=UTC).isoformat()
    sampling = await client.post(
        f"/api/v1/batches/{ctx['batch_id']}/events",
        json={
            "event_type": "SAMPLING",
            "performed_at": performed_at,
            "data": sampling_payload(estimated_population=700),
        },
    )
    mortality = await client.post(
        f"/api/v1/batches/{ctx['batch_id']}/events",
        json={
            "event_type": "MORTALITY",
            "performed_at": performed_at,
            "data": mortality_payload(count=20),
        },
    )
    assert sampling.status_code == mortality.status_code == 201
    projection = (await client.get(f"/api/v1/batches/{ctx['batch_id']}/projections")).json()
    assert projection["estimated_remaining_population"] == 680


async def test_equal_time_sampling_after_mortality_supersedes_mortality(
    client: AsyncClient,
) -> None:
    ctx = await _prepare_active_batch(client, quantity=1000)
    performed_at = datetime(2030, 1, 2, tzinfo=UTC).isoformat()
    mortality = await client.post(
        f"/api/v1/batches/{ctx['batch_id']}/events",
        json={
            "event_type": "MORTALITY",
            "performed_at": performed_at,
            "data": mortality_payload(count=20),
        },
    )
    sampling = await client.post(
        f"/api/v1/batches/{ctx['batch_id']}/events",
        json={
            "event_type": "SAMPLING",
            "performed_at": performed_at,
            "data": sampling_payload(estimated_population=700),
        },
    )
    assert mortality.status_code == sampling.status_code == 201
    projection = (await client.get(f"/api/v1/batches/{ctx['batch_id']}/projections")).json()
    assert projection["estimated_remaining_population"] == 700


async def test_equal_time_transfer_pair_folds_after_both_sampling_checkpoints(
    client: AsyncClient,
) -> None:
    source, _unit, destination, body = await _pair(client, loss=5)
    performed_at = datetime(2030, 1, 3, tzinfo=UTC).isoformat()
    for batch_id, estimate in ((source["batch_id"], 900), (destination, 80)):
        response = await client.post(
            f"/api/v1/batches/{batch_id}/events",
            json={
                "event_type": "SAMPLING",
                "performed_at": performed_at,
                "data": sampling_payload(estimated_population=estimate),
            },
        )
        assert response.status_code == 201
    body["data"]["transferred_at"] = performed_at
    transfer = await client.post(f"/api/v1/batches/{source['batch_id']}/events", json=body)
    assert transfer.status_code == 201, transfer.text
    source_projection = (
        await client.get(f"/api/v1/batches/{source['batch_id']}/projections")
    ).json()
    destination_projection = (await client.get(f"/api/v1/batches/{destination}/projections")).json()
    assert source_projection["estimated_remaining_population"] == 695
    assert destination_projection["estimated_remaining_population"] == 280


async def test_equal_time_harvest_after_sampling_folds_after_checkpoint(
    client: AsyncClient,
) -> None:
    ctx = await _prepare_active_batch(client, quantity=1000)
    performed_at = datetime(2030, 1, 4, tzinfo=UTC).isoformat()
    sampling = await client.post(
        f"/api/v1/batches/{ctx['batch_id']}/events",
        json={
            "event_type": "SAMPLING",
            "performed_at": performed_at,
            "data": sampling_payload(estimated_population=700),
        },
    )
    harvest = await client.post(
        f"/api/v1/batches/{ctx['batch_id']}/events",
        json={
            "event_type": "HARVEST",
            "performed_at": performed_at,
            "data": harvest_payload(
                quantity=50,
                total_weight=10,
                harvest_type="partial",
                is_final=False,
            ),
        },
    )
    assert sampling.status_code == harvest.status_code == 201
    projection = (await client.get(f"/api/v1/batches/{ctx['batch_id']}/projections")).json()
    assert projection["estimated_remaining_population"] == 650


async def test_equal_time_later_sampling_checkpoint_wins(client: AsyncClient) -> None:
    ctx = await _prepare_active_batch(client, quantity=1000)
    performed_at = datetime(2030, 1, 5, tzinfo=UTC).isoformat()
    for estimate in (700, 650):
        response = await client.post(
            f"/api/v1/batches/{ctx['batch_id']}/events",
            json={
                "event_type": "SAMPLING",
                "performed_at": performed_at,
                "data": sampling_payload(estimated_population=estimate),
            },
        )
        assert response.status_code == 201
    projection = (await client.get(f"/api/v1/batches/{ctx['batch_id']}/projections")).json()
    assert projection["estimated_remaining_population"] == 650


@pytest.mark.parametrize(
    ("field", "changed_value"),
    [
        ("performed_at", "2031-01-01T00:00:00+00:00"),
        ("notes", "changed transfer note"),
        ("attachments", [{"name": "changed-evidence.pdf"}]),
    ],
)
async def test_transfer_idempotency_conflicts_on_request_metadata_change(
    client: AsyncClient, field: str, changed_value: object
) -> None:
    source, _unit, _destination, body = await _pair(client, loss=0)
    body.update(
        {
            "performed_at": "2030-01-01T00:00:00+00:00",
            "notes": "original transfer note",
            "attachments": [{"name": "original-evidence.pdf"}],
        }
    )
    path = f"/api/v1/batches/{source['batch_id']}/events"
    first = await client.post(path, json=body, headers={"Idempotency-Key": f"metadata-{field}"})
    assert first.status_code == 201, first.text
    changed_body = {**body, field: changed_value}
    conflict = await client.post(
        path,
        json=changed_body,
        headers={"Idempotency-Key": f"metadata-{field}"},
    )
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["detail"]["code"] == "idempotency_key_payload_conflict"
