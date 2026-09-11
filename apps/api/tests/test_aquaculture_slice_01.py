"""Sprint 3 — Aquaculture Vertical Slice 01 regression tests.

Covers, in order of the sprint spec:

1. Unit-type seeding idempotency + `display_name`/`plural_name`.
2. Valid & invalid payloads for every Sprint-3 event type.
3. Cross-tenant event attempts.
4. Rejection on deleted units / deleted batches.
5. Duplicate idempotency-key replay.
6. Same key + different payload → 409.
7. Atomic event + transition behaviour.
8. MORTALITY > estimated population.
9. TRANSFER: source-unit / destination-unit / cross-farm validation.
10. Final-harvest transition.
11. Stable event timeline pagination.
12. Projection calculations.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from httpx import AsyncClient

from tests._helpers import (
    create_farm,
    feeding_payload,
    harvest_payload,
    mortality_payload,
    sampling_payload,
    stocking_payload,
    transfer_payload,
    water_quality_payload,
)
from tests.test_production_engine import (
    _create_batch,
    _create_unit,
    _new_owner_org_farm,
    _pick_system_unit_type_id,
)

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------- #
# 1. Unit type seeding
# --------------------------------------------------------------------- #
async def test_all_sprint3_aquaculture_unit_types_are_seeded(client: AsyncClient) -> None:
    await _new_owner_org_farm(client)
    r = await client.get("/api/v1/production-unit-types")
    assert r.status_code == 200, r.text
    by_code = {row["code"]: row for row in r.json()}
    for code in (
        "BROODSTOCK_UNIT",
        "INCUBATION_UNIT",
        "HATCHERY_TANK",
        "FRY_TANK",
        "NURSERY_TANK",
        "GROW_OUT_POND",
        "BIOFLOC_TANK",
        "RACEWAY",
        "FLOATING_CAGE",
        "QUARANTINE_UNIT",
    ):
        assert code in by_code, f"missing {code}"
        assert by_code[code]["is_system"] is True
        assert by_code[code]["vertical"] == "aquaculture"
        assert by_code[code]["display_name"], code
    # Pond in the UI, GROW_OUT_POND in the model.
    assert by_code["GROW_OUT_POND"]["display_name"] == "Pond"
    assert by_code["GROW_OUT_POND"]["plural_name"] == "Ponds"
    assert by_code["FLOATING_CAGE"]["display_name"] == "Cage"


async def test_unit_type_seed_is_idempotent(client: AsyncClient) -> None:
    """Re-running the seeder MUST NOT duplicate system rows."""
    from app.seed import seed_permissions_and_roles

    await _new_owner_org_farm(client)
    for _ in range(3):
        await seed_permissions_and_roles()

    r = await client.get("/api/v1/production-unit-types")
    system_codes = [row["code"] for row in r.json() if row["is_system"]]
    assert len(system_codes) == len(set(system_codes)), system_codes


# --------------------------------------------------------------------- #
# 2. Valid payloads for every Sprint-3 event type
# --------------------------------------------------------------------- #
async def _prepare_active_grow_out(client: AsyncClient) -> tuple[str, str, str]:
    """Create an ACTIVE, stocked GROW_OUT_POND batch. Returns
    (batch_id, unit_id, ctx['farm_id'])."""
    ctx = await _new_owner_org_farm(client)
    # Prefer GROW_OUT_POND if visible — otherwise first system type.
    r = await client.get("/api/v1/production-unit-types")
    grow_pond = next(
        (row for row in r.json() if row["code"] == "GROW_OUT_POND"),
        r.json()[0],
    )
    unit_id = await _create_unit(client, ctx["site_id"], grow_pond["id"])
    batch_id = await _create_batch(client, unit_id)

    # STOCKING → STOCKED
    r = await client.post(
        f"/api/v1/batches/{batch_id}/events",
        json={"event_type": "STOCKING", "data": stocking_payload(quantity=25_000)},
    )
    assert r.status_code == 201, r.text

    # STOCKED → ACTIVE (explicit)
    r = await client.post(
        f"/api/v1/batches/{batch_id}/transitions",
        json={"target_state": "active"},
    )
    assert r.status_code == 200, r.text
    return batch_id, unit_id, ctx["farm_id"]


@pytest.mark.parametrize(
    "event_type,payload_factory",
    [
        ("FEEDING", lambda **_: feeding_payload()),
        ("MORTALITY", lambda **_: mortality_payload(count=50)),
        ("SAMPLING", lambda **_: sampling_payload()),
        ("WATER_QUALITY", lambda **_: water_quality_payload()),
    ],
)
async def test_every_event_type_accepts_a_valid_payload(
    client: AsyncClient, event_type: str, payload_factory
) -> None:
    batch_id, _, _ = await _prepare_active_grow_out(client)
    r = await client.post(
        f"/api/v1/batches/{batch_id}/events",
        json={"event_type": event_type, "data": payload_factory()},
    )
    assert r.status_code == 201, r.text


async def test_stocking_transitions_planned_to_stocked(client: AsyncClient) -> None:
    ctx = await _new_owner_org_farm(client)
    ut = await _pick_system_unit_type_id(client, ctx["org_id"])
    unit_id = await _create_unit(client, ctx["site_id"], ut)
    batch_id = await _create_batch(client, unit_id)
    r = await client.post(
        f"/api/v1/batches/{batch_id}/events",
        json={"event_type": "STOCKING", "data": stocking_payload(quantity=1000)},
    )
    assert r.status_code == 201
    r = await client.get(f"/api/v1/batches/{batch_id}")
    assert r.json()["state"] == "stocked"


# --------------------------------------------------------------------- #
# 3. Invalid payloads — schema-level rejections
# --------------------------------------------------------------------- #
async def test_stocking_rejects_missing_species(client: AsyncClient) -> None:
    ctx = await _new_owner_org_farm(client)
    ut = await _pick_system_unit_type_id(client, ctx["org_id"])
    unit_id = await _create_unit(client, ctx["site_id"], ut)
    batch_id = await _create_batch(client, unit_id)

    bad = stocking_payload()
    bad.pop("species_code")
    r = await client.post(
        f"/api/v1/batches/{batch_id}/events",
        json={"event_type": "STOCKING", "data": bad},
    )
    assert r.status_code == 422


async def test_feeding_rejects_zero_quantity(client: AsyncClient) -> None:
    batch_id, _, _ = await _prepare_active_grow_out(client)
    body = feeding_payload(quantity=0)
    r = await client.post(
        f"/api/v1/batches/{batch_id}/events",
        json={"event_type": "FEEDING", "data": body},
    )
    assert r.status_code == 422


async def test_water_quality_rejects_impossible_values(client: AsyncClient) -> None:
    batch_id, _, _ = await _prepare_active_grow_out(client)
    body = water_quality_payload(ph=15.0)  # > 14
    r = await client.post(
        f"/api/v1/batches/{batch_id}/events",
        json={"event_type": "WATER_QUALITY", "data": body},
    )
    assert r.status_code == 422


async def test_sampling_rejects_min_above_average(client: AsyncClient) -> None:
    batch_id, _, _ = await _prepare_active_grow_out(client)
    body = sampling_payload(average_weight=5.0, minimum_weight=6.0)
    r = await client.post(
        f"/api/v1/batches/{batch_id}/events",
        json={"event_type": "SAMPLING", "data": body},
    )
    assert r.status_code == 422


async def test_harvest_total_requires_is_final(client: AsyncClient) -> None:
    batch_id, _, _ = await _prepare_active_grow_out(client)
    body = harvest_payload(harvest_type="total", is_final=False)
    r = await client.post(
        f"/api/v1/batches/{batch_id}/events",
        json={"event_type": "HARVEST", "data": body},
    )
    assert r.status_code == 422


# --------------------------------------------------------------------- #
# 4. Deleted resources cannot accept new events
# --------------------------------------------------------------------- #
async def test_deleted_unit_rejects_new_events(client: AsyncClient) -> None:
    batch_id, unit_id, _ = await _prepare_active_grow_out(client)
    # Close the batch so the unit can be deleted (ACTIVE → FAILED
    # is the only ACTIVE-to-terminal-adjacent transition available
    # for a test-only cleanup path).
    r = await client.post(
        f"/api/v1/batches/{batch_id}/transitions",
        json={"target_state": "failed", "reason": "test-cleanup"},
    )
    assert r.status_code == 200, r.text
    r = await client.delete(f"/api/v1/units/{unit_id}")
    assert r.status_code == 200, r.text
    r = await client.post(
        f"/api/v1/batches/{batch_id}/events",
        json={"event_type": "FEEDING", "data": feeding_payload()},
    )
    assert r.status_code == 404


# --------------------------------------------------------------------- #
# 5. Idempotency — same key + same payload = replay
# --------------------------------------------------------------------- #
async def test_idempotency_replay_for_new_event_types(client: AsyncClient) -> None:
    batch_id, _, _ = await _prepare_active_grow_out(client)
    key = f"idem-{uuid4().hex}"
    body = {"event_type": "SAMPLING", "data": sampling_payload()}
    r1 = await client.post(
        f"/api/v1/batches/{batch_id}/events", json=body, headers={"Idempotency-Key": key}
    )
    assert r1.status_code == 201, r1.text
    r2 = await client.post(
        f"/api/v1/batches/{batch_id}/events", json=body, headers={"Idempotency-Key": key}
    )
    assert r2.status_code == 200
    assert r2.headers.get("X-Idempotent-Replay") == "true"
    assert r2.json()["id"] == r1.json()["id"]


async def test_idempotency_key_conflict_for_new_event_types(client: AsyncClient) -> None:
    batch_id, _, _ = await _prepare_active_grow_out(client)
    key = f"idem-{uuid4().hex}"
    r1 = await client.post(
        f"/api/v1/batches/{batch_id}/events",
        json={"event_type": "FEEDING", "data": feeding_payload(quantity=1.0)},
        headers={"Idempotency-Key": key},
    )
    assert r1.status_code == 201, r1.text
    r2 = await client.post(
        f"/api/v1/batches/{batch_id}/events",
        json={"event_type": "FEEDING", "data": feeding_payload(quantity=9.9)},
        headers={"Idempotency-Key": key},
    )
    assert r2.status_code == 409
    assert r2.json()["detail"]["code"] == "idempotency_key_payload_conflict"


# --------------------------------------------------------------------- #
# 6. Mortality guard
# --------------------------------------------------------------------- #
async def test_mortality_exceeding_population_is_rejected(client: AsyncClient) -> None:
    batch_id, _, _ = await _prepare_active_grow_out(client)
    # We stocked 25_000. 25_001 must be rejected.
    r = await client.post(
        f"/api/v1/batches/{batch_id}/events",
        json={"event_type": "MORTALITY", "data": mortality_payload(count=25_001)},
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "mortality_exceeds_population"


async def test_mortality_on_planned_batch_is_rejected(client: AsyncClient) -> None:
    """MORTALITY on a batch that has never been stocked is rejected —
    the platform never invents negative stock silently."""
    ctx = await _new_owner_org_farm(client)
    ut = await _pick_system_unit_type_id(client, ctx["org_id"])
    unit_id = await _create_unit(client, ctx["site_id"], ut)
    batch_id = await _create_batch(client, unit_id)
    # PLANNED batch — service rejects with mortality_before_stocking.
    # Note: state guard rejects with 409 first in some engines; either
    # is an acceptable "not silently accepted" signal.
    r = await client.post(
        f"/api/v1/batches/{batch_id}/events",
        json={"event_type": "MORTALITY", "data": mortality_payload(count=1)},
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] in (
        "mortality_before_stocking",
        "mortality_exceeds_population",
    )


async def test_cross_tenant_event_write_is_404(client: AsyncClient) -> None:
    """A caller from tenant B cannot log events on tenant A's batches."""
    from tests._helpers import create_verified_user, switch_user

    batch_id, _, _ = await _prepare_active_grow_out(client)

    # Fresh outsider.
    outsider = f"outsider-{uuid4().hex[:8]}@agrovix.dev"
    await create_verified_user(outsider)
    await switch_user(client, outsider)
    # They need at least one org so the auth middleware is happy.
    from tests._helpers import create_org

    await create_org(client, slug=f"out-{uuid4().hex[:6]}")

    r = await client.post(
        f"/api/v1/batches/{batch_id}/events",
        json={"event_type": "FEEDING", "data": feeding_payload()},
    )
    assert r.status_code == 404, r.text


