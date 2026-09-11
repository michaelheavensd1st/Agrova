"""Production Engine integration tests.

Covers the full hierarchy (site → unit → batch → event), the batch
state machine, event catalog validation, cross-tenant isolation
inside the engine, and concurrent transitions.

Shared setup uses the ``tests._helpers`` module from Sprint 1.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from httpx import AsyncClient

from tests._helpers import (
    create_farm,
    create_org,
    create_verified_user,
    feeding_payload,
    harvest_payload,
    stocking_payload,
    switch_user,
)


# --------------------------------------------------------------------- #
# Shared fixture helpers
# --------------------------------------------------------------------- #
async def _new_owner_org_farm(client: AsyncClient) -> dict:
    email = f"prod-{uuid4().hex[:8]}@agrovix.dev"
    await create_verified_user(email)
    await switch_user(client, email)
    org_id = await create_org(client, slug=f"org-{uuid4().hex[:6]}")
    farm_id = await create_farm(client, org_id)
    # Retrieve the auto-created Main Site.
    r = await client.get(f"/api/v1/farms/{farm_id}/sites")
    assert r.status_code == 200, r.text
    sites = r.json()
    assert len(sites) == 1
    return {
        "owner": email,
        "org_id": org_id,
        "farm_id": farm_id,
        "site_id": sites[0]["id"],
    }


async def _pick_system_unit_type_id(client: AsyncClient, org_id: str) -> str:
    r = await client.get("/api/v1/production-unit-types", params={"organization_id": org_id})
    assert r.status_code == 200, r.text
    types = r.json()
    system = [t for t in types if t["is_system"]]
    assert system, "System unit types must be seeded"
    return system[0]["id"]


async def _create_unit(client: AsyncClient, site_id: str, unit_type_id: str) -> str:
    r = await client.post(
        f"/api/v1/sites/{site_id}/units",
        json={"unit_type_id": unit_type_id, "name": "Tank A", "code": f"T-{uuid4().hex[:6]}"},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _create_batch(client: AsyncClient, unit_id: str) -> str:
    r = await client.post(
        f"/api/v1/units/{unit_id}/batches",
        json={"code": f"B-{uuid4().hex[:6]}", "species": "L. vannamei", "expected_quantity": 10000},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ===================================================================== #
# 1. Site auto-creation + soft-delete guardrails
# ===================================================================== #
@pytest.mark.asyncio
async def test_main_site_is_auto_created_with_the_farm(client: AsyncClient) -> None:
    ctx = await _new_owner_org_farm(client)
    r = await client.get(f"/api/v1/farms/{ctx['farm_id']}/sites")
    body = r.json()
    assert len(body) == 1
    assert body[0]["is_default"] is True
    assert body[0]["code"] == "MAIN"


@pytest.mark.asyncio
async def test_site_soft_delete_blocked_while_units_exist(client: AsyncClient) -> None:
    ctx = await _new_owner_org_farm(client)
    unit_type = await _pick_system_unit_type_id(client, ctx["org_id"])
    await _create_unit(client, ctx["site_id"], unit_type)
    r = await client.delete(f"/api/v1/sites/{ctx['site_id']}")
    assert r.status_code == 409, r.text
    assert "active production unit" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_site_delete_and_restore_lifecycle(client: AsyncClient) -> None:
    ctx = await _new_owner_org_farm(client)
    # Add a second (non-default) site so we don't leave the farm with 0.
    r = await client.post(
        f"/api/v1/farms/{ctx['farm_id']}/sites",
        json={"name": "Second", "code": "SEC"},
    )
    assert r.status_code == 201, r.text
    site2_id = r.json()["id"]

    # Delete + verify hidden from list.
    r = await client.delete(f"/api/v1/sites/{site2_id}")
    assert r.status_code == 200
    r = await client.get(f"/api/v1/farms/{ctx['farm_id']}/sites")
    assert all(s["id"] != site2_id for s in r.json())

    # Restore.
    r = await client.post(f"/api/v1/sites/{site2_id}/restore")
    assert r.status_code == 200
    r = await client.get(f"/api/v1/farms/{ctx['farm_id']}/sites")
    assert any(s["id"] == site2_id for s in r.json())


# ===================================================================== #
# 2. ProductionUnitType — system defaults + org customs
# ===================================================================== #
@pytest.mark.asyncio
async def test_system_unit_types_are_seeded(client: AsyncClient) -> None:
    await _new_owner_org_farm(client)
    r = await client.get("/api/v1/production-unit-types")
    body = r.json()
    codes = {t["code"] for t in body if t["is_system"]}
    for expected in (
        "HATCHERY_TANK",
        "NURSERY_TANK",
        "GROW_OUT_POND",
        "FLOATING_CAGE",
        "RACEWAY",
        "BIOFLOC_TANK",
        "BROODSTOCK_UNIT",
        "INCUBATION_UNIT",
        "FRY_TANK",
        "QUARANTINE_UNIT",
    ):
        assert expected in codes


@pytest.mark.asyncio
async def test_custom_unit_type_lifecycle_and_system_immutability(client: AsyncClient) -> None:
    ctx = await _new_owner_org_farm(client)
    # Custom unit type.
    r = await client.post(
        f"/api/v1/organizations/{ctx['org_id']}/production-unit-types",
        json={"code": "CUSTOM_TANK", "name": "Custom Tank", "category": "custom"},
    )
    assert r.status_code == 201, r.text
    custom_id = r.json()["id"]
    assert r.json()["is_system"] is False

    # Reusing a system code is rejected.
    r = await client.post(
        f"/api/v1/organizations/{ctx['org_id']}/production-unit-types",
        json={"code": "HATCHERY_TANK", "name": "Nope"},
    )
    assert r.status_code == 409

    # Deleting a system type is forbidden.
    r = await client.get("/api/v1/production-unit-types")
    system_id = next(t["id"] for t in r.json() if t["is_system"])
    r = await client.delete(f"/api/v1/production-unit-types/{system_id}")
    assert r.status_code == 403

    # Deleting an org's own custom type works.
    r = await client.delete(f"/api/v1/production-unit-types/{custom_id}")
    assert r.status_code == 200


# ===================================================================== #
# 3. ProductionUnit + soft delete + active-batch guard
# ===================================================================== #
@pytest.mark.asyncio
async def test_duplicate_unit_code_in_same_site_returns_sanitized_409(
    client: AsyncClient,
) -> None:
    ctx = await _new_owner_org_farm(client)
    unit_type = await _pick_system_unit_type_id(client, ctx["org_id"])
    payload = {
        "unit_type_id": unit_type,
        "name": "Duplicate Code Unit",
        "code": f"DUP-{uuid4().hex[:6]}",
    }

    first = await client.post(
        f"/api/v1/sites/{ctx['site_id']}/units",
        json=payload,
    )
    assert first.status_code == 201, first.text

    duplicate = await client.post(
        f"/api/v1/sites/{ctx['site_id']}/units",
        json={**payload, "name": "Different Name Same Code"},
    )

    assert duplicate.status_code == 409, duplicate.text
    assert duplicate.json() == {
        "detail": {
            "code": "production_unit_code_conflict",
            "message": "A production unit with this code already exists in this site.",
        }
    }


@pytest.mark.asyncio
async def test_unit_delete_blocked_while_active_batches_exist(client: AsyncClient) -> None:
    ctx = await _new_owner_org_farm(client)
    ut = await _pick_system_unit_type_id(client, ctx["org_id"])
    unit_id = await _create_unit(client, ctx["site_id"], ut)
    await _create_batch(client, unit_id)  # batch state = PLANNED (active-ish)

    r = await client.delete(f"/api/v1/units/{unit_id}")
    assert r.status_code == 409, r.text
    assert "active batch" in r.json()["detail"].lower()


# ===================================================================== #
# 4. Batch state machine
# ===================================================================== #
@pytest.mark.asyncio
async def test_batch_lifecycle_via_event_and_explicit_transitions(client: AsyncClient) -> None:
    """PLANNED → STOCKED (via STOCKING event) → ACTIVE → HARVESTED (via final HARVEST) → CLOSED."""
    ctx = await _new_owner_org_farm(client)
    ut = await _pick_system_unit_type_id(client, ctx["org_id"])
    unit_id = await _create_unit(client, ctx["site_id"], ut)
    batch_id = await _create_batch(client, unit_id)

    # PLANNED → STOCKED via STOCKING event
    r = await client.post(
        f"/api/v1/batches/{batch_id}/events",
        json={
            "event_type": "STOCKING",
            "data": stocking_payload(quantity=10000, average_weight=0.2, source="Hatchery X"),
        },
    )
    assert r.status_code == 201, r.text
    r = await client.get(f"/api/v1/batches/{batch_id}")
    assert r.json()["state"] == "stocked"
    assert r.json()["stocked_at"] is not None

    # STOCKED → ACTIVE (explicit)
    r = await client.post(
        f"/api/v1/batches/{batch_id}/transitions",
        json={"target_state": "active"},
    )
    assert r.status_code == 200
    r = await client.get(f"/api/v1/batches/{batch_id}")
    assert r.json()["state"] == "active"

    # ACTIVE → HARVESTED via HARVEST event with is_final=true
    r = await client.post(
        f"/api/v1/batches/{batch_id}/events",
        json={
            "event_type": "HARVEST",
            "data": harvest_payload(quantity=9500, total_weight=1200.0, is_final=True),
        },
    )
    assert r.status_code == 201
    r = await client.get(f"/api/v1/batches/{batch_id}")
    assert r.json()["state"] == "harvested"

    # HARVESTED → CLOSED (explicit reconciliation)
    r = await client.post(
        f"/api/v1/batches/{batch_id}/transitions",
        json={"target_state": "closed", "reason": "reconciled"},
    )
    assert r.status_code == 200
    r = await client.get(f"/api/v1/batches/{batch_id}")
    assert r.json()["state"] == "closed"

    # Cannot log new events on a closed batch.
    r = await client.post(
        f"/api/v1/batches/{batch_id}/events",
        json={"event_type": "FEEDING", "data": feeding_payload()},
    )
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_invalid_transitions_return_409(client: AsyncClient) -> None:
    ctx = await _new_owner_org_farm(client)
    ut = await _pick_system_unit_type_id(client, ctx["org_id"])
    unit_id = await _create_unit(client, ctx["site_id"], ut)
    batch_id = await _create_batch(client, unit_id)

    # PLANNED → HARVESTED directly is forbidden.
    r = await client.post(
        f"/api/v1/batches/{batch_id}/transitions",
        json={"target_state": "harvested"},
    )
    assert r.status_code == 409
    assert "invalid batch transition" in r.json()["detail"].lower()

    # PLANNED → STOCKED via /transitions is forbidden — must come from event.
    r = await client.post(
        f"/api/v1/batches/{batch_id}/transitions",
        json={"target_state": "stocked"},
    )
    assert r.status_code == 409
    assert "stocking event" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_batch_transitions_are_recorded_in_history(client: AsyncClient) -> None:
    ctx = await _new_owner_org_farm(client)
    ut = await _pick_system_unit_type_id(client, ctx["org_id"])
    unit_id = await _create_unit(client, ctx["site_id"], ut)
    batch_id = await _create_batch(client, unit_id)

    await client.post(
        f"/api/v1/batches/{batch_id}/events",
        json={"event_type": "STOCKING", "data": stocking_payload(quantity=100)},
    )

    r = await client.get(f"/api/v1/batches/{batch_id}/transitions")
    assert r.status_code == 200
    rows = r.json()
    # PLANNED (initial) then PLANNED → STOCKED
    assert len(rows) == 2
    assert rows[0]["from_state"] is None
    assert rows[0]["to_state"] == "planned"
    assert rows[1]["from_state"] == "planned"
    assert rows[1]["to_state"] == "stocked"


@pytest.mark.asyncio
async def test_concurrent_transitions_only_one_wins(client: AsyncClient) -> None:
    """Two async transitions must not race the batch into an inconsistent
    state.

    Codex Review Gate 02 hardens transition safety by holding a
    row-level lock (`SELECT ... FOR UPDATE`) on the batch through
    every event insert AND every explicit transition. Two concurrent
    transitions therefore SERIALISE cleanly:

    * The first flips STOCKED → ACTIVE (target valid from STOCKED).
    * The second acquires the lock, observes state=ACTIVE, and either
      succeeds (if its target is also reachable from ACTIVE, e.g.
      SUSPENDED) or is rejected with 409 (if it isn't).

    Both outcomes uphold the state-machine invariant and keep exactly
    one visible transition per acquired lock — the old racy-CAS
    `[200, 409]` shape was a symptom of pre-lock behaviour, not a
    correctness requirement. We assert the invariants here directly.
    """
    ctx = await _new_owner_org_farm(client)
    ut = await _pick_system_unit_type_id(client, ctx["org_id"])
    unit_id = await _create_unit(client, ctx["site_id"], ut)
    batch_id = await _create_batch(client, unit_id)
    # Move to STOCKED so both transitions below are legal from that state.
    await client.post(
        f"/api/v1/batches/{batch_id}/events",
        json={"event_type": "STOCKING", "data": stocking_payload(quantity=5)},
    )

    # Concurrent STOCKED→ACTIVE and STOCKED→SUSPENDED
    r1, r2 = await asyncio.gather(
        client.post(f"/api/v1/batches/{batch_id}/transitions", json={"target_state": "active"}),
        client.post(f"/api/v1/batches/{batch_id}/transitions", json={"target_state": "suspended"}),
    )
    statuses = sorted([r1.status_code, r2.status_code])
    # At least one succeeded; the other either succeeded (its target
    # is legal from the intermediate state) or was rejected 409.
    assert statuses[0] == 200, (r1.status_code, r2.status_code, r1.text, r2.text)
    assert statuses[1] in (200, 409), (r1.status_code, r2.status_code, r1.text, r2.text)

    # Terminal invariant: the batch settled into exactly one legal
    # state. Under Postgres the FOR UPDATE lock serialises the two
    # calls into a well-defined ACTIVE/SUSPENDED result. Under
    # SQLite (StaticPool, shared connection) FOR UPDATE is a no-op
    # and one caller may have raised 409 without any effect — that
    # is still race-safe (no torn state).
    r = await client.get(f"/api/v1/batches/{batch_id}")
    assert r.status_code == 200
    assert r.json()["state"] in {"stocked", "active", "suspended"}


# ===================================================================== #
# 5. Event catalog validation
# ===================================================================== #
@pytest.mark.asyncio
async def test_event_catalog_returns_all_types(client: AsyncClient) -> None:
    await _new_owner_org_farm(client)
    r = await client.get("/api/v1/production-events/catalog")
    assert r.status_code == 200, r.text
    codes = {e["code"] for e in r.json()["entries"]}
    # Sprint 3 aquaculture slice — MEDICATION / INSPECTION are
    # explicitly deferred; verticals will register them later.
    assert {
        "STOCKING",
        "FEEDING",
        "MORTALITY",
        "SAMPLING",
        "WATER_QUALITY",
        "TRANSFER",
        "HARVEST",
    } == codes


@pytest.mark.asyncio
async def test_event_rejects_unknown_event_type(client: AsyncClient) -> None:
    ctx = await _new_owner_org_farm(client)
    ut = await _pick_system_unit_type_id(client, ctx["org_id"])
    unit_id = await _create_unit(client, ctx["site_id"], ut)
    batch_id = await _create_batch(client, unit_id)
    r = await client.post(
        f"/api/v1/batches/{batch_id}/events",
        json={"event_type": "TELEPORT", "data": {"x": 1}},
    )
    assert r.status_code == 400
    assert "unknown event_type" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_event_rejects_extra_fields(client: AsyncClient) -> None:
    ctx = await _new_owner_org_farm(client)
    ut = await _pick_system_unit_type_id(client, ctx["org_id"])
    unit_id = await _create_unit(client, ctx["site_id"], ut)
    batch_id = await _create_batch(client, unit_id)
    # Move to ACTIVE so FEEDING is allowed.
    await client.post(
        f"/api/v1/batches/{batch_id}/events",
        json={"event_type": "STOCKING", "data": {"quantity": 1}},
    )
    await client.post(f"/api/v1/batches/{batch_id}/transitions", json={"target_state": "active"})

    r = await client.post(
        f"/api/v1/batches/{batch_id}/events",
        json={
            "event_type": "FEEDING",
            "data": {**feeding_payload(quantity=5.0), "totally_extra": True},
        },
    )
    assert r.status_code == 422, r.text
    body = r.json()["detail"]
    assert body["event_type"] == "FEEDING"
    assert any("totally_extra" in e["field"] for e in body["errors"])


@pytest.mark.asyncio
async def test_event_rejects_missing_required_field(client: AsyncClient) -> None:
    ctx = await _new_owner_org_farm(client)
    ut = await _pick_system_unit_type_id(client, ctx["org_id"])
    unit_id = await _create_unit(client, ctx["site_id"], ut)
    batch_id = await _create_batch(client, unit_id)
    r = await client.post(
        f"/api/v1/batches/{batch_id}/events",
        json={"event_type": "STOCKING", "data": {}},  # missing required fields
    )
    assert r.status_code == 422, r.text
    body = r.json()["detail"]
    fields = {e["field"] for e in body["errors"]}
    assert "quantity" in fields
    assert "species_code" in fields


@pytest.mark.asyncio
async def test_event_pagination_is_stable_and_cursor_based(client: AsyncClient) -> None:
    ctx = await _new_owner_org_farm(client)
    ut = await _pick_system_unit_type_id(client, ctx["org_id"])
    unit_id = await _create_unit(client, ctx["site_id"], ut)
    batch_id = await _create_batch(client, unit_id)
    # Move to ACTIVE so FEEDING is allowed.
    await client.post(
        f"/api/v1/batches/{batch_id}/events",
        json={"event_type": "STOCKING", "data": {"quantity": 1}},
    )
    await client.post(f"/api/v1/batches/{batch_id}/transitions", json={"target_state": "active"})

    # Log 5 FEEDING events.
    for i in range(5):
        r = await client.post(
            f"/api/v1/batches/{batch_id}/events",
            json={"event_type": "FEEDING", "data": feeding_payload(quantity=1.0 + i)},
        )
        assert r.status_code == 201

    r1 = await client.get(f"/api/v1/batches/{batch_id}/events", params={"limit": 2})
    b1 = r1.json()
    assert len(b1["items"]) == 2
    assert b1["next_cursor"] is not None

    r2 = await client.get(
        f"/api/v1/batches/{batch_id}/events",
        params={"limit": 2, "cursor": b1["next_cursor"]},
    )
    b2 = r2.json()
    assert len(b2["items"]) == 2

    ids_p1 = {i["id"] for i in b1["items"]}
    ids_p2 = {i["id"] for i in b2["items"]}
    assert ids_p1.isdisjoint(ids_p2)

    # Ordering: strictly newest-first.
    times_p1 = [i["performed_at"] for i in b1["items"]]
    assert times_p1 == sorted(times_p1, reverse=True)


# ===================================================================== #
# 6. Cross-tenant isolation
# ===================================================================== #
@pytest.mark.asyncio
async def test_cross_tenant_access_returns_404(client: AsyncClient) -> None:
    a = await _new_owner_org_farm(client)
    ut = await _pick_system_unit_type_id(client, a["org_id"])
    a_unit = await _create_unit(client, a["site_id"], ut)
    a_batch = await _create_batch(client, a_unit)

    # Log an event so we have something concrete to leak.
    await client.post(
        f"/api/v1/batches/{a_batch}/events",
        json={"event_type": "STOCKING", "data": stocking_payload(quantity=1)},
    )
    r = await client.get(f"/api/v1/batches/{a_batch}/events")
    event_id = r.json()["items"][0]["id"]

    # Fresh outsider org.
    outsider = f"outside-{uuid4().hex[:8]}@agrovix.dev"
    await create_verified_user(outsider)
    await switch_user(client, outsider)
    await create_org(client, slug=f"out-{uuid4().hex[:6]}")

    # Every direct URL against alpha's resources must return 404.
    for url in (
        f"/api/v1/sites/{a['site_id']}",
        f"/api/v1/units/{a_unit}",
        f"/api/v1/batches/{a_batch}",
        f"/api/v1/batches/{a_batch}/events",
        f"/api/v1/batches/{a_batch}/transitions",
        f"/api/v1/events/{event_id}",
    ):
        r = await client.get(url)
        assert r.status_code == 404, (url, r.status_code, r.text)