# --------------------------------------------------------------------- #
# 7. Transfer scope
# --------------------------------------------------------------------- #
async def test_transfer_source_must_match_batch_unit(client: AsyncClient) -> None:
    batch_id, _unit_id, _ = await _prepare_active_grow_out(client)
    # A random UUID as the "source" — not this batch's unit.
    body = transfer_payload(
        source_unit_id=str(uuid4()),
        destination_unit_id=str(uuid4()),
        quantity=10,
    )
    r = await client.post(
        f"/api/v1/batches/{batch_id}/events",
        json={"event_type": "TRANSFER", "data": body},
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "transfer_source_changed"


async def test_transfer_destination_not_found(client: AsyncClient) -> None:
    batch_id, unit_id, _ = await _prepare_active_grow_out(client)
    body = transfer_payload(
        source_unit_id=str(unit_id),
        destination_unit_id=str(uuid4()),
        quantity=10,
    )
    r = await client.post(
        f"/api/v1/batches/{batch_id}/events",
        json={"event_type": "TRANSFER", "data": body},
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "transfer_destination_ineligible"


async def test_transfer_cross_farm_is_blocked(client: AsyncClient) -> None:
    batch_id, src_unit_id, farm_id = await _prepare_active_grow_out(client)

    # Fetch parent org id.
    r = await client.get(f"/api/v1/farms/{farm_id}")
    assert r.status_code == 200, r.text
    org_id = r.json()["organization_id"]

    # Same owner: create a second farm within the same org, with a site+unit.
    farm2_id = await create_farm(client, org_id, name="Farm 2")
    r = await client.post(
        f"/api/v1/farms/{farm2_id}/sites",
        json={"name": "Site F2", "code": f"S-{uuid4().hex[:6]}"},
    )
    assert r.status_code == 201, r.text
    site2_id = r.json()["id"]
    ut = await _pick_system_unit_type_id(client, org_id)
    dst_unit_id = await _create_unit(client, site2_id, ut)

    body = transfer_payload(
        source_unit_id=str(src_unit_id),
        destination_unit_id=str(dst_unit_id),
        quantity=100,
    )
    r = await client.post(
        f"/api/v1/batches/{batch_id}/events",
        json={"event_type": "TRANSFER", "data": body},
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "transfer_destination_ineligible"

    # Ensure no event slipped in (atomicity + rejection).
    r = await client.get(f"/api/v1/batches/{batch_id}/events", params={"event_type": "TRANSFER"})
    assert r.json()["items"] == []


# --------------------------------------------------------------------- #
# 8. Final harvest transitions
# --------------------------------------------------------------------- #
async def test_final_harvest_transitions_to_harvested(client: AsyncClient) -> None:
    batch_id, _, _ = await _prepare_active_grow_out(client)
    r = await client.post(
        f"/api/v1/batches/{batch_id}/events",
        json={"event_type": "HARVEST", "data": harvest_payload(is_final=True)},
    )
    assert r.status_code == 201, r.text
    r = await client.get(f"/api/v1/batches/{batch_id}")
    assert r.json()["state"] == "harvested"


async def test_partial_harvest_stays_active(client: AsyncClient) -> None:
    batch_id, _, _ = await _prepare_active_grow_out(client)
    r = await client.post(
        f"/api/v1/batches/{batch_id}/events",
        json={
            "event_type": "HARVEST",
            "data": harvest_payload(
                quantity=1000, total_weight=50.0, harvest_type="partial", is_final=False
            ),
        },
    )
    assert r.status_code == 201, r.text
    r = await client.get(f"/api/v1/batches/{batch_id}")
    assert r.json()["state"] == "active"


# --------------------------------------------------------------------- #
# 9. Projections
# --------------------------------------------------------------------- #
async def test_projections_reflect_event_stream(client: AsyncClient) -> None:
    batch_id, _, _ = await _prepare_active_grow_out(client)
    # 25_000 already stocked. Add feeding and mortality.
    for _ in range(3):
        r = await client.post(
            f"/api/v1/batches/{batch_id}/events",
            json={"event_type": "FEEDING", "data": feeding_payload(quantity=5.0)},
        )
        assert r.status_code == 201
    r = await client.post(
        f"/api/v1/batches/{batch_id}/events",
        json={"event_type": "MORTALITY", "data": mortality_payload(count=500)},
    )
    assert r.status_code == 201

    r = await client.get(f"/api/v1/batches/{batch_id}/projections")
    assert r.status_code == 200, r.text
    proj = r.json()
    assert proj["initial_stocked_quantity"] == 25_000
    assert proj["cumulative_mortality"] == 500
    assert proj["estimated_remaining_population"] == 24_500
    assert proj["total_feed_kg"] == 15.0
    assert proj["survival_rate"] == pytest.approx(24_500 / 25_000)
    assert proj["batch_age_days"] is not None
    assert proj["latest_water_quality"] is None  # not logged yet


async def test_projections_sampling_overrides_population(client: AsyncClient) -> None:
    batch_id, _, _ = await _prepare_active_grow_out(client)
    r = await client.post(
        f"/api/v1/batches/{batch_id}/events",
        json={
            "event_type": "SAMPLING",
            "data": sampling_payload(
                sample_size=50,
                average_weight=6.5,
                minimum_weight=None,
                maximum_weight=None,
                estimated_population=22_000,
            ),
        },
    )
    assert r.status_code == 201

    r = await client.get(f"/api/v1/batches/{batch_id}/projections")
    proj = r.json()
    # Authoritative override
    assert proj["estimated_remaining_population"] == 22_000
    assert proj["latest_average_weight"] == 6.5


# --------------------------------------------------------------------- #
# 10. Timeline pagination stability
# --------------------------------------------------------------------- #
async def test_event_timeline_is_stable_and_cursor_paginated(client: AsyncClient) -> None:
    batch_id, _, _ = await _prepare_active_grow_out(client)
    for i in range(7):
        r = await client.post(
            f"/api/v1/batches/{batch_id}/events",
            json={"event_type": "FEEDING", "data": feeding_payload(quantity=1.0 + i)},
        )
        assert r.status_code == 201

    seen: list[str] = []
    cursor: str | None = None
    while True:
        params: dict = {"limit": 3, "event_type": "FEEDING"}
        if cursor:
            params["cursor"] = cursor
        r = await client.get(f"/api/v1/batches/{batch_id}/events", params=params)
        page = r.json()
        seen.extend(row["id"] for row in page["items"])
        cursor = page.get("next_cursor")
        if not cursor:
            break
    assert len(seen) == 7
    assert len(set(seen)) == 7  # no duplicates
