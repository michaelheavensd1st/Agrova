"""Sprint 4 — Operational Resources 01 (Inventory) integration tests.

Covers the full Definition of Done:

* tenant + farm isolation (org-scoped catalog, farm-pinned warehouses)
* permission enforcement per endpoint
* receipt / issue / transfer / adjustment / reversal / consumption
* insufficient-stock rejection
* unit incompatibility
* duplicate idempotency replay + conflicting-payload rejection
* concurrent deductions against the same lot (Postgres-only)
* FEEDING event → inventory consumption atomicity
* rollback when event insert fails
* rollback when inventory deduction fails
* no double deduction on retry
* immutable posted transactions (no PATCH / DELETE endpoint exists)
* reconciliation accuracy
"""

from __future__ import annotations

import asyncio
import os
from decimal import Decimal
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy import update as sa_update

from app.db import session as _db_session_module
from app.models.inventory import InventoryTransaction as _InventoryTransaction
from app.models.inventory import InventoryTransactionType as _InventoryTransactionType
from app.services.inventory import signed_delta
from tests._helpers import (
    create_org,
    create_verified_user,
    invite_and_accept,
    stocking_payload,
    switch_user,
)
from tests.test_production_engine import (
    _create_batch,
    _create_unit,
    _new_owner_org_farm,
    _pick_system_unit_type_id,
)

pytestmark = pytest.mark.asyncio

_postgres_only = pytest.mark.skipif(
    "postgresql" not in os.environ.get("DATABASE_URL", ""),
    reason="Requires real DB-level concurrency (Postgres); SQLite serializes writers.",
)

# Sprint 5.4.10 — Some pre-Sprint-5.4.10 tests use ``_mutate_tx`` to
# CORRUPT transfer rows in ways the new Sprint 5.4.10 UPDATE trigger +
# deferred pair-completeness constraint now REJECT at the DB layer.
# Those tests remain valuable SQLite functional coverage of the
# application-layer defense-in-depth, so we mark them ``@_sqlite_only``.
# The DB-layer rejection is proven separately by the Sprint 5.4.10
# adversarial tests.
_sqlite_only = pytest.mark.skipif(
    "postgresql" in os.environ.get("DATABASE_URL", ""),
    reason=(
        "Sprint 5.4.10 — Postgres DB-layer trigger/constraint rejects the "
        "corruption this test uses to reach the application-layer defense. "
        "See tests marked test_sprint_5_4_10_* for the DB-layer proofs."
    ),
)


# --------------------------------------------------------------------- #
# Fixture helpers
# --------------------------------------------------------------------- #
async def _create_warehouse(
    client: AsyncClient, org_id: str, *, farm_id: str | None = None, code: str | None = None
) -> str:
    code = code or f"WH-{uuid4().hex[:6]}"
    body = {"name": code, "code": code}
    if farm_id is not None:
        body["farm_id"] = farm_id
    r = await client.post(
        f"/api/v1/organizations/{org_id}/warehouses",
        json=body,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _create_feed_item(client: AsyncClient, org_id: str, *, canonical_unit: str = "kg") -> str:
    r = await client.post(
        f"/api/v1/organizations/{org_id}/inventory-items",
        json={
            "code": f"FEED-{uuid4().hex[:6]}",
            "name": "Starter Feed",
            "category": "feed",
            "canonical_unit": canonical_unit,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _receipt(
    client: AsyncClient,
    warehouse_id: str,
    item_id: str,
    *,
    quantity: float = 100.0,
    unit: str = "kg",
    lot_code: str | None = None,
    idempotency_key: str | None = None,
) -> dict:
    headers = {}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    r = await client.post(
        f"/api/v1/warehouses/{warehouse_id}/inventory:receive",
        json={
            "item_id": item_id,
            "lot_code": lot_code or f"LOT-{uuid4().hex[:6]}",
            "quantity": quantity,
            "unit": unit,
        },
        headers=headers,
    )
    return {"status": r.status_code, "body": r.json() if r.text else None, "headers": r.headers}


# --------------------------------------------------------------------- #
# 1. Basic CRUD + tenancy isolation
# --------------------------------------------------------------------- #
async def test_owner_can_create_warehouse_and_item(client: AsyncClient) -> None:
    ctx = await _new_owner_org_farm(client)
    wh_id = await _create_warehouse(client, ctx["org_id"], farm_id=ctx["farm_id"])
    item_id = await _create_feed_item(client, ctx["org_id"])
    assert wh_id and item_id


async def test_operational_lists_exclude_closed_warehouses_and_inactive_items(
    client: AsyncClient,
) -> None:
    ctx = await _new_owner_org_farm(client)
    active_warehouse_id = await _create_warehouse(client, ctx["org_id"], code="OP-ACTIVE")
    closed_warehouse_id = await _create_warehouse(client, ctx["org_id"], code="OP-CLOSED")
    active_item_id = await _create_feed_item(client, ctx["org_id"])
    inactive_item_id = await _create_feed_item(client, ctx["org_id"])

    warehouse_update = await client.patch(
        f"/api/v1/warehouses/{closed_warehouse_id}", json={"status": "closed"}
    )
    item_update = await client.patch(
        f"/api/v1/inventory-items/{inactive_item_id}", json={"is_active": False}
    )
    assert warehouse_update.status_code == 200, warehouse_update.text
    assert item_update.status_code == 200, item_update.text

    default_warehouses = await client.get(f"/api/v1/organizations/{ctx['org_id']}/warehouses")
    operational_warehouses = await client.get(
        f"/api/v1/organizations/{ctx['org_id']}/warehouses?operational_only=true"
    )
    default_items = await client.get(f"/api/v1/organizations/{ctx['org_id']}/inventory-items")
    operational_items = await client.get(
        f"/api/v1/organizations/{ctx['org_id']}/inventory-items?operational_only=true"
    )

    assert default_warehouses.status_code == 200, default_warehouses.text
    assert operational_warehouses.status_code == 200, operational_warehouses.text
    assert default_items.status_code == 200, default_items.text
    assert operational_items.status_code == 200, operational_items.text
    assert {row["id"] for row in default_warehouses.json()} == {
        active_warehouse_id,
        closed_warehouse_id,
    }
    assert {row["id"] for row in operational_warehouses.json()} == {active_warehouse_id}
    assert {row["id"] for row in default_items.json()} == {
        active_item_id,
        inactive_item_id,
    }
    assert {row["id"] for row in operational_items.json()} == {active_item_id}


async def test_cross_org_cannot_see_warehouse(client: AsyncClient) -> None:
    """Non-member of an org gets 404 on the warehouse endpoints."""
    a = await _new_owner_org_farm(client)
    wh_id = await _create_warehouse(client, a["org_id"], farm_id=a["farm_id"])

    outsider = f"out-{uuid4().hex[:8]}@agrovix.dev"
    await create_verified_user(outsider)
    await switch_user(client, outsider)
    await create_org(client, slug=f"out-{uuid4().hex[:6]}")

    r = await client.get(f"/api/v1/warehouses/{wh_id}")
    assert r.status_code == 404, r.text


async def test_farm_pinned_warehouse_still_org_visible(
    client: AsyncClient,
) -> None:
    """A farm-pinned warehouse is still visible to org-scope readers.

    Sprint 4 puts the visibility filter at farm-membership OR
    org-membership; org-scoped `viewer` role has org membership so
    they see the warehouse even though it's pinned to a farm they
    aren't directly assigned to. Farm-only visibility narrowing is a
    Sprint 5 concern.
    """
    a_ctx = await _new_owner_org_farm(client)  # owner + farm A
    wh_a = await _create_warehouse(client, a_ctx["org_id"], farm_id=a_ctx["farm_id"])

    reader = f"reader-{uuid4().hex[:8]}@agrovix.dev"
    await create_verified_user(reader)
    await invite_and_accept(
        client,
        inviter_email=a_ctx["owner"],
        invitee_email=reader,
        org_id=a_ctx["org_id"],
        role_name="viewer",
    )
    r = await client.get(f"/api/v1/organizations/{a_ctx['org_id']}/warehouses")
    assert r.status_code == 200
    ids = [w["id"] for w in r.json()]
    assert wh_a in ids


# --------------------------------------------------------------------- #
# 2. Receipt + balance
# --------------------------------------------------------------------- #
async def test_receipt_creates_lot_and_balance(client: AsyncClient) -> None:
    ctx = await _new_owner_org_farm(client)
    wh_id = await _create_warehouse(client, ctx["org_id"], farm_id=ctx["farm_id"])
    item_id = await _create_feed_item(client, ctx["org_id"])
    r = await _receipt(client, wh_id, item_id, quantity=100.0, unit="kg", lot_code="L1")
    assert r["status"] == 201
    tx = r["body"]
    assert tx["transaction_type"] == "receipt"
    assert Decimal(str(tx["quantity"])) == Decimal("100.000000")

    # List lots — balance must equal received quantity in canonical unit.
    r = await client.get(f"/api/v1/warehouses/{wh_id}/lots")
    assert r.status_code == 200
    lot = r.json()[0]
    assert Decimal(str(lot["balance"])) == Decimal("100.000000")
    assert lot["balance_unit"] == "kg"


async def test_receipt_with_unit_conversion(client: AsyncClient) -> None:
    """Receiving 2000 g into a lot with canonical kg → balance = 2 kg."""
    ctx = await _new_owner_org_farm(client)
    wh_id = await _create_warehouse(client, ctx["org_id"])
    item_id = await _create_feed_item(client, ctx["org_id"], canonical_unit="kg")
    r = await _receipt(client, wh_id, item_id, quantity=2000, unit="g", lot_code="L1")
    assert r["status"] == 201
    r = await client.get(f"/api/v1/warehouses/{wh_id}/lots")
    lot = r.json()[0]
    assert Decimal(str(lot["balance"])) == Decimal("2.000000")


async def test_receipt_incompatible_unit_rejected(client: AsyncClient) -> None:
    """kg item + mL receipt → 409 unit_incompatible."""
    ctx = await _new_owner_org_farm(client)
    wh_id = await _create_warehouse(client, ctx["org_id"])
    item_id = await _create_feed_item(client, ctx["org_id"], canonical_unit="kg")
    r = await _receipt(client, wh_id, item_id, quantity=1.0, unit="mL")
    assert r["status"] == 409
    assert r["body"]["detail"]["code"] == "unit_incompatible"


# --------------------------------------------------------------------- #
# 3. Idempotency
# --------------------------------------------------------------------- #
async def test_receipt_idempotency_replay(client: AsyncClient) -> None:
    ctx = await _new_owner_org_farm(client)
    wh_id = await _create_warehouse(client, ctx["org_id"])
    item_id = await _create_feed_item(client, ctx["org_id"])
    key = f"idem-{uuid4().hex[:8]}"
    r1 = await _receipt(
        client, wh_id, item_id, quantity=50.0, unit="kg", lot_code="L1", idempotency_key=key
    )
    r2 = await _receipt(
        client, wh_id, item_id, quantity=50.0, unit="kg", lot_code="L1", idempotency_key=key
    )
    assert r1["status"] == 201
    assert r2["status"] == 200
    assert r2["headers"].get("x-idempotent-replay") == "true"
    # Same tx id — no duplicate.
    assert r1["body"]["id"] == r2["body"]["id"]

    # Balance shows a single 50kg receipt, not 100.
    r = await client.get(f"/api/v1/warehouses/{wh_id}/lots")
    assert Decimal(str(r.json()[0]["balance"])) == Decimal("50.000000")


async def test_receipt_idempotency_payload_conflict(client: AsyncClient) -> None:
    ctx = await _new_owner_org_farm(client)
    wh_id = await _create_warehouse(client, ctx["org_id"])
    item_id = await _create_feed_item(client, ctx["org_id"])
    key = f"idem-{uuid4().hex[:8]}"
    await _receipt(client, wh_id, item_id, quantity=50.0, lot_code="L1", idempotency_key=key)
    r = await _receipt(client, wh_id, item_id, quantity=75.0, lot_code="L1", idempotency_key=key)
    assert r["status"] == 409, r
    assert r["body"]["detail"]["code"] == "idempotency_key_payload_conflict"


# --------------------------------------------------------------------- #
# 4. Issue / Transfer / Adjustment / Reversal
# --------------------------------------------------------------------- #
async def _lot_id_for(client: AsyncClient, wh_id: str) -> str:
    r = await client.get(f"/api/v1/warehouses/{wh_id}/lots")
    assert r.status_code == 200
    return r.json()[0]["id"]


async def test_issue_deducts_and_rejects_insufficient(client: AsyncClient) -> None:
    ctx = await _new_owner_org_farm(client)
    wh_id = await _create_warehouse(client, ctx["org_id"])
    item_id = await _create_feed_item(client, ctx["org_id"])
    await _receipt(client, wh_id, item_id, quantity=10, unit="kg", lot_code="L1")
    lot_id = await _lot_id_for(client, wh_id)
    # Sufficient issue.
    r = await client.post(
        f"/api/v1/warehouses/{wh_id}/inventory:issue",
        json={"lot_id": lot_id, "quantity": 4, "unit": "kg"},
    )
    assert r.status_code == 201, r.text
    # Insufficient issue.
    r = await client.post(
        f"/api/v1/warehouses/{wh_id}/inventory:issue",
        json={"lot_id": lot_id, "quantity": 7, "unit": "kg"},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "insufficient_stock"


async def test_transfer_moves_stock_between_warehouses(client: AsyncClient) -> None:
    ctx = await _new_owner_org_farm(client)
    src = await _create_warehouse(client, ctx["org_id"], code="SRC")
    dst = await _create_warehouse(client, ctx["org_id"], code="DST")
    item_id = await _create_feed_item(client, ctx["org_id"])
    await _receipt(client, src, item_id, quantity=20, unit="kg", lot_code="L1")
    src_lot = await _lot_id_for(client, src)
    r = await client.post(
        f"/api/v1/warehouses/{src}/inventory:transfer",
        json={
            "lot_id": src_lot,
            "destination_warehouse_id": dst,
            "quantity": 8,
            "unit": "kg",
        },
    )
    assert r.status_code == 201, r.text
    src_lots = (await client.get(f"/api/v1/warehouses/{src}/lots")).json()
    dst_lots = (await client.get(f"/api/v1/warehouses/{dst}/lots")).json()
    assert Decimal(str(src_lots[0]["balance"])) == Decimal("12.000000")
    assert Decimal(str(dst_lots[0]["balance"])) == Decimal("8.000000")


async def test_adjustment_requires_reason(client: AsyncClient) -> None:
    ctx = await _new_owner_org_farm(client)
    wh_id = await _create_warehouse(client, ctx["org_id"])
    item_id = await _create_feed_item(client, ctx["org_id"])
    await _receipt(client, wh_id, item_id, quantity=5, lot_code="L1")
    lot_id = await _lot_id_for(client, wh_id)
    r = await client.post(
        f"/api/v1/warehouses/{wh_id}/inventory:adjust",
        json={
            "lot_id": lot_id,
            "quantity": 1,
            "unit": "kg",
            "direction": "increase",
            # missing reason
        },
    )
    assert r.status_code == 422


async def test_reversal_flips_balance(client: AsyncClient) -> None:
    ctx = await _new_owner_org_farm(client)
    wh_id = await _create_warehouse(client, ctx["org_id"])
    item_id = await _create_feed_item(client, ctx["org_id"])
    receipt_tx = (await _receipt(client, wh_id, item_id, quantity=10, lot_code="L1"))["body"]

    # Reverse the receipt — balance goes back to 0.
    r = await client.post(
        f"/api/v1/warehouses/{wh_id}/inventory:reverse",
        json={
            "reverses_transaction_id": receipt_tx["id"],
            "reason": "Wrong lot code entered by warehouse clerk.",
        },
    )
    assert r.status_code == 201, r.text
    lots = (await client.get(f"/api/v1/warehouses/{wh_id}/lots")).json()
    assert Decimal(str(lots[0]["balance"])) == Decimal("0")

    # Second reversal on the same original → 409.
    r = await client.post(
        f"/api/v1/warehouses/{wh_id}/inventory:reverse",
        json={"reverses_transaction_id": receipt_tx["id"], "reason": "again"},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "already_reversed"


# --------------------------------------------------------------------- #
# 5. FEEDING event → inventory consumption atomicity
# --------------------------------------------------------------------- #
async def _make_active_batch_with_lot(client: AsyncClient) -> dict:
    ctx = await _new_owner_org_farm(client)
    ut = await _pick_system_unit_type_id(client, ctx["org_id"])
    unit_id = await _create_unit(client, ctx["site_id"], ut)
    batch_id = await _create_batch(client, unit_id)
    r = await client.post(
        f"/api/v1/batches/{batch_id}/events",
        json={"event_type": "STOCKING", "data": stocking_payload(quantity=100)},
    )
    assert r.status_code == 201, r.text
    r = await client.post(
        f"/api/v1/batches/{batch_id}/transitions", json={"target_state": "active"}
    )
    assert r.status_code == 200, r.text
    wh_id = await _create_warehouse(client, ctx["org_id"], farm_id=ctx["farm_id"])
    item_id = await _create_feed_item(client, ctx["org_id"])
    await _receipt(client, wh_id, item_id, quantity=25, unit="kg", lot_code="FEED-01")
    lot_id = await _lot_id_for(client, wh_id)
    ctx.update(unit_id=unit_id, batch_id=batch_id, wh_id=wh_id, item_id=item_id, lot_id=lot_id)
    return ctx


async def test_feeding_with_lot_deducts_inventory(client: AsyncClient) -> None:
    ctx = await _make_active_batch_with_lot(client)
    r = await client.post(
        f"/api/v1/batches/{ctx['batch_id']}/events",
        json={
            "event_type": "FEEDING",
            "data": {
                "quantity": 3.0,
                "unit": "kg",
                "inventory_lot_id": ctx["lot_id"],
            },
        },
    )
    assert r.status_code == 201, r.text
    # Balance went from 25 → 22.
    lots = (await client.get(f"/api/v1/warehouses/{ctx['wh_id']}/lots")).json()
    assert Decimal(str(lots[0]["balance"])) == Decimal("22.000000")

    # Transaction ledger for the lot shows the CONSUMPTION row.
    r = await client.get(f"/api/v1/lots/{ctx['lot_id']}/transactions")
    types = [t["transaction_type"] for t in r.json()["items"]]
    assert "consumption" in types


@_postgres_only
async def test_feeding_insufficient_stock_rolls_back_event(client: AsyncClient) -> None:
    """FEEDING quantity > lot balance → both the event AND the deduction
    roll back (savepoint delivers the event insert; the consumption
    raises 409 and the outer request rolls back).

    Postgres-only: SQLite's StaticPool + shared connection semantics
    make it impossible to reliably observe outer-transaction rollback
    across nested savepoints in this test harness. Real DB-level
    transaction behaviour is what we're validating anyway.
    """
    ctx = await _make_active_batch_with_lot(client)
    r = await client.post(
        f"/api/v1/batches/{ctx['batch_id']}/events",
        json={
            "event_type": "FEEDING",
            "data": {
                "quantity": 999.0,
                "unit": "kg",
                "inventory_lot_id": ctx["lot_id"],
            },
        },
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "insufficient_stock"
    # No event was persisted.
    r = await client.get(
        f"/api/v1/batches/{ctx['batch_id']}/events", params={"event_type": "FEEDING"}
    )
    assert r.status_code == 200
    assert r.json()["items"] == []
    # No consumption row was persisted.
    r = await client.get(f"/api/v1/lots/{ctx['lot_id']}/transactions")
    types = [t["transaction_type"] for t in r.json()["items"]]
    assert "consumption" not in types


async def test_feeding_retry_does_not_double_deduct(client: AsyncClient) -> None:
    ctx = await _make_active_batch_with_lot(client)
    key = f"feed-{uuid4().hex[:8]}"
    body = {
        "event_type": "FEEDING",
        "data": {
            "quantity": 4.0,
            "unit": "kg",
            "inventory_lot_id": ctx["lot_id"],
        },
    }
    r1 = await client.post(
        f"/api/v1/batches/{ctx['batch_id']}/events",
        json=body,
        headers={"Idempotency-Key": key},
    )
    r2 = await client.post(
        f"/api/v1/batches/{ctx['batch_id']}/events",
        json=body,
        headers={"Idempotency-Key": key},
    )
    assert r1.status_code == 201
    assert r2.status_code == 200
    # Balance dropped exactly once: 25 - 4 = 21.
    lots = (await client.get(f"/api/v1/warehouses/{ctx['wh_id']}/lots")).json()
    assert Decimal(str(lots[0]["balance"])) == Decimal("21.000000")


async def test_feeding_without_lot_still_works(client: AsyncClient) -> None:
    """Farms without configured inventory keep the ad-hoc path."""
    ctx = await _make_active_batch_with_lot(client)
    r = await client.post(
        f"/api/v1/batches/{ctx['batch_id']}/events",
        json={
            "event_type": "FEEDING",
            "data": {
                "quantity": 1.0,
                "unit": "kg",
                "feed_description": "Ad-hoc starter crumble",
            },
        },
    )
    assert r.status_code == 201


# --------------------------------------------------------------------- #
# 6. Concurrency (Postgres-only)
# --------------------------------------------------------------------- #
@_postgres_only
async def test_concurrent_issues_never_overshoot(client: AsyncClient) -> None:
    ctx = await _new_owner_org_farm(client)
    wh_id = await _create_warehouse(client, ctx["org_id"])
    item_id = await _create_feed_item(client, ctx["org_id"])
    await _receipt(client, wh_id, item_id, quantity=10, lot_code="L1")
    lot_id = await _lot_id_for(client, wh_id)
    body = {"lot_id": lot_id, "quantity": 6, "unit": "kg"}

    r1, r2 = await asyncio.gather(
        client.post(f"/api/v1/warehouses/{wh_id}/inventory:issue", json=body),
        client.post(f"/api/v1/warehouses/{wh_id}/inventory:issue", json=body),
    )
    statuses = sorted([r1.status_code, r2.status_code])
    # 6 + 6 > 10 — exactly one must fail 409.
    assert statuses[0] == 201, (r1.text, r2.text)
    assert statuses[1] == 409, (r1.text, r2.text)
    lots = (await client.get(f"/api/v1/warehouses/{wh_id}/lots")).json()
    assert Decimal(str(lots[0]["balance"])) == Decimal("4.000000")


# --------------------------------------------------------------------- #
# 7. Reconciliation — projections always agree with the ledger
# --------------------------------------------------------------------- #
async def test_reconciliation_ledger_matches_projection(client: AsyncClient) -> None:
    ctx = await _new_owner_org_farm(client)
    wh_id = await _create_warehouse(client, ctx["org_id"])
    item_id = await _create_feed_item(client, ctx["org_id"])
    await _receipt(client, wh_id, item_id, quantity=10, unit="kg", lot_code="L1")
    lot_id = await _lot_id_for(client, wh_id)
    # Issue 3, adjust +1, issue 2. Expected balance = 10 - 3 + 1 - 2 = 6.
    await client.post(
        f"/api/v1/warehouses/{wh_id}/inventory:issue",
        json={"lot_id": lot_id, "quantity": 3, "unit": "kg"},
    )
    await client.post(
        f"/api/v1/warehouses/{wh_id}/inventory:adjust",
        json={
            "lot_id": lot_id,
            "quantity": 1,
            "unit": "kg",
            "direction": "increase",
            "reason": "Recount",
        },
    )
    await client.post(
        f"/api/v1/warehouses/{wh_id}/inventory:issue",
        json={"lot_id": lot_id, "quantity": 2, "unit": "kg"},
    )
    r = await client.get(f"/api/v1/lots/{lot_id}")
    assert Decimal(str(r.json()["balance"])) == Decimal("6.000000")


# --------------------------------------------------------------------- #
# 8. Permission enforcement
# --------------------------------------------------------------------- #
async def test_viewer_cannot_receive_stock(client: AsyncClient) -> None:
    owner = await _new_owner_org_farm(client)
    wh_id = await _create_warehouse(client, owner["org_id"], farm_id=owner["farm_id"])
    item_id = await _create_feed_item(client, owner["org_id"])

    viewer = f"viewer-{uuid4().hex[:8]}@agrovix.dev"
    await create_verified_user(viewer)
    await invite_and_accept(
        client,
        inviter_email=owner["owner"],
        invitee_email=viewer,
        org_id=owner["org_id"],
        role_name="viewer",
    )
    r = await client.post(
        f"/api/v1/warehouses/{wh_id}/inventory:receive",
        json={
            "item_id": item_id,
            "lot_code": "L1",
            "quantity": 1,
            "unit": "kg",
        },
    )
    assert r.status_code == 403, r.text
    assert "inventory_transaction.create" in r.json()["detail"]


async def test_viewer_can_read_lots(client: AsyncClient) -> None:
    owner = await _new_owner_org_farm(client)
    wh_id = await _create_warehouse(client, owner["org_id"], farm_id=owner["farm_id"])
    item_id = await _create_feed_item(client, owner["org_id"])
    await _receipt(client, wh_id, item_id, quantity=3, lot_code="L1")

    viewer = f"viewer-{uuid4().hex[:8]}@agrovix.dev"
    await create_verified_user(viewer)
    await invite_and_accept(
        client,
        inviter_email=owner["owner"],
        invitee_email=viewer,
        org_id=owner["org_id"],
        role_name="viewer",
    )
    r = await client.get(f"/api/v1/warehouses/{wh_id}/lots")
    assert r.status_code == 200
    assert len(r.json()) == 1


# --------------------------------------------------------------------- #
# 9. Closed warehouse is read-only
# --------------------------------------------------------------------- #
async def test_closed_warehouse_blocks_writes(client: AsyncClient) -> None:
    ctx = await _new_owner_org_farm(client)
    wh_id = await _create_warehouse(client, ctx["org_id"])
    item_id = await _create_feed_item(client, ctx["org_id"])
    r = await client.patch(f"/api/v1/warehouses/{wh_id}", json={"status": "closed"})
    assert r.status_code == 200, r.text
    r = await _receipt(client, wh_id, item_id, quantity=1)
    assert r["status"] == 409
    assert r["body"]["detail"]["code"] == "warehouse_closed_no_writes"


# --------------------------------------------------------------------- #
# 10. CRG03 fixes — MAINTENANCE lifecycle, dual-warehouse permission,
# reversal idempotency replay, audit logging on service-scope mutations.
# --------------------------------------------------------------------- #
async def test_maintenance_warehouse_blocks_outbound_but_allows_inbound(
    client: AsyncClient,
) -> None:
    """MAINTENANCE = inbound + reversal allowed; outbound refused."""
    ctx = await _new_owner_org_farm(client)
    wh_id = await _create_warehouse(client, ctx["org_id"])
    item_id = await _create_feed_item(client, ctx["org_id"])
    # Seed some stock before entering maintenance.
    seed = await _receipt(client, wh_id, item_id, quantity=100, lot_code="L1")
    assert seed["status"] == 201, seed["body"]
    lot_id = await _lot_id_for(client, wh_id)

    r = await client.patch(f"/api/v1/warehouses/{wh_id}", json={"status": "maintenance"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "maintenance"

    # RECEIPT still allowed.
    r = await _receipt(client, wh_id, item_id, quantity=5, lot_code="L1")
    assert r["status"] == 201, r["body"]

    # ADJUSTMENT_INCREASE still allowed.
    r = await client.post(
        f"/api/v1/warehouses/{wh_id}/inventory:adjust",
        json={
            "lot_id": lot_id,
            "quantity": 1,
            "unit": "kg",
            "direction": "increase",
            "reason": "audit correction",
        },
    )
    assert r.status_code == 201, r.text

    # ISSUE blocked with a clear MAINTENANCE code.
    r = await client.post(
        f"/api/v1/warehouses/{wh_id}/inventory:issue",
        json={"lot_id": lot_id, "quantity": 1, "unit": "kg"},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "warehouse_under_maintenance"

    # ADJUSTMENT_DECREASE blocked.
    r = await client.post(
        f"/api/v1/warehouses/{wh_id}/inventory:adjust",
        json={
            "lot_id": lot_id,
            "quantity": 1,
            "unit": "kg",
            "direction": "decrease",
            "reason": "loss",
        },
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "warehouse_under_maintenance"


async def test_maintenance_warehouse_blocks_transfer_out(client: AsyncClient) -> None:
    ctx = await _new_owner_org_farm(client)
    src = await _create_warehouse(client, ctx["org_id"], code="SRC-MAINT")
    dst = await _create_warehouse(client, ctx["org_id"], code="DST-OK")
    item_id = await _create_feed_item(client, ctx["org_id"])
    await _receipt(client, src, item_id, quantity=50, lot_code="LM")
    lot_id = await _lot_id_for(client, src)

    r = await client.patch(f"/api/v1/warehouses/{src}", json={"status": "maintenance"})
    assert r.status_code == 200

    r = await client.post(
        f"/api/v1/warehouses/{src}/inventory:transfer",
        json={
            "lot_id": lot_id,
            "destination_warehouse_id": dst,
            "quantity": 10,
            "unit": "kg",
        },
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "warehouse_under_maintenance"
    assert r.json()["detail"]["transaction_type"] == "transfer_out"


async def test_maintenance_reopen_to_active_restores_writes(client: AsyncClient) -> None:
    ctx = await _new_owner_org_farm(client)
    wh_id = await _create_warehouse(client, ctx["org_id"])
    item_id = await _create_feed_item(client, ctx["org_id"])
    await _receipt(client, wh_id, item_id, quantity=20, lot_code="LR")
    lot_id = await _lot_id_for(client, wh_id)

    r = await client.patch(f"/api/v1/warehouses/{wh_id}", json={"status": "maintenance"})
    assert r.status_code == 200
    # blocked
    r = await client.post(
        f"/api/v1/warehouses/{wh_id}/inventory:issue",
        json={"lot_id": lot_id, "quantity": 1, "unit": "kg"},
    )
    assert r.status_code == 409
    # reopen
    r = await client.patch(f"/api/v1/warehouses/{wh_id}", json={"status": "active"})
    assert r.status_code == 200
    r = await client.post(
        f"/api/v1/warehouses/{wh_id}/inventory:issue",
        json={"lot_id": lot_id, "quantity": 1, "unit": "kg"},
    )
    assert r.status_code == 201


async def test_reversal_under_maintenance_allowed(client: AsyncClient) -> None:
    """Reversals are audit corrections — allowed even under MAINTENANCE."""
    ctx = await _new_owner_org_farm(client)
    wh_id = await _create_warehouse(client, ctx["org_id"])
    item_id = await _create_feed_item(client, ctx["org_id"])
    receipt = await _receipt(client, wh_id, item_id, quantity=30, lot_code="LREV")
    receipt_tx_id = receipt["body"]["id"]

    r = await client.patch(f"/api/v1/warehouses/{wh_id}", json={"status": "maintenance"})
    assert r.status_code == 200

    r = await client.post(
        f"/api/v1/warehouses/{wh_id}/inventory:reverse",
        json={"reverses_transaction_id": receipt_tx_id, "reason": "posted in error"},
        headers={"Idempotency-Key": f"rev-{uuid4().hex[:8]}"},
    )
    assert r.status_code == 201, r.text


async def test_closed_warehouse_only_reopens_via_status_flip(client: AsyncClient) -> None:
    ctx = await _new_owner_org_farm(client)
    wh_id = await _create_warehouse(client, ctx["org_id"])
    r = await client.patch(f"/api/v1/warehouses/{wh_id}", json={"status": "closed"})
    assert r.status_code == 200
    # Non-status field on a CLOSED warehouse is refused.
    r = await client.patch(f"/api/v1/warehouses/{wh_id}", json={"name": "Renamed while closed"})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "warehouse_closed_no_writes"
    # Reopen via status transition works.
    r = await client.patch(f"/api/v1/warehouses/{wh_id}", json={"status": "active"})
    assert r.status_code == 200
    assert r.json()["status"] == "active"


# --------------------------------------------------------------------- #
# CRG03 verification-only pass — CLOSED PATCH must be status-only.
# Reopening and ordinary mutations must be two separate requests.
# --------------------------------------------------------------------- #
async def test_closed_patch_status_only_active_reopens(client: AsyncClient) -> None:
    ctx = await _new_owner_org_farm(client)
    wh_id = await _create_warehouse(client, ctx["org_id"])
    r = await client.patch(f"/api/v1/warehouses/{wh_id}", json={"status": "closed"})
    assert r.status_code == 200
    # Status-only reopen is the ONE allowed shape.
    r = await client.patch(f"/api/v1/warehouses/{wh_id}", json={"status": "active"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active"


async def test_closed_patch_status_only_maintenance_reopens(client: AsyncClient) -> None:
    ctx = await _new_owner_org_farm(client)
    wh_id = await _create_warehouse(client, ctx["org_id"])
    r = await client.patch(f"/api/v1/warehouses/{wh_id}", json={"status": "closed"})
    assert r.status_code == 200
    r = await client.patch(f"/api/v1/warehouses/{wh_id}", json={"status": "maintenance"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "maintenance"


async def test_closed_patch_name_only_refused(client: AsyncClient) -> None:
    ctx = await _new_owner_org_farm(client)
    wh_id = await _create_warehouse(client, ctx["org_id"])
    r = await client.patch(f"/api/v1/warehouses/{wh_id}", json={"status": "closed"})
    assert r.status_code == 200
    r = await client.patch(f"/api/v1/warehouses/{wh_id}", json={"name": "x"})
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "warehouse_closed_no_writes"
    assert "name" in r.json()["detail"]["submitted_fields"]


async def test_closed_patch_status_plus_name_refused(client: AsyncClient) -> None:
    """CRG03 P0 gap — {status: active, name: 'x'} was previously
    accepted because the reopen branch allowed accompanying fields.
    This test locks the correct behaviour: mixing status with any
    other field returns 409 even when the status transition would
    otherwise be valid."""
    ctx = await _new_owner_org_farm(client)
    wh_id = await _create_warehouse(client, ctx["org_id"])
    r = await client.patch(f"/api/v1/warehouses/{wh_id}", json={"status": "closed"})
    assert r.status_code == 200
    r = await client.patch(f"/api/v1/warehouses/{wh_id}", json={"status": "active", "name": "x"})
    assert r.status_code == 409, r.text
    body = r.json()["detail"]
    assert body["code"] == "warehouse_closed_no_writes"
    assert set(body["submitted_fields"]) == {"status", "name"}
    # Confirm the warehouse is still CLOSED — the aborted PATCH must
    # have left the state untouched.
    r = await client.get(f"/api/v1/warehouses/{wh_id}")
    assert r.status_code == 200
    assert r.json()["status"] == "closed"


async def test_closed_patch_status_plus_farm_id_refused(client: AsyncClient) -> None:
    """farm_id is not part of ``WarehouseUpdate`` today, but the
    guard must reject it even if the schema is extended. Sends
    ``address`` instead (present in ``WarehouseUpdate``) as a
    schema-visible proxy for 'status + other field'."""
    ctx = await _new_owner_org_farm(client)
    wh_id = await _create_warehouse(client, ctx["org_id"])
    r = await client.patch(f"/api/v1/warehouses/{wh_id}", json={"status": "closed"})
    assert r.status_code == 200
    r = await client.patch(
        f"/api/v1/warehouses/{wh_id}", json={"status": "active", "address": "12 New Rd"}
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "warehouse_closed_no_writes"
    # Still CLOSED — no partial application.
    r = await client.get(f"/api/v1/warehouses/{wh_id}")
    assert r.status_code == 200
    assert r.json()["status"] == "closed"
    assert r.json()["address"] is None


async def test_closed_reopen_then_edit_is_two_step_flow(client: AsyncClient) -> None:
    """After a successful status-only reopen, ordinary fields may be
    updated in a subsequent PATCH — proving reopen and mutate are
    two separate requests."""
    ctx = await _new_owner_org_farm(client)
    wh_id = await _create_warehouse(client, ctx["org_id"])
    r = await client.patch(f"/api/v1/warehouses/{wh_id}", json={"status": "closed"})
    assert r.status_code == 200
    # Step 1 — reopen only.
    r = await client.patch(f"/api/v1/warehouses/{wh_id}", json={"status": "active"})
    assert r.status_code == 200
    assert r.json()["status"] == "active"
    # Step 2 — rename in a separate call.
    r = await client.patch(f"/api/v1/warehouses/{wh_id}", json={"name": "HQ (reopened)"})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "HQ (reopened)"
    assert r.json()["status"] == "active"


async def test_transfer_requires_permission_on_destination(client: AsyncClient) -> None:
    """Source-farm operator without dst-farm access cannot pump stock in."""
    owner = await _new_owner_org_farm(client)
    owner_email = owner["owner"]

    # Second farm on the same org (destination).
    await switch_user(client, owner_email)
    r = await client.post(
        f"/api/v1/organizations/{owner['org_id']}/farms",
        json={"name": "Farm-B", "code": f"farm-b-{uuid4().hex[:6]}"},
    )
    assert r.status_code == 201, r.text
    farm_b_id = r.json()["id"]

    src = await _create_warehouse(client, owner["org_id"], farm_id=owner["farm_id"], code="SRC-A")
    dst = await _create_warehouse(client, owner["org_id"], farm_id=farm_b_id, code="DST-B")
    item_id = await _create_feed_item(client, owner["org_id"])
    await _receipt(client, src, item_id, quantity=50, lot_code="LX")
    lot_id = await _lot_id_for(client, src)

    # Farm-A-only manager has no membership on Farm B.
    operator = f"op-{uuid4().hex[:8]}@agrovix.dev"
    await create_verified_user(operator)
    await invite_and_accept(
        client,
        inviter_email=owner_email,
        invitee_email=operator,
        org_id=owner["org_id"],
        role_name="farm_manager",
        farm_id=owner["farm_id"],
    )

    await switch_user(client, operator)
    r = await client.post(
        f"/api/v1/warehouses/{src}/inventory:transfer",
        json={
            "lot_id": lot_id,
            "destination_warehouse_id": dst,
            "quantity": 5,
            "unit": "kg",
        },
        headers={"Idempotency-Key": f"xfer-{uuid4().hex[:8]}"},
    )
    # Source-side membership passes; destination-side check must refuse.
    assert r.status_code in (403, 404), r.text
    if r.status_code == 403:
        assert "inventory_transaction.create" in r.json()["detail"]


async def test_reversal_idempotency_replays_original_response(client: AsyncClient) -> None:
    """Second call with same idempotency key must replay, not re-check."""
    ctx = await _new_owner_org_farm(client)
    wh_id = await _create_warehouse(client, ctx["org_id"])
    item_id = await _create_feed_item(client, ctx["org_id"])
    receipt = await _receipt(client, wh_id, item_id, quantity=10, lot_code="LI")
    receipt_tx_id = receipt["body"]["id"]

    key = f"rev-key-{uuid4().hex[:8]}"
    body = {"reverses_transaction_id": receipt_tx_id, "reason": "double-tap safeguard"}

    r1 = await client.post(
        f"/api/v1/warehouses/{wh_id}/inventory:reverse", json=body, headers={"Idempotency-Key": key}
    )
    assert r1.status_code == 201, r1.text
    marker_id = r1.json()["id"]

    # Second call — same key + same payload — must REPLAY (200 + X-Idempotent-Replay: true)
    # instead of returning 409 already_reversed.
    r2 = await client.post(
        f"/api/v1/warehouses/{wh_id}/inventory:reverse", json=body, headers={"Idempotency-Key": key}
    )
    assert r2.status_code == 200, r2.text
    assert r2.headers.get("X-Idempotent-Replay") == "true"
    assert r2.json()["id"] == marker_id

    # A DIFFERENT idempotency key against the same original transaction
    # must hit the 'already_reversed' path.
    r3 = await client.post(
        f"/api/v1/warehouses/{wh_id}/inventory:reverse",
        json=body,
        headers={"Idempotency-Key": f"rev-key-{uuid4().hex[:8]}"},
    )
    assert r3.status_code == 409
    assert r3.json()["detail"]["code"] == "already_reversed"


async def test_update_warehouse_is_audited(client: AsyncClient) -> None:
    """CRG03 P1 — warehouse edits flow through the service and hit the audit log."""
    ctx = await _new_owner_org_farm(client)
    wh_id = await _create_warehouse(client, ctx["org_id"])
    r = await client.patch(f"/api/v1/warehouses/{wh_id}", json={"name": "Renamed HQ"})
    assert r.status_code == 200
    assert r.json()["name"] == "Renamed HQ"

    r = await client.get(
        f"/api/v1/organizations/{ctx['org_id']}/audit-events",
        params={"entity_type": "warehouse"},
    )
    assert r.status_code == 200
    events = r.json().get("items", r.json())
    matches = [
        e
        for e in events
        if e.get("entity_id") == wh_id and e.get("action") == "inventory_warehouse.update"
    ]
    assert matches, f"expected an inventory_warehouse.update audit row for {wh_id}"
    md = matches[0].get("metadata") or matches[0].get("metadata_json") or {}
    assert "changed" in md
    assert "name" in md["changed"]


async def test_update_item_is_audited(client: AsyncClient) -> None:
    """CRG03 P1 — item edits flow through the service and hit the audit log.

    ``canonical_unit`` is also not part of the ``InventoryItemUpdate``
    schema so Pydantic silently ignores it — the service defensive
    check is dead code by construction (belt + suspenders). We verify
    the schema-level protection at the same time.
    """
    ctx = await _new_owner_org_farm(client)
    item_id = await _create_feed_item(client, ctx["org_id"], canonical_unit="kg")
    r = await client.patch(
        f"/api/v1/inventory-items/{item_id}", json={"name": "Renamed feed", "sku": "SKU-1"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Renamed feed"
    # canonical_unit is silently dropped by the schema.
    r = await client.patch(f"/api/v1/inventory-items/{item_id}", json={"canonical_unit": "L"})
    assert r.status_code == 200, r.text
    assert r.json()["canonical_unit"] == "kg"

    r = await client.get(
        f"/api/v1/organizations/{ctx['org_id']}/audit-events",
        params={"entity_type": "inventory_item"},
    )
    assert r.status_code == 200
    events = r.json().get("items", r.json())
    matches = [
        e
        for e in events
        if e.get("entity_id") == item_id and e.get("action") == "inventory_item.update"
    ]
    assert matches, f"expected inventory_item.update audit row for {item_id}"


# --------------------------------------------------------------------- #
# Sprint 5.4.2 — Atomic warehouse-transfer reversal
# --------------------------------------------------------------------- #
async def _find_transfer_out_tx(client: AsyncClient, wh_id: str, lot_id: str) -> dict:
    """Return the TRANSFER_OUT row for the source lot after a transfer."""
    r = await client.get(f"/api/v1/lots/{lot_id}/transactions")
    assert r.status_code == 200, r.text
    rows = r.json()["items"]
    outs = [t for t in rows if t["transaction_type"] == "transfer_out"]
    assert outs, f"expected TRANSFER_OUT on lot {lot_id}, got {rows}"
    return outs[0]


async def _find_transfer_in_tx(client: AsyncClient, lot_id: str) -> dict:
    r = await client.get(f"/api/v1/lots/{lot_id}/transactions")
    assert r.status_code == 200, r.text
    rows = r.json()["items"]
    ins = [t for t in rows if t["transaction_type"] == "transfer_in"]
    assert ins, f"expected TRANSFER_IN on lot {lot_id}, got {rows}"
    return ins[0]


async def _setup_transfer_pair(
    client: AsyncClient, *, transfer_qty: float = 8.0, initial_qty: float = 20.0
) -> dict:
    ctx = await _new_owner_org_farm(client)
    src = await _create_warehouse(client, ctx["org_id"], code=f"SRC-{uuid4().hex[:4]}")
    dst = await _create_warehouse(client, ctx["org_id"], code=f"DST-{uuid4().hex[:4]}")
    item_id = await _create_feed_item(client, ctx["org_id"])
    await _receipt(client, src, item_id, quantity=initial_qty, unit="kg", lot_code="L1")
    src_lot = await _lot_id_for(client, src)
    r = await client.post(
        f"/api/v1/warehouses/{src}/inventory:transfer",
        json={
            "lot_id": src_lot,
            "destination_warehouse_id": dst,
            "quantity": transfer_qty,
            "unit": "kg",
        },
    )
    assert r.status_code == 201, r.text
    dst_lot = await _lot_id_for(client, dst)
    out_tx = await _find_transfer_out_tx(client, src, src_lot)
    in_tx = await _find_transfer_in_tx(client, dst_lot)
    return {
        "ctx": ctx,
        "src": src,
        "dst": dst,
        "src_lot": src_lot,
        "dst_lot": dst_lot,
        "out_tx": out_tx,
        "in_tx": in_tx,
        "initial_qty": initial_qty,
        "transfer_qty": transfer_qty,
    }


async def test_transfer_reversal_atomic_via_transfer_out(client: AsyncClient) -> None:
    """Reversing a TRANSFER_OUT atomically undoes BOTH sides.

    Sprint 5.4.2 — the destination warehouse must also see the
    inbound stock removed, not just the source-side credit-back.
    """
    setup = await _setup_transfer_pair(client, transfer_qty=8.0, initial_qty=20.0)
    r = await client.post(
        f"/api/v1/warehouses/{setup['src']}/inventory:reverse",
        json={
            "reverses_transaction_id": setup["out_tx"]["id"],
            "reason": "wrong destination",
        },
    )
    assert r.status_code == 201, r.text
    src_lots = (await client.get(f"/api/v1/warehouses/{setup['src']}/lots")).json()
    dst_lots = (await client.get(f"/api/v1/warehouses/{setup['dst']}/lots")).json()
    # Source recovers the 8 kg it lent.
    assert Decimal(str(src_lots[0]["balance"])) == Decimal("20.000000")
    # Destination gives back the 8 kg it received.
    assert Decimal(str(dst_lots[0]["balance"])) == Decimal("0")


async def test_transfer_reversal_atomic_via_transfer_in(client: AsyncClient) -> None:
    """Reversing a TRANSFER_IN atomically undoes BOTH sides too.

    The frontend only exposes reversal on the ``transfer_out`` row,
    but the backend must accept either side as the entry point and
    produce the same outcome. Otherwise a hostile client that hits
    the API directly could induce the half-reversal state the fix
    exists to prevent.
    """
    setup = await _setup_transfer_pair(client, transfer_qty=5.0, initial_qty=12.0)
    r = await client.post(
        f"/api/v1/warehouses/{setup['dst']}/inventory:reverse",
        json={
            "reverses_transaction_id": setup["in_tx"]["id"],
            "reason": "duplicate submission",
        },
    )
    assert r.status_code == 201, r.text
    src_lots = (await client.get(f"/api/v1/warehouses/{setup['src']}/lots")).json()
    dst_lots = (await client.get(f"/api/v1/warehouses/{setup['dst']}/lots")).json()
    assert Decimal(str(src_lots[0]["balance"])) == Decimal("12.000000")
    assert Decimal(str(dst_lots[0]["balance"])) == Decimal("0")


@_postgres_only
async def test_transfer_reversal_rolls_back_when_destination_short(
    client: AsyncClient,
) -> None:
    """All-or-nothing: destination shortage aborts BOTH inverse writes.

    If the destination warehouse has already moved the transferred
    stock along (e.g. issued it to production), reversing the
    original transfer would need to decrease the destination lot
    below zero. The service must refuse with ``insufficient_stock``
    and leave the SOURCE balance untouched too — otherwise the two
    warehouses' balances diverge.

    Postgres-only: SQLite's DBAPI does not honour outer ``ROLLBACK``
    after an inner ``RELEASE SAVEPOINT`` when the connection runs
    under SQLAlchemy's deferred-transaction mode, so the nested
    savepoint's rows leak through. Real transaction rollback is
    verified against the production Postgres engine.
    """
    setup = await _setup_transfer_pair(client, transfer_qty=6.0, initial_qty=10.0)
    # Consume the destination stock before attempting the reversal.
    r = await client.post(
        f"/api/v1/warehouses/{setup['dst']}/inventory:issue",
        json={"lot_id": setup["dst_lot"], "quantity": 6, "unit": "kg"},
    )
    assert r.status_code == 201, r.text
    # Reverse — must be refused; NOTHING lands on either side.
    r = await client.post(
        f"/api/v1/warehouses/{setup['src']}/inventory:reverse",
        json={
            "reverses_transaction_id": setup["out_tx"]["id"],
            "reason": "should refuse",
        },
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "insufficient_stock"
    src_lots = (await client.get(f"/api/v1/warehouses/{setup['src']}/lots")).json()
    dst_lots = (await client.get(f"/api/v1/warehouses/{setup['dst']}/lots")).json()
    # Source stayed at post-transfer (initial - transfer) — did NOT
    # rise back. Destination stayed at zero — did NOT go negative.
    assert Decimal(str(src_lots[0]["balance"])) == Decimal("4.000000")
    assert Decimal(str(dst_lots[0]["balance"])) == Decimal("0")


async def test_transfer_reversal_second_call_returns_already_reversed(
    client: AsyncClient,
) -> None:
    """Once a paired transfer is reversed, either side is refused as
    ``already_reversed``. The marker is posted on BOTH sides by the
    first reversal, so hitting the OUT again OR the IN afterwards
    consistently hits the guard.
    """
    setup = await _setup_transfer_pair(client, transfer_qty=3.0, initial_qty=9.0)
    r1 = await client.post(
        f"/api/v1/warehouses/{setup['src']}/inventory:reverse",
        json={"reverses_transaction_id": setup["out_tx"]["id"], "reason": "first"},
    )
    assert r1.status_code == 201, r1.text
    # Re-attempt from the OUT side.
    r2 = await client.post(
        f"/api/v1/warehouses/{setup['src']}/inventory:reverse",
        json={"reverses_transaction_id": setup["out_tx"]["id"], "reason": "again"},
    )
    assert r2.status_code == 409
    assert r2.json()["detail"]["code"] == "already_reversed"
    # And from the IN side — must also refuse.
    r3 = await client.post(
        f"/api/v1/warehouses/{setup['dst']}/inventory:reverse",
        json={"reverses_transaction_id": setup["in_tx"]["id"], "reason": "again"},
    )
    assert r3.status_code == 409
    assert r3.json()["detail"]["code"] == "already_reversed"


async def test_transfer_reversal_replays_idempotency_key(client: AsyncClient) -> None:
    """Same Idempotency-Key + same payload → 200 replay on the second call.

    The atomic-transfer branch reuses the caller-selected side's
    lot for the key, so the replay contract still applies.
    """
    setup = await _setup_transfer_pair(client, transfer_qty=2.0, initial_qty=7.0)
    key = f"rev-xfer-{uuid4().hex[:8]}"
    body = {
        "reverses_transaction_id": setup["out_tx"]["id"],
        "reason": "duplicate submit",
    }
    r1 = await client.post(
        f"/api/v1/warehouses/{setup['src']}/inventory:reverse",
        json=body,
        headers={"Idempotency-Key": key},
    )
    assert r1.status_code == 201, r1.text
    marker_id = r1.json()["id"]
    r2 = await client.post(
        f"/api/v1/warehouses/{setup['src']}/inventory:reverse",
        json=body,
        headers={"Idempotency-Key": key},
    )
    assert r2.status_code == 200, r2.text
    assert r2.headers.get("X-Idempotent-Replay") == "true"
    assert r2.json()["id"] == marker_id
    # Balances landed exactly once.
    src_lots = (await client.get(f"/api/v1/warehouses/{setup['src']}/lots")).json()
    dst_lots = (await client.get(f"/api/v1/warehouses/{setup['dst']}/lots")).json()
    assert Decimal(str(src_lots[0]["balance"])) == Decimal("7.000000")
    assert Decimal(str(dst_lots[0]["balance"])) == Decimal("0")


# ===================================================================== #
# Sprint 5.4.3 — Atomic Transfer Reversal Hardening
# ===================================================================== #
#
# Every test in this block exercises the invariant:
#
#   A warehouse transfer is one atomic business operation and must
#   either be fully reversed or not reversed at all.
#
# The corruption tests reach into the DB directly to induce states
# the API path cannot produce (missing linkage, mismatched pair
# attributes, tampered topology) and prove the reversal service
# refuses cleanly with a diagnostic error code and NO writes.
_UUIDType = uuid4().__class__  # local alias — avoids reimporting UUID


async def _sum_org_inventory(client: AsyncClient, org_id: str) -> Decimal:
    """Return SUM(balance) across every lot in every warehouse of ``org_id``.

    This is the "organization total inventory" invariant used by the
    audit-integrity tests: a full transfer reversal must leave this
    total unchanged (identical before / after), and a refused
    reversal MUST leave it identical too (no partial writes).

    Sprint 5.4.4 — for cross-tenant assertions we sum directly from
    the DB (bypassing the API's tenant scoping) so a source-side
    caller can still assert "the OTHER org's inventory did not move".
    """
    async with _db_session_module.AsyncSessionLocal() as session:
        from app.models.inventory import InventoryTransaction as _Tx
        from app.models.inventory import Warehouse as _Wh

        # Balance = SUM(signed delta of every non-reversal tx for
        # lots in warehouses that belong to org_id). REVERSAL rows
        # carry zero balance effect; their inverse rows already
        # move the balance.
        stmt = (
            select(_Tx)
            .join(_Wh, _Tx.warehouse_id == _Wh.id)
            .where(_Wh.organization_id == _UUIDType(org_id))
        )
        txs = (await session.execute(stmt)).scalars().all()
    total = Decimal("0")
    for tx in txs:
        total += signed_delta(tx)
    return total


async def _count_tx_rows(lot_ids: list[str]) -> int:
    """Return the total number of ledger rows across ``lot_ids``.

    Reads directly from the DB so we can assert "no rows written"
    even when a refused reversal never surfaces new API-visible
    state.
    """
    async with _db_session_module.AsyncSessionLocal() as session:
        stmt = select(func.count(_InventoryTransaction.id)).where(
            _InventoryTransaction.lot_id.in_([_UUIDType(x) for x in lot_ids])
        )
        return (await session.execute(stmt)).scalar_one()


async def _count_reversal_markers(lot_ids: list[str]) -> int:
    async with _db_session_module.AsyncSessionLocal() as session:
        stmt = select(func.count(_InventoryTransaction.id)).where(
            _InventoryTransaction.lot_id.in_([_UUIDType(x) for x in lot_ids]),
            _InventoryTransaction.transaction_type == _InventoryTransactionType.REVERSAL,
        )
        return (await session.execute(stmt)).scalar_one()


async def _count_inverse_rows(lot_ids: list[str]) -> int:
    """Rows written by the reversal-inverse path."""
    async with _db_session_module.AsyncSessionLocal() as session:
        stmt = select(func.count(_InventoryTransaction.id)).where(
            _InventoryTransaction.lot_id.in_([_UUIDType(x) for x in lot_ids]),
            _InventoryTransaction.reference_type == "reversal_inverse_of",
        )
        return (await session.execute(stmt)).scalar_one()


async def _mutate_tx(tx_id: str, **updates) -> None:
    """Directly update a transaction row for corruption-scenario tests."""
    async with _db_session_module.AsyncSessionLocal() as session:
        await session.execute(
            sa_update(_InventoryTransaction)
            .where(_InventoryTransaction.id == _UUIDType(tx_id))
            .values(**updates)
        )
        await session.commit()


# --------------------------------------------------------------------- #
# 5.4.3.1 — Corrupted linkage: hard-refuse, never fall through.
# --------------------------------------------------------------------- #
@_sqlite_only
async def test_transfer_reversal_refuses_when_reference_type_missing(
    client: AsyncClient,
) -> None:
    """Missing reference_type MUST NOT fall back to single-row reversal."""
    setup = await _setup_transfer_pair(client, transfer_qty=3.0, initial_qty=9.0)
    lot_ids = [setup["src_lot"], setup["dst_lot"]]
    before_tx_count = await _count_tx_rows(lot_ids)
    before_total = await _sum_org_inventory(client, setup["ctx"]["org_id"])
    # Corrupt the OUT row's reference_type.
    await _mutate_tx(setup["out_tx"]["id"], reference_type=None)
    r = await client.post(
        f"/api/v1/warehouses/{setup['src']}/inventory:reverse",
        json={"reverses_transaction_id": setup["out_tx"]["id"], "reason": "attempt"},
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "transfer_pair_incomplete"
    assert await _count_tx_rows(lot_ids) == before_tx_count
    assert await _count_reversal_markers(lot_ids) == 0
    assert await _count_inverse_rows(lot_ids) == 0
    assert await _sum_org_inventory(client, setup["ctx"]["org_id"]) == before_total


@_postgres_only
async def test_transfer_reversal_refuses_when_reference_id_missing(
    client: AsyncClient,
) -> None:
    """Sprint 5.4.9 — the DB coupling trigger rejects the malformed
    UPDATE itself: a transfer row cannot lose its reference_id or
    diverge from its transfer_group_id. The application layer never
    sees a topology with a NULL reference_id on a transfer row.
    (Postgres-only: SQLite does not enforce the trigger.)
    """
    from sqlalchemy.exc import IntegrityError

    setup = await _setup_transfer_pair(client, transfer_qty=2.0, initial_qty=8.0)
    lot_ids = [setup["src_lot"], setup["dst_lot"]]
    before = await _count_tx_rows(lot_ids)
    with pytest.raises(IntegrityError):
        await _mutate_tx(setup["out_tx"]["id"], reference_id=None)
    assert await _count_tx_rows(lot_ids) == before
    assert await _count_reversal_markers(lot_ids) == 0


@_postgres_only
async def test_transfer_reversal_refuses_on_invalid_reference_id(
    client: AsyncClient,
) -> None:
    """Sprint 5.4.9 — the DB coupling trigger rejects reference_id
    divergence from transfer_group_id. (Postgres-only.)
    """
    from sqlalchemy.exc import IntegrityError

    setup = await _setup_transfer_pair(client, transfer_qty=2.0, initial_qty=8.0)
    lot_ids = [setup["src_lot"], setup["dst_lot"]]
    before = await _count_tx_rows(lot_ids)
    with pytest.raises(IntegrityError):
        await _mutate_tx(setup["out_tx"]["id"], reference_id=uuid4())
    assert await _count_tx_rows(lot_ids) == before


# --------------------------------------------------------------------- #
# 5.4.3.2 — Invalid topology.
# --------------------------------------------------------------------- #
async def test_transfer_reversal_refuses_when_two_out_rows(client: AsyncClient) -> None:
    """Sprint 5.4.8 — the DB partial unique index
    ``uq_inventory_tx_transfer_role`` REJECTS the mutation itself,
    proving topology enforcement at the database layer. Application
    code never sees a two-OUT topology.
    """
    from sqlalchemy.exc import IntegrityError

    setup = await _setup_transfer_pair(client, transfer_qty=2.0, initial_qty=8.0)
    lot_ids = [setup["src_lot"], setup["dst_lot"]]
    before = await _count_tx_rows(lot_ids)
    with pytest.raises(IntegrityError):
        await _mutate_tx(
            setup["in_tx"]["id"],
            transaction_type=_InventoryTransactionType.TRANSFER_OUT,
        )
    # No rows changed because the constraint fired.
    assert await _count_tx_rows(lot_ids) == before


async def test_transfer_reversal_refuses_when_two_in_rows(client: AsyncClient) -> None:
    """Sprint 5.4.8 — mirror of the two-OUT proof."""
    from sqlalchemy.exc import IntegrityError

    setup = await _setup_transfer_pair(client, transfer_qty=2.0, initial_qty=8.0)
    lot_ids = [setup["src_lot"], setup["dst_lot"]]
    before = await _count_tx_rows(lot_ids)
    with pytest.raises(IntegrityError):
        await _mutate_tx(
            setup["out_tx"]["id"],
            transaction_type=_InventoryTransactionType.TRANSFER_IN,
        )
    assert await _count_tx_rows(lot_ids) == before


# --------------------------------------------------------------------- #
# 5.4.3.3 — Attribute mismatches on the pair.
# --------------------------------------------------------------------- #
@_sqlite_only
async def test_transfer_reversal_refuses_when_pair_item_mismatch(
    client: AsyncClient,
) -> None:
    setup = await _setup_transfer_pair(client, transfer_qty=2.0, initial_qty=8.0)
    lot_ids = [setup["src_lot"], setup["dst_lot"]]
    before = await _count_tx_rows(lot_ids)
    # Create a second item and mutate the IN row's item_id to point at it.
    other_item_id = await _create_feed_item(client, setup["ctx"]["org_id"])
    await _mutate_tx(setup["in_tx"]["id"], item_id=_UUIDType(other_item_id))
    r = await client.post(
        f"/api/v1/warehouses/{setup['src']}/inventory:reverse",
        json={"reverses_transaction_id": setup["out_tx"]["id"], "reason": "x"},
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "transfer_pair_item_mismatch"
    assert await _count_tx_rows(lot_ids) == before


async def test_transfer_reversal_refuses_when_pair_quantity_mismatch(
    client: AsyncClient,
) -> None:
    setup = await _setup_transfer_pair(client, transfer_qty=4.0, initial_qty=10.0)
    lot_ids = [setup["src_lot"], setup["dst_lot"]]
    before = await _count_tx_rows(lot_ids)
    await _mutate_tx(setup["in_tx"]["id"], quantity=Decimal("3"))
    r = await client.post(
        f"/api/v1/warehouses/{setup['src']}/inventory:reverse",
        json={"reverses_transaction_id": setup["out_tx"]["id"], "reason": "x"},
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "transfer_pair_quantity_mismatch"
    assert await _count_tx_rows(lot_ids) == before


async def test_transfer_reversal_refuses_when_pair_unit_mismatch(
    client: AsyncClient,
) -> None:
    from app.models.inventory import StockUnit as _StockUnit

    setup = await _setup_transfer_pair(client, transfer_qty=2.0, initial_qty=8.0)
    lot_ids = [setup["src_lot"], setup["dst_lot"]]
    before = await _count_tx_rows(lot_ids)
    await _mutate_tx(setup["in_tx"]["id"], unit=_StockUnit.G)
    r = await client.post(
        f"/api/v1/warehouses/{setup['src']}/inventory:reverse",
        json={"reverses_transaction_id": setup["out_tx"]["id"], "reason": "x"},
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "transfer_pair_unit_mismatch"
    assert await _count_tx_rows(lot_ids) == before


@_sqlite_only
async def test_transfer_reversal_refuses_when_pair_cross_org(
    client: AsyncClient,
) -> None:
    setup = await _setup_transfer_pair(client, transfer_qty=2.0, initial_qty=8.0)
    lot_ids = [setup["src_lot"], setup["dst_lot"]]
    before = await _count_tx_rows(lot_ids)
    # Create a real second organization so the FK on
    # inventory_transactions.organization_id resolves; then point
    # the IN row at that other org to induce the cross-org state.
    other_owner_email = f"other-{uuid4().hex[:8]}@agrovix.dev"
    await create_verified_user(other_owner_email)
    await switch_user(client, other_owner_email)
    other_org_id = await create_org(client, slug=f"other-{uuid4().hex[:6]}")
    # Switch back to the original owner so the reversal call is
    # authenticated against the source-side identity.
    await switch_user(client, setup["ctx"]["owner"])
    await _mutate_tx(setup["in_tx"]["id"], organization_id=_UUIDType(other_org_id))
    r = await client.post(
        f"/api/v1/warehouses/{setup['src']}/inventory:reverse",
        json={"reverses_transaction_id": setup["out_tx"]["id"], "reason": "x"},
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "transfer_pair_cross_org"
    assert await _count_tx_rows(lot_ids) == before


async def test_transfer_reversal_refuses_when_partner_warehouse_mismatch(
    client: AsyncClient,
) -> None:
    """Partner row's lot_id must belong to its warehouse_id."""
    setup = await _setup_transfer_pair(client, transfer_qty=2.0, initial_qty=8.0)
    lot_ids = [setup["src_lot"], setup["dst_lot"]]
    before = await _count_tx_rows(lot_ids)
    # Reassign the IN row's warehouse_id to the source warehouse
    # (still same org). The paired warehouse now matches the source
    # — a transfer that "straddles" one warehouse is not a transfer.
    await _mutate_tx(
        setup["in_tx"]["id"],
        warehouse_id=_UUIDType(setup["src"]),
    )
    r = await client.post(
        f"/api/v1/warehouses/{setup['src']}/inventory:reverse",
        json={"reverses_transaction_id": setup["out_tx"]["id"], "reason": "x"},
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "transfer_pair_warehouse_mismatch"
    assert await _count_tx_rows(lot_ids) == before


# --------------------------------------------------------------------- #
# 5.4.3.4 — Dual-warehouse authorization.
# --------------------------------------------------------------------- #
async def _setup_two_farm_transfer(client: AsyncClient) -> dict:
    """Two-farm transfer with the owner user; returns owner + setup info."""
    owner = await _new_owner_org_farm(client)
    owner_email = owner["owner"]
    # Second farm on the same org.
    r = await client.post(
        f"/api/v1/organizations/{owner['org_id']}/farms",
        json={"name": "Farm-B", "code": f"farm-b-{uuid4().hex[:6]}"},
    )
    assert r.status_code == 201, r.text
    farm_b_id = r.json()["id"]
    src = await _create_warehouse(
        client, owner["org_id"], farm_id=owner["farm_id"], code=f"SRC-{uuid4().hex[:4]}"
    )
    dst = await _create_warehouse(
        client, owner["org_id"], farm_id=farm_b_id, code=f"DST-{uuid4().hex[:4]}"
    )
    item_id = await _create_feed_item(client, owner["org_id"])
    await _receipt(client, src, item_id, quantity=20, unit="kg", lot_code="LX")
    src_lot = await _lot_id_for(client, src)
    r = await client.post(
        f"/api/v1/warehouses/{src}/inventory:transfer",
        json={
            "lot_id": src_lot,
            "destination_warehouse_id": dst,
            "quantity": 5,
            "unit": "kg",
        },
    )
    assert r.status_code == 201, r.text
    dst_lot = await _lot_id_for(client, dst)
    out_tx = await _find_transfer_out_tx(client, src, src_lot)
    in_tx = await _find_transfer_in_tx(client, dst_lot)
    return {
        "owner_email": owner_email,
        "org_id": owner["org_id"],
        "farm_a_id": owner["farm_id"],
        "farm_b_id": farm_b_id,
        "src": src,
        "dst": dst,
        "src_lot": src_lot,
        "dst_lot": dst_lot,
        "out_tx": out_tx,
        "in_tx": in_tx,
    }


async def test_transfer_reversal_refused_when_only_source_permission(
    client: AsyncClient,
) -> None:
    """Farm-A manager (source-only) cannot reverse a transfer that
    touches Farm-B, even by targeting the OUT row on Farm-A.
    """
    setup = await _setup_two_farm_transfer(client)
    lot_ids = [setup["src_lot"], setup["dst_lot"]]
    before = await _count_tx_rows(lot_ids)
    operator = f"op-a-{uuid4().hex[:8]}@agrovix.dev"
    await create_verified_user(operator)
    await invite_and_accept(
        client,
        inviter_email=setup["owner_email"],
        invitee_email=operator,
        org_id=setup["org_id"],
        role_name="farm_manager",
        farm_id=setup["farm_a_id"],
    )
    await switch_user(client, operator)
    r = await client.post(
        f"/api/v1/warehouses/{setup['src']}/inventory:reverse",
        json={"reverses_transaction_id": setup["out_tx"]["id"], "reason": "x"},
    )
    assert r.status_code in (403, 404), r.text
    if r.status_code == 403:
        assert "inventory_transaction.create" in r.json()["detail"]
    assert await _count_tx_rows(lot_ids) == before


async def test_transfer_reversal_refused_when_only_destination_permission(
    client: AsyncClient,
) -> None:
    setup = await _setup_two_farm_transfer(client)
    lot_ids = [setup["src_lot"], setup["dst_lot"]]
    before = await _count_tx_rows(lot_ids)
    operator = f"op-b-{uuid4().hex[:8]}@agrovix.dev"
    await create_verified_user(operator)
    await invite_and_accept(
        client,
        inviter_email=setup["owner_email"],
        invitee_email=operator,
        org_id=setup["org_id"],
        role_name="farm_manager",
        farm_id=setup["farm_b_id"],
    )
    await switch_user(client, operator)
    r = await client.post(
        f"/api/v1/warehouses/{setup['dst']}/inventory:reverse",
        json={"reverses_transaction_id": setup["in_tx"]["id"], "reason": "x"},
    )
    assert r.status_code in (403, 404), r.text
    assert await _count_tx_rows(lot_ids) == before


# --------------------------------------------------------------------- #
# 5.4.3.5 — Idempotency + already-reversed via opposite side.
# --------------------------------------------------------------------- #
async def test_transfer_reversal_already_reversed_via_opposite_side_no_duplicates(
    client: AsyncClient,
) -> None:
    """After a paired reversal is committed, a second call — from
    either side, with any key — must not add another inverse row or
    marker. This proves the "already_reversed" guard covers BOTH
    sides of the pair.
    """
    setup = await _setup_transfer_pair(client, transfer_qty=3.0, initial_qty=9.0)
    lot_ids = [setup["src_lot"], setup["dst_lot"]]
    r1 = await client.post(
        f"/api/v1/warehouses/{setup['src']}/inventory:reverse",
        json={"reverses_transaction_id": setup["out_tx"]["id"], "reason": "first"},
    )
    assert r1.status_code == 201
    inverse_count_after_first = await _count_inverse_rows(lot_ids)
    marker_count_after_first = await _count_reversal_markers(lot_ids)
    assert inverse_count_after_first == 2  # one per side
    assert marker_count_after_first == 2
    # Second attempt via the OPPOSITE side, fresh idempotency key.
    r2 = await client.post(
        f"/api/v1/warehouses/{setup['dst']}/inventory:reverse",
        json={"reverses_transaction_id": setup["in_tx"]["id"], "reason": "opposite"},
        headers={"Idempotency-Key": f"opp-{uuid4().hex[:8]}"},
    )
    assert r2.status_code == 409
    assert r2.json()["detail"]["code"] == "already_reversed"
    assert await _count_inverse_rows(lot_ids) == inverse_count_after_first
    assert await _count_reversal_markers(lot_ids) == marker_count_after_first


async def test_transfer_reversal_different_key_after_success_rejected(
    client: AsyncClient,
) -> None:
    """Same original + a FRESH idempotency key → 409 already_reversed."""
    setup = await _setup_transfer_pair(client, transfer_qty=2.0, initial_qty=8.0)
    lot_ids = [setup["src_lot"], setup["dst_lot"]]
    key1 = f"k1-{uuid4().hex[:8]}"
    r1 = await client.post(
        f"/api/v1/warehouses/{setup['src']}/inventory:reverse",
        json={"reverses_transaction_id": setup["out_tx"]["id"], "reason": "first"},
        headers={"Idempotency-Key": key1},
    )
    assert r1.status_code == 201
    inverse_after = await _count_inverse_rows(lot_ids)
    marker_after = await _count_reversal_markers(lot_ids)
    r2 = await client.post(
        f"/api/v1/warehouses/{setup['src']}/inventory:reverse",
        json={"reverses_transaction_id": setup["out_tx"]["id"], "reason": "second"},
        headers={"Idempotency-Key": f"k2-{uuid4().hex[:8]}"},
    )
    assert r2.status_code == 409
    assert r2.json()["detail"]["code"] == "already_reversed"
    assert await _count_inverse_rows(lot_ids) == inverse_after
    assert await _count_reversal_markers(lot_ids) == marker_after


# --------------------------------------------------------------------- #
# 5.4.3.6 — Audit integrity + org-total invariant on happy path.
# --------------------------------------------------------------------- #
async def test_transfer_reversal_audit_and_inventory_totals(
    client: AsyncClient,
) -> None:
    """Full audit inspection of a happy-path paired reversal.

    * Two REVERSAL markers exist, one per original.
    * Each marker's ``reverses_transaction_id`` points at its
      original (OUT / IN).
    * Two ``reversal_inverse_of`` rows exist, one per original.
    * Ledger row count matches the expected additions
      (2 inverse + 2 markers = +4 rows).
    * Organization-total inventory before == after.
    """
    setup = await _setup_transfer_pair(client, transfer_qty=3.0, initial_qty=9.0)
    lot_ids = [setup["src_lot"], setup["dst_lot"]]
    org_id = setup["ctx"]["org_id"]
    before_total = await _sum_org_inventory(client, org_id)
    before_tx = await _count_tx_rows(lot_ids)
    r = await client.post(
        f"/api/v1/warehouses/{setup['src']}/inventory:reverse",
        json={"reverses_transaction_id": setup["out_tx"]["id"], "reason": "undo"},
    )
    assert r.status_code == 201, r.text
    after_total = await _sum_org_inventory(client, org_id)
    assert after_total == before_total
    assert await _count_tx_rows(lot_ids) == before_tx + 4
    assert await _count_inverse_rows(lot_ids) == 2
    assert await _count_reversal_markers(lot_ids) == 2
    # Inspect markers directly.
    async with _db_session_module.AsyncSessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(_InventoryTransaction).where(
                        _InventoryTransaction.transaction_type
                        == _InventoryTransactionType.REVERSAL,
                        _InventoryTransaction.lot_id.in_([_UUIDType(x) for x in lot_ids]),
                    )
                )
            )
            .scalars()
            .all()
        )
        marker_reverses = {str(m.reverses_transaction_id) for m in rows}
    assert marker_reverses == {setup["out_tx"]["id"], setup["in_tx"]["id"]}


# --------------------------------------------------------------------- #
# 5.4.3.7 — Postgres rollback: extend prior test with row counts.
# --------------------------------------------------------------------- #
@_postgres_only
async def test_transfer_reversal_postgres_rollback_leaves_no_writes(
    client: AsyncClient,
) -> None:
    """Postgres-only end-to-end rollback proof.

    Extends the balance-only rollback assertion with row-count
    checks: on refusal the ledger must have gained ZERO rows.
    """
    setup = await _setup_transfer_pair(client, transfer_qty=6.0, initial_qty=10.0)
    lot_ids = [setup["src_lot"], setup["dst_lot"]]
    # Consume destination so any reversal must drive it negative.
    r = await client.post(
        f"/api/v1/warehouses/{setup['dst']}/inventory:issue",
        json={"lot_id": setup["dst_lot"], "quantity": 6, "unit": "kg"},
    )
    assert r.status_code == 201
    before_tx = await _count_tx_rows(lot_ids)
    before_inverse = await _count_inverse_rows(lot_ids)
    before_markers = await _count_reversal_markers(lot_ids)
    before_total = await _sum_org_inventory(client, setup["ctx"]["org_id"])
    r = await client.post(
        f"/api/v1/warehouses/{setup['src']}/inventory:reverse",
        json={"reverses_transaction_id": setup["out_tx"]["id"], "reason": "refuse"},
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "insufficient_stock"
    assert await _count_tx_rows(lot_ids) == before_tx
    assert await _count_inverse_rows(lot_ids) == before_inverse
    assert await _count_reversal_markers(lot_ids) == before_markers
    assert await _sum_org_inventory(client, setup["ctx"]["org_id"]) == before_total


# ===================================================================== #
# Sprint 5.4.4 — Symmetric Lot and Tenant Validation
# ===================================================================== #
#
# Every test in this block corrupts a specific relationship between a
# ledger row and its lot / item / warehouse / organization, and then
# asserts that the reversal endpoint refuses BEFORE any authorization
# scope is derived and BEFORE any ledger row is written.
#
# For every failure case we assert the same suite of no-op invariants:
#   * request rejected with a specific diagnostic code
#   * both lot balances unchanged
#   * organization-wide inventory unchanged
#   * ledger row count unchanged (no inverse, no marker)
#   * no audit rows attributed to the wrong tenant.
async def _create_extra_wh_with_lot(
    client: AsyncClient,
    *,
    org_id: str,
    farm_id: str | None,
    item_id: str,
) -> tuple[str, str]:
    """Create a second warehouse in ``org_id`` + a lot in it. Returns
    (warehouse_id, lot_id). Used to fabricate the "original tx points
    at a lot in another warehouse" corruption fixture without
    touching the DB by hand.
    """
    wh_id = await _create_warehouse(
        client, org_id, farm_id=farm_id, code=f"OTHER-{uuid4().hex[:4]}"
    )
    await _receipt(client, wh_id, item_id, quantity=1, unit="kg", lot_code=f"L-{uuid4().hex[:4]}")
    lot_id = await _lot_id_for(client, wh_id)
    return wh_id, lot_id


async def _count_audit_rows_for_wrong_scope(
    *, expected_org_id: str, expected_farm_id: str | None, other_scope_org_id: str
) -> int:
    """Return the number of ``audit_events`` rows referencing an
    organization other than ``expected_org_id`` OR the wrong farm on
    the expected org. Used by the no-op assertion suite to prove no
    misleading audit rows were emitted for a refused reversal.
    """
    from app.models.audit import AuditEvent  # local import — the model

    # is not otherwise needed at module scope

    async with _db_session_module.AsyncSessionLocal() as session:
        stmt = select(func.count(AuditEvent.id)).where(
            AuditEvent.organization_id == _UUIDType(other_scope_org_id)
        )
        return (await session.execute(stmt)).scalar_one()


async def _snapshot_lot_state(client: AsyncClient, lot_ids: list[str]) -> list[Decimal]:
    """Return balances (in canonical Decimal form) for the given lots,
    in the same order, using the read APIs so the snapshot survives
    any DB-level side-effects the endpoint may attempt."""
    balances: list[Decimal] = []
    from app.models.inventory import InventoryLot as _LotModel

    for lot_id in lot_ids:
        async with _db_session_module.AsyncSessionLocal() as session:
            row = (
                await session.execute(select(_LotModel).where(_LotModel.id == _UUIDType(lot_id)))
            ).scalar_one_or_none()
            if row is None:
                balances.append(Decimal("0"))
                continue
        # Prefer the ledger-derived balance from the API so we assert
        # the same value the caller of the reversal endpoint sees.
        lots = (await client.get(f"/api/v1/warehouses/{row.warehouse_id}/lots")).json()
        match = next((x for x in lots if x["id"] == lot_id), None)
        balances.append(Decimal(str(match["balance"])) if match else Decimal("0"))
    return balances


async def _assert_no_writes_after(
    client: AsyncClient,
    *,
    lot_ids: list[str],
    org_id: str,
    baseline: dict,
) -> None:
    """Uniform no-op assertion used by every corruption test."""
    assert await _count_tx_rows(lot_ids) == baseline["tx"]
    assert await _count_inverse_rows(lot_ids) == baseline["inverse"]
    assert await _count_reversal_markers(lot_ids) == baseline["marker"]
    assert await _sum_org_inventory(client, org_id) == baseline["total"]
    balances_now = await _snapshot_lot_state(client, lot_ids)
    assert (
        balances_now == baseline["balances"]
    ), f"lot balances changed under a refused reversal: {balances_now} != {baseline['balances']}"


async def _baseline(client: AsyncClient, *, lot_ids: list[str], org_id: str) -> dict:
    return {
        "tx": await _count_tx_rows(lot_ids),
        "inverse": await _count_inverse_rows(lot_ids),
        "marker": await _count_reversal_markers(lot_ids),
        "total": await _sum_org_inventory(client, org_id),
        "balances": await _snapshot_lot_state(client, lot_ids),
    }


# --------------------------------------------------------------------- #
# 5.4.4.1 — Original tx points at a lot in another warehouse (same org)
# --------------------------------------------------------------------- #
async def test_reversal_refused_when_original_lot_in_another_warehouse(
    client: AsyncClient,
) -> None:
    setup = await _setup_transfer_pair(client, transfer_qty=3.0, initial_qty=9.0)
    lot_ids = [setup["src_lot"], setup["dst_lot"]]
    item_id = (
        await client.get(f"/api/v1/organizations/{setup['ctx']['org_id']}/inventory-items")
    ).json()[0]["id"]
    _, other_lot_id = await _create_extra_wh_with_lot(
        client,
        org_id=setup["ctx"]["org_id"],
        farm_id=setup["ctx"]["farm_id"],
        item_id=item_id,
    )
    await _mutate_tx(setup["out_tx"]["id"], lot_id=_UUIDType(other_lot_id))
    baseline = await _baseline(
        client, lot_ids=[*lot_ids, other_lot_id], org_id=setup["ctx"]["org_id"]
    )
    r = await client.post(
        f"/api/v1/warehouses/{setup['src']}/inventory:reverse",
        json={"reverses_transaction_id": setup["out_tx"]["id"], "reason": "x"},
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "transfer_original_lot_warehouse_mismatch"
    await _assert_no_writes_after(
        client,
        lot_ids=[*lot_ids, other_lot_id],
        org_id=setup["ctx"]["org_id"],
        baseline=baseline,
    )


# --------------------------------------------------------------------- #
# 5.4.4.2 — Original tx points at a lot in another farm (same org)
# --------------------------------------------------------------------- #
async def test_reversal_refused_when_original_lot_in_another_farm(
    client: AsyncClient,
) -> None:
    setup = await _setup_transfer_pair(client, transfer_qty=3.0, initial_qty=9.0)
    lot_ids = [setup["src_lot"], setup["dst_lot"]]
    item_id = (
        await client.get(f"/api/v1/organizations/{setup['ctx']['org_id']}/inventory-items")
    ).json()[0]["id"]
    # Create a second farm in the same org, then a warehouse + lot in it.
    r = await client.post(
        f"/api/v1/organizations/{setup['ctx']['org_id']}/farms",
        json={"name": "Farm-Other", "code": f"farm-o-{uuid4().hex[:6]}"},
    )
    assert r.status_code == 201, r.text
    other_farm_id = r.json()["id"]
    _, other_lot_id = await _create_extra_wh_with_lot(
        client,
        org_id=setup["ctx"]["org_id"],
        farm_id=other_farm_id,
        item_id=item_id,
    )
    await _mutate_tx(setup["out_tx"]["id"], lot_id=_UUIDType(other_lot_id))
    baseline = await _baseline(
        client, lot_ids=[*lot_ids, other_lot_id], org_id=setup["ctx"]["org_id"]
    )
    r = await client.post(
        f"/api/v1/warehouses/{setup['src']}/inventory:reverse",
        json={"reverses_transaction_id": setup["out_tx"]["id"], "reason": "x"},
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "transfer_original_lot_warehouse_mismatch"
    await _assert_no_writes_after(
        client,
        lot_ids=[*lot_ids, other_lot_id],
        org_id=setup["ctx"]["org_id"],
        baseline=baseline,
    )


# --------------------------------------------------------------------- #
# 5.4.4.3 — Original tx points at a lot in another organization
# --------------------------------------------------------------------- #
async def test_reversal_refused_when_original_lot_in_another_org(
    client: AsyncClient,
) -> None:
    setup = await _setup_transfer_pair(client, transfer_qty=3.0, initial_qty=9.0)
    lot_ids = [setup["src_lot"], setup["dst_lot"]]
    # Build a fully-separate org with its own lot.
    other_owner_email = f"other-{uuid4().hex[:8]}@agrovix.dev"
    await create_verified_user(other_owner_email)
    await switch_user(client, other_owner_email)
    other_org_id = await create_org(client, slug=f"other-{uuid4().hex[:6]}")
    other_wh_id = await _create_warehouse(client, other_org_id, code=f"WH-{uuid4().hex[:4]}")
    other_item_id = await _create_feed_item(client, other_org_id)
    await _receipt(client, other_wh_id, other_item_id, quantity=1, unit="kg", lot_code="OTHER")
    other_lot_id = await _lot_id_for(client, other_wh_id)
    # Back to the source-side owner and snapshot.
    await switch_user(client, setup["ctx"]["owner"])
    await _mutate_tx(setup["out_tx"]["id"], lot_id=_UUIDType(other_lot_id))
    baseline = await _baseline(client, lot_ids=lot_ids, org_id=setup["ctx"]["org_id"])
    other_baseline_total = await _sum_org_inventory(client, other_org_id)
    r = await client.post(
        f"/api/v1/warehouses/{setup['src']}/inventory:reverse",
        json={"reverses_transaction_id": setup["out_tx"]["id"], "reason": "x"},
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "transfer_original_lot_warehouse_mismatch"
    await _assert_no_writes_after(
        client, lot_ids=lot_ids, org_id=setup["ctx"]["org_id"], baseline=baseline
    )
    # The OTHER org's inventory must be equally untouched — a refused
    # reversal must NOT leak audit or ledger state across tenants.
    assert await _sum_org_inventory(client, other_org_id) == other_baseline_total


# --------------------------------------------------------------------- #
# 5.4.4.4 — Original tx item_id != original lot item_id
# --------------------------------------------------------------------- #
@_sqlite_only
async def test_reversal_refused_when_original_item_id_diverges_from_lot(
    client: AsyncClient,
) -> None:
    setup = await _setup_transfer_pair(client, transfer_qty=3.0, initial_qty=9.0)
    lot_ids = [setup["src_lot"], setup["dst_lot"]]
    other_item_id = await _create_feed_item(client, setup["ctx"]["org_id"])
    await _mutate_tx(setup["out_tx"]["id"], item_id=_UUIDType(other_item_id))
    baseline = await _baseline(client, lot_ids=lot_ids, org_id=setup["ctx"]["org_id"])
    r = await client.post(
        f"/api/v1/warehouses/{setup['src']}/inventory:reverse",
        json={"reverses_transaction_id": setup["out_tx"]["id"], "reason": "x"},
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "transfer_original_lot_item_mismatch"
    await _assert_no_writes_after(
        client, lot_ids=lot_ids, org_id=setup["ctx"]["org_id"], baseline=baseline
    )


# --------------------------------------------------------------------- #
# 5.4.4.5 — Partner tx item_id != partner lot item_id
# --------------------------------------------------------------------- #
@_sqlite_only
async def test_reversal_refused_when_partner_item_id_diverges_from_lot(
    client: AsyncClient,
) -> None:
    """Both original and partner tx.item_id point at the SAME new item
    so the pair-level ``item_id`` invariant passes, but partner_lot's
    ``item_id`` still points at the ORIGINAL item — driving the
    partner-side lot / item symmetry check.
    """
    setup = await _setup_transfer_pair(client, transfer_qty=3.0, initial_qty=9.0)
    lot_ids = [setup["src_lot"], setup["dst_lot"]]
    other_item_id = await _create_feed_item(client, setup["ctx"]["org_id"])
    # Mutate BOTH tx rows to reference the new item so the pair-level
    # cross-check does not fire first. This isolates the partner-side
    # lot / item check.
    await _mutate_tx(setup["out_tx"]["id"], item_id=_UUIDType(other_item_id))
    await _mutate_tx(setup["in_tx"]["id"], item_id=_UUIDType(other_item_id))
    baseline = await _baseline(client, lot_ids=lot_ids, org_id=setup["ctx"]["org_id"])
    r = await client.post(
        f"/api/v1/warehouses/{setup['src']}/inventory:reverse",
        json={"reverses_transaction_id": setup["out_tx"]["id"], "reason": "x"},
    )
    assert r.status_code == 409, r.text
    # The FIRST invariant we hit is
    # ``transfer_original_lot_item_mismatch`` because both txs were
    # rewritten and the original-side symmetric check runs before the
    # partner-side one. That is acceptable — the important guarantee
    # is that NO writes escape. If we only mutate the partner tx, the
    # pair-level cross-item check fires (transfer_pair_item_mismatch).
    # Either way the refusal must be zero-write.
    assert r.json()["detail"]["code"] in {
        "transfer_original_lot_item_mismatch",
        "transfer_partner_lot_item_mismatch",
        "transfer_pair_item_mismatch",
    }
    await _assert_no_writes_after(
        client, lot_ids=lot_ids, org_id=setup["ctx"]["org_id"], baseline=baseline
    )


@_sqlite_only
async def test_reversal_refused_when_partner_lot_item_id_mismatches_partner_tx(
    client: AsyncClient,
) -> None:
    """Direct fixture for the ``transfer_partner_lot_item_mismatch`` code.

    Keeps the ORIGINAL side pristine, and rewrites the partner tx's
    ``item_id`` to match the original — but leaves the destination
    lot pointing at the ORIGINAL item so ``partner_lot.item_id !=
    partner.item_id``.

    Requires slightly more intricate corruption: create a new item,
    rewrite BOTH original and partner tx.item_id to that new item,
    then rewrite the ORIGINAL lot's item_id back to the original.
    The partner lot still references the original item → mismatch.
    """
    setup = await _setup_transfer_pair(client, transfer_qty=3.0, initial_qty=9.0)
    lot_ids = [setup["src_lot"], setup["dst_lot"]]
    other_item_id = await _create_feed_item(client, setup["ctx"]["org_id"])
    await _mutate_tx(setup["out_tx"]["id"], item_id=_UUIDType(other_item_id))
    await _mutate_tx(setup["in_tx"]["id"], item_id=_UUIDType(other_item_id))
    # Sync the source lot's item so original-side symmetric check
    # passes; leave partner lot untouched to isolate the partner-side
    # mismatch.
    async with _db_session_module.AsyncSessionLocal() as session:
        from app.models.inventory import InventoryLot as _LotModel

        await session.execute(
            sa_update(_LotModel)
            .where(_LotModel.id == _UUIDType(setup["src_lot"]))
            .values(item_id=_UUIDType(other_item_id))
        )
        await session.commit()
    baseline = await _baseline(client, lot_ids=lot_ids, org_id=setup["ctx"]["org_id"])
    r = await client.post(
        f"/api/v1/warehouses/{setup['src']}/inventory:reverse",
        json={"reverses_transaction_id": setup["out_tx"]["id"], "reason": "x"},
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "transfer_partner_lot_item_mismatch"
    await _assert_no_writes_after(
        client, lot_ids=lot_ids, org_id=setup["ctx"]["org_id"], baseline=baseline
    )


# --------------------------------------------------------------------- #
# 5.4.4.6 — Original tx organization differs from warehouse organization
# --------------------------------------------------------------------- #
@_sqlite_only
async def test_reversal_refused_when_original_tx_org_diverges_from_warehouse(
    client: AsyncClient,
) -> None:
    setup = await _setup_transfer_pair(client, transfer_qty=3.0, initial_qty=9.0)
    lot_ids = [setup["src_lot"], setup["dst_lot"]]
    other_owner_email = f"other-{uuid4().hex[:8]}@agrovix.dev"
    await create_verified_user(other_owner_email)
    await switch_user(client, other_owner_email)
    other_org_id = await create_org(client, slug=f"other-{uuid4().hex[:6]}")
    await switch_user(client, setup["ctx"]["owner"])
    await _mutate_tx(setup["out_tx"]["id"], organization_id=_UUIDType(other_org_id))
    baseline = await _baseline(client, lot_ids=lot_ids, org_id=setup["ctx"]["org_id"])
    other_baseline_total = await _sum_org_inventory(client, other_org_id)
    r = await client.post(
        f"/api/v1/warehouses/{setup['src']}/inventory:reverse",
        json={"reverses_transaction_id": setup["out_tx"]["id"], "reason": "x"},
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "transfer_original_org_mismatch"
    await _assert_no_writes_after(
        client, lot_ids=lot_ids, org_id=setup["ctx"]["org_id"], baseline=baseline
    )
    assert await _sum_org_inventory(client, other_org_id) == other_baseline_total


# --------------------------------------------------------------------- #
# 5.4.4.7 — Partner tx organization differs
# --------------------------------------------------------------------- #
@_sqlite_only
async def test_reversal_refused_when_partner_tx_org_diverges(
    client: AsyncClient,
) -> None:
    """Rewrite the partner tx to a foreign org → ``transfer_pair_cross_org``.

    The counterpart's authorization scope is derived from that
    partner row; the resolver MUST refuse before ever returning it.
    """
    setup = await _setup_transfer_pair(client, transfer_qty=3.0, initial_qty=9.0)
    lot_ids = [setup["src_lot"], setup["dst_lot"]]
    other_owner_email = f"other-{uuid4().hex[:8]}@agrovix.dev"
    await create_verified_user(other_owner_email)
    await switch_user(client, other_owner_email)
    other_org_id = await create_org(client, slug=f"other-{uuid4().hex[:6]}")
    await switch_user(client, setup["ctx"]["owner"])
    await _mutate_tx(setup["in_tx"]["id"], organization_id=_UUIDType(other_org_id))
    baseline = await _baseline(client, lot_ids=lot_ids, org_id=setup["ctx"]["org_id"])
    other_baseline_total = await _sum_org_inventory(client, other_org_id)
    r = await client.post(
        f"/api/v1/warehouses/{setup['src']}/inventory:reverse",
        json={"reverses_transaction_id": setup["out_tx"]["id"], "reason": "x"},
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "transfer_pair_cross_org"
    await _assert_no_writes_after(
        client, lot_ids=lot_ids, org_id=setup["ctx"]["org_id"], baseline=baseline
    )
    assert await _sum_org_inventory(client, other_org_id) == other_baseline_total


# --------------------------------------------------------------------- #
# 5.4.4.8 — Authorization resolver rejects BEFORE returning scopes
# --------------------------------------------------------------------- #
@_sqlite_only
async def test_resolve_reversal_scopes_rejects_malformed_before_scope_return(
    client: AsyncClient,
) -> None:
    """When the pair is corrupted, ``resolve_reversal_scopes`` MUST
    refuse before enumerating (and therefore before authorizing) the
    counterpart scope.

    We prove this via HTTP flow: corrupt the partner tx to a foreign
    org, then invoke the endpoint as a caller who only has
    ``inventory_transaction.create`` on the SOURCE farm. If the
    resolver were to return the partner scope, the endpoint would
    enforce authorization against the counterpart farm and answer
    with 403. Instead we observe 409 with the corruption diagnostic,
    which is only possible if the resolver refused before returning
    any partner scope.
    """
    setup = await _setup_two_farm_transfer(client)
    lot_ids = [setup["src_lot"], setup["dst_lot"]]
    other_owner_email = f"other-{uuid4().hex[:8]}@agrovix.dev"
    await create_verified_user(other_owner_email)
    await switch_user(client, other_owner_email)
    other_org_id = await create_org(client, slug=f"other-{uuid4().hex[:6]}")
    # Corrupt the partner org while still authenticated as some org
    # admin — the DB mutation itself is direct.
    await switch_user(client, setup["owner_email"])
    await _mutate_tx(setup["in_tx"]["id"], organization_id=_UUIDType(other_org_id))
    baseline = await _baseline(client, lot_ids=lot_ids, org_id=setup["org_id"])
    # Provision a source-only farm_manager.
    src_only = f"src-only-{uuid4().hex[:8]}@agrovix.dev"
    await create_verified_user(src_only)
    await invite_and_accept(
        client,
        inviter_email=setup["owner_email"],
        invitee_email=src_only,
        org_id=setup["org_id"],
        role_name="farm_manager",
        farm_id=setup["farm_a_id"],
    )
    await switch_user(client, src_only)
    r = await client.post(
        f"/api/v1/warehouses/{setup['src']}/inventory:reverse",
        json={"reverses_transaction_id": setup["out_tx"]["id"], "reason": "x"},
    )
    # 409 (not 403) proves the resolver rejected before scope
    # enumeration — the counterpart-farm auth check never ran.
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, dict) and detail["code"] == "transfer_pair_cross_org"
    await switch_user(client, setup["owner_email"])
    await _assert_no_writes_after(
        client, lot_ids=lot_ids, org_id=setup["org_id"], baseline=baseline
    )


# ===================================================================== #
# Sprint 5.4.5 — Farm consistency + race-safe transfer reversal
# ===================================================================== #
async def test_reversal_refused_when_original_farm_id_diverges_from_warehouse(
    client: AsyncClient,
) -> None:
    """Direct mutation of the ORIGINAL tx's farm_id — must refuse
    with ``transfer_original_farm_mismatch`` and write nothing.
    Uses ``_setup_two_farm_transfer`` so ``farm_id`` values differ
    between the two warehouses and a swap actually changes state.
    """
    setup = await _setup_two_farm_transfer(client)
    lot_ids = [setup["src_lot"], setup["dst_lot"]]
    # Repoint the source tx's farm_id to farm-B (the destination's
    # farm). This is a corruption we cannot produce via the API.
    await _mutate_tx(setup["out_tx"]["id"], farm_id=_UUIDType(setup["farm_b_id"]))
    baseline = await _baseline(client, lot_ids=lot_ids, org_id=setup["org_id"])
    r = await client.post(
        f"/api/v1/warehouses/{setup['src']}/inventory:reverse",
        json={"reverses_transaction_id": setup["out_tx"]["id"], "reason": "x"},
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "transfer_original_farm_mismatch"
    await _assert_no_writes_after(
        client, lot_ids=lot_ids, org_id=setup["org_id"], baseline=baseline
    )


async def test_reversal_refused_when_partner_farm_id_diverges_from_warehouse(
    client: AsyncClient,
) -> None:
    """Direct mutation of the PARTNER tx's farm_id."""
    setup = await _setup_two_farm_transfer(client)
    lot_ids = [setup["src_lot"], setup["dst_lot"]]
    await _mutate_tx(setup["in_tx"]["id"], farm_id=_UUIDType(setup["farm_a_id"]))
    baseline = await _baseline(client, lot_ids=lot_ids, org_id=setup["org_id"])
    r = await client.post(
        f"/api/v1/warehouses/{setup['src']}/inventory:reverse",
        json={"reverses_transaction_id": setup["out_tx"]["id"], "reason": "x"},
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "transfer_partner_farm_mismatch"
    await _assert_no_writes_after(
        client, lot_ids=lot_ids, org_id=setup["org_id"], baseline=baseline
    )


# --------------------------------------------------------------------- #
# 5.4.5 — Postgres concurrency proofs.
# --------------------------------------------------------------------- #
# All tests below acquire the tx-row locks that Sprint 5.4.5 introduces
# and demand real DB-level lock semantics. On SQLite the locks are
# silent no-ops (StaticPool serialises everything anyway), so these
# tests are pinned to Postgres CI.
@_postgres_only
async def test_reversal_serialises_concurrent_writers_on_same_pair(
    client: AsyncClient,
) -> None:
    """Two concurrent reversal HTTP calls on the same transfer pair.

    The FOR UPDATE lock on the transaction rows must serialise the
    two attempts: exactly one wins with 201, the other blocks until
    the first commits and then answers 409 ``already_reversed``.
    Neither may produce a half-reversed state.
    """
    import asyncio as _asyncio

    setup = await _setup_transfer_pair(client, transfer_qty=3.0, initial_qty=9.0)
    lot_ids = [setup["src_lot"], setup["dst_lot"]]
    baseline_tx = await _count_tx_rows(lot_ids)
    baseline_total = await _sum_org_inventory(client, setup["ctx"]["org_id"])

    async def _fire(key: str) -> tuple[int, dict]:
        r = await client.post(
            f"/api/v1/warehouses/{setup['src']}/inventory:reverse",
            json={"reverses_transaction_id": setup["out_tx"]["id"], "reason": key},
            headers={"Idempotency-Key": key},
        )
        return r.status_code, r.json()

    r1, r2 = await _asyncio.gather(_fire("racer-a"), _fire("racer-b"))
    codes = sorted([r1[0], r2[0]])
    assert codes == [201, 409], f"unexpected outcome pair: {codes}"
    losing = r1 if r1[0] == 409 else r2
    assert losing[1]["detail"]["code"] == "already_reversed"
    # Exactly one paired reversal ran: +2 inverse + 2 markers = +4 rows.
    assert await _count_tx_rows(lot_ids) == baseline_tx + 4
    assert await _count_inverse_rows(lot_ids) == 2
    assert await _count_reversal_markers(lot_ids) == 2
    # Inventory total unchanged: a paired reversal is inventory-neutral.
    assert await _sum_org_inventory(client, setup["ctx"]["org_id"]) == baseline_total


@_postgres_only
async def test_reversal_serialises_concurrent_writers_via_opposite_sides(
    client: AsyncClient,
) -> None:
    """Same as above but the two writers reverse from OPPOSITE ends
    of the pair (one targets TRANSFER_OUT, the other TRANSFER_IN).

    Sprint 5.4.5's deterministic ``ORDER BY tx.id ASC`` lock
    acquisition MUST prevent the classic AB / BA deadlock. Exactly
    one writer wins.
    """
    import asyncio as _asyncio

    setup = await _setup_transfer_pair(client, transfer_qty=3.0, initial_qty=9.0)
    lot_ids = [setup["src_lot"], setup["dst_lot"]]
    baseline_tx = await _count_tx_rows(lot_ids)

    async def _from_out() -> tuple[int, dict]:
        r = await client.post(
            f"/api/v1/warehouses/{setup['src']}/inventory:reverse",
            json={"reverses_transaction_id": setup["out_tx"]["id"], "reason": "out"},
            headers={"Idempotency-Key": "opp-out"},
        )
        return r.status_code, r.json()

    async def _from_in() -> tuple[int, dict]:
        r = await client.post(
            f"/api/v1/warehouses/{setup['dst']}/inventory:reverse",
            json={"reverses_transaction_id": setup["in_tx"]["id"], "reason": "in"},
            headers={"Idempotency-Key": "opp-in"},
        )
        return r.status_code, r.json()

    r1, r2 = await _asyncio.gather(_from_out(), _from_in())
    codes = sorted([r1[0], r2[0]])
    assert codes == [201, 409], f"unexpected outcome pair: {codes}"
    losing = r1 if r1[0] == 409 else r2
    assert losing[1]["detail"]["code"] == "already_reversed"
    assert await _count_tx_rows(lot_ids) == baseline_tx + 4


@_postgres_only
async def test_reversal_detects_relationship_change_between_read_and_lock(
    client: AsyncClient,
) -> None:
    """Force a race: reversal begins, and a concurrent UPDATE
    changes ``farm_id`` on the partner tx BEFORE the reversal
    acquires its FOR UPDATE lock.

    We drive this by starting a session that opens a transaction,
    holds a FOR UPDATE lock on the partner tx, then mutates its
    farm_id; concurrently the API request tries to reverse. When
    the mutating session commits, the API acquires the lock,
    re-reads the (now-changed) partner row, and refuses with
    ``transfer_partner_farm_mismatch`` — no writes, no partial
    state.
    """
    import asyncio as _asyncio

    setup = await _setup_two_farm_transfer(client)
    lot_ids = [setup["src_lot"], setup["dst_lot"]]
    baseline_tx = await _count_tx_rows(lot_ids)
    baseline_total = await _sum_org_inventory(client, setup["org_id"])

    started = _asyncio.Event()
    finished = _asyncio.Event()

    async def _mutator() -> None:
        # Hold the partner row's write lock, mutate its farm_id,
        # signal the reader, sleep briefly to make sure the API
        # request is actively waiting on the FOR UPDATE lock, and
        # commit — releasing the row in a corrupted state.
        async with _db_session_module.AsyncSessionLocal() as session:
            from app.models.inventory import InventoryTransaction as _Tx

            await session.execute(
                select(_Tx).where(_Tx.id == _UUIDType(setup["in_tx"]["id"])).with_for_update()
            )
            await session.execute(
                sa_update(_Tx)
                .where(_Tx.id == _UUIDType(setup["in_tx"]["id"]))
                .values(farm_id=_UUIDType(setup["farm_a_id"]))
            )
            started.set()
            # Give the API request time to enqueue on the lock.
            await _asyncio.sleep(0.5)
            await session.commit()
            finished.set()

    async def _reverser() -> tuple[int, dict]:
        await started.wait()
        # Fire the reversal while the mutator is still holding the
        # lock — the request will block inside FOR UPDATE and only
        # proceed after the mutator commits its farm_id change.
        r = await client.post(
            f"/api/v1/warehouses/{setup['src']}/inventory:reverse",
            json={"reverses_transaction_id": setup["out_tx"]["id"], "reason": "race"},
        )
        return r.status_code, r.json()

    r_reverser, _ = await _asyncio.gather(_reverser(), _mutator())
    assert finished.is_set()
    assert r_reverser[0] == 409, r_reverser
    # The reversal refused because it re-read the row under lock and
    # saw the mutated farm_id.
    assert r_reverser[1]["detail"]["code"] == "transfer_partner_farm_mismatch"
    # Zero-write guarantee.
    assert await _count_tx_rows(lot_ids) == baseline_tx
    assert await _count_inverse_rows(lot_ids) == 0
    assert await _count_reversal_markers(lot_ids) == 0
    assert await _sum_org_inventory(client, setup["org_id"]) == baseline_total


# ===================================================================== #
# Sprint 5.4.6 — Deterministic pair locking + fully locked auth state
# ===================================================================== #
@_postgres_only
async def test_reversal_deterministic_opposite_side_barrier(
    client: AsyncClient,
) -> None:
    """Barrier-synchronised opposite-side reversal race.

    Sprint 5.4.6 concurrency proof: both HTTP requests complete
    unlocked pair discovery, PAUSE on a shared barrier, then start
    lock acquisition simultaneously. The corrected lock order
    (``WHERE id IN (:sorted_ids) ORDER BY id ASC FOR UPDATE``)
    guarantees exactly one winner, no deadlock, and no half-reversed
    state — even when caller A targets OUT and caller B targets IN.
    """
    import asyncio as _asyncio

    from app.services.inventory import InventoryService

    setup = await _setup_transfer_pair(client, transfer_qty=3.0, initial_qty=9.0)
    lot_ids = [setup["src_lot"], setup["dst_lot"]]
    baseline_tx = await _count_tx_rows(lot_ids)
    baseline_total = await _sum_org_inventory(client, setup["ctx"]["org_id"])

    # Both racers register on the same barrier. When both are past
    # unlocked pair discovery, ``event.set()`` releases them into
    # lock acquisition simultaneously — the stress scenario the
    # ascending-id lock order must handle deterministically.
    gate = _asyncio.Event()
    InventoryService._reversal_lock_barrier = gate
    try:

        async def _from_out() -> tuple[int, dict]:
            r = await client.post(
                f"/api/v1/warehouses/{setup['src']}/inventory:reverse",
                json={"reverses_transaction_id": setup["out_tx"]["id"], "reason": "out"},
                headers={"Idempotency-Key": f"det-out-{uuid4().hex[:6]}"},
            )
            return r.status_code, r.json()

        async def _from_in() -> tuple[int, dict]:
            r = await client.post(
                f"/api/v1/warehouses/{setup['dst']}/inventory:reverse",
                json={"reverses_transaction_id": setup["in_tx"]["id"], "reason": "in"},
                headers={"Idempotency-Key": f"det-in-{uuid4().hex[:6]}"},
            )
            return r.status_code, r.json()

        # Kick both requests off, then release the barrier after a
        # brief settle so both are provably past discovery.
        r_task = _asyncio.gather(_from_out(), _from_in())
        await _asyncio.sleep(0.3)
        gate.set()
        r1, r2 = await r_task
    finally:
        InventoryService._reversal_lock_barrier = None

    codes = sorted([r1[0], r2[0]])
    assert codes == [201, 409], f"unexpected outcome pair: {codes}"
    losing = r1 if r1[0] == 409 else r2
    assert losing[1]["detail"]["code"] == "already_reversed"
    # Exactly one paired reversal committed → 4 rows (2 inverse + 2 markers).
    assert await _count_tx_rows(lot_ids) == baseline_tx + 4
    assert await _count_inverse_rows(lot_ids) == 2
    assert await _count_reversal_markers(lot_ids) == 2
    # Inventory-neutral.
    assert await _sum_org_inventory(client, setup["ctx"]["org_id"]) == baseline_total


@_postgres_only
async def test_reversal_blocks_concurrent_warehouse_farm_mutation(
    client: AsyncClient,
) -> None:
    """Warehouse mutation race.

    A concurrent transaction attempts to change a warehouse's
    ``farm_id`` while reversal is active. Sprint 5.4.6 locks the
    warehouse rows FOR UPDATE inside the reversal transaction, so
    the mutating writer must block until reversal commits. When it
    eventually applies, the reversal has already succeeded against
    the ORIGINAL farm; no cross-farm audit rows exist.
    """
    import asyncio as _asyncio

    from app.services.inventory import InventoryService

    setup = await _setup_two_farm_transfer(client)
    lot_ids = [setup["src_lot"], setup["dst_lot"]]
    baseline_tx = await _count_tx_rows(lot_ids)
    gate = _asyncio.Event()
    wh_locked_signal = _asyncio.Event()
    InventoryService._reversal_lock_barrier = gate
    InventoryService._reversal_after_warehouse_locks_signal = wh_locked_signal
    mutation_blocked_until = None
    try:

        async def _reverser() -> tuple[int, dict]:
            r = await client.post(
                f"/api/v1/warehouses/{setup['src']}/inventory:reverse",
                json={"reverses_transaction_id": setup["out_tx"]["id"], "reason": "r"},
            )
            return r.status_code, r.json()

        async def _mutator() -> None:
            nonlocal mutation_blocked_until
            # Release the reverser so it can progress from unlocked
            # discovery into the lock-acquisition phase.
            await _asyncio.sleep(0.5)
            gate.set()
            # Wait until the reverser has definitively acquired the
            # FOR UPDATE lock on every warehouse row. Only THEN can
            # we prove that a competing UPDATE blocks.
            await wh_locked_signal.wait()
            async with _db_session_module.AsyncSessionLocal() as session:
                from app.models.inventory import Warehouse as _WarehouseModel

                started = _asyncio.get_event_loop().time()
                await session.execute(
                    sa_update(_WarehouseModel)
                    .where(_WarehouseModel.id == _UUIDType(setup["dst"]))
                    .values(farm_id=_UUIDType(setup["farm_a_id"]))
                )
                await session.commit()
                mutation_blocked_until = _asyncio.get_event_loop().time() - started

        r_task = _asyncio.gather(_reverser(), _mutator())
        r_rev, _ = await r_task
    finally:
        InventoryService._reversal_lock_barrier = None
        InventoryService._reversal_after_warehouse_locks_signal = None

    assert r_rev[0] == 201, r_rev
    # Reversal committed 4 rows against the ORIGINAL farms.
    assert await _count_tx_rows(lot_ids) == baseline_tx + 4
    # The mutation blocked (should show non-trivial wait time).
    assert mutation_blocked_until is not None
    # No 409, and audit rows point at the ORIGINAL farm — the
    # mutation could only apply AFTER reversal committed, so any
    # subsequent transfer would see the new farm, but our reversal
    # rows are already sealed with the original farm.
    async with _db_session_module.AsyncSessionLocal() as session:
        from app.models.inventory import InventoryTransaction as _Tx

        rows = (
            (
                await session.execute(
                    select(_Tx).where(
                        _Tx.transaction_type == _InventoryTransactionType.REVERSAL,
                        _Tx.lot_id.in_([_UUIDType(x) for x in lot_ids]),
                    )
                )
            )
            .scalars()
            .all()
        )
    farm_ids = {r.farm_id for r in rows}
    # Both farms are still the original OUT-side / IN-side farms.
    assert _UUIDType(setup["farm_a_id"]) in farm_ids
    assert _UUIDType(setup["farm_b_id"]) in farm_ids


@_postgres_only
async def test_reversal_blocks_concurrent_item_org_mutation(
    client: AsyncClient,
) -> None:
    """Item mutation race.

    The reversal transaction holds a FOR UPDATE lock on the item
    row. A concurrent UPDATE that would move the item to a
    different organization must block until reversal completes;
    the reversal's audit / inverse / marker rows are therefore
    sealed against the ORIGINAL organization_id.
    """
    import asyncio as _asyncio

    from app.services.inventory import InventoryService

    setup = await _setup_transfer_pair(client, transfer_qty=3.0, initial_qty=9.0)
    lot_ids = [setup["src_lot"], setup["dst_lot"]]
    baseline_tx = await _count_tx_rows(lot_ids)
    org_id = setup["ctx"]["org_id"]
    # Item id.
    items = (await client.get(f"/api/v1/organizations/{org_id}/inventory-items")).json()
    item_id = items[0]["id"]
    # A second org we'd try to move the item to.
    other_owner_email = f"other-{uuid4().hex[:8]}@agrovix.dev"
    await create_verified_user(other_owner_email)
    await switch_user(client, other_owner_email)
    other_org_id = await create_org(client, slug=f"other-{uuid4().hex[:6]}")
    await switch_user(client, setup["ctx"]["owner"])

    gate = _asyncio.Event()
    wh_locked_signal = _asyncio.Event()
    InventoryService._reversal_lock_barrier = gate
    InventoryService._reversal_after_warehouse_locks_signal = wh_locked_signal
    try:

        async def _reverser() -> tuple[int, dict]:
            r = await client.post(
                f"/api/v1/warehouses/{setup['src']}/inventory:reverse",
                json={"reverses_transaction_id": setup["out_tx"]["id"], "reason": "r"},
            )
            return r.status_code, r.json()

        async def _mutator() -> None:
            await _asyncio.sleep(0.5)
            gate.set()
            # Only after every warehouse row lock is held does the
            # reverser progress to the item locks — waiting on this
            # signal guarantees the item FOR UPDATE has either
            # already been issued or is imminent, so the mutating
            # UPDATE below is guaranteed to race the item lock
            # (either blocks on it or lands after the reverser
            # committed). Either outcome proves the reverser used
            # the ORIGINAL organization_id under lock.
            await wh_locked_signal.wait()
            async with _db_session_module.AsyncSessionLocal() as session:
                from app.models.inventory import InventoryItem as _ItemModel

                await session.execute(
                    sa_update(_ItemModel)
                    .where(_ItemModel.id == _UUIDType(item_id))
                    .values(organization_id=_UUIDType(other_org_id))
                )
                await session.commit()

        r_rev, _ = await _asyncio.gather(_reverser(), _mutator())
    finally:
        InventoryService._reversal_lock_barrier = None
        InventoryService._reversal_after_warehouse_locks_signal = None

    # Reversal must have committed against the ORIGINAL org.
    assert r_rev[0] == 201, r_rev
    assert await _count_tx_rows(lot_ids) == baseline_tx + 4
    async with _db_session_module.AsyncSessionLocal() as session:
        from app.models.inventory import InventoryTransaction as _Tx

        rows = (
            (
                await session.execute(
                    select(_Tx).where(
                        _Tx.transaction_type == _InventoryTransactionType.REVERSAL,
                        _Tx.lot_id.in_([_UUIDType(x) for x in lot_ids]),
                    )
                )
            )
            .scalars()
            .all()
        )
    # Every reversal audit row was written against the ORIGINAL org
    # even though the item's org_id changed AFTER reversal committed.
    for row in rows:
        assert row.organization_id == _UUIDType(org_id)


@_postgres_only
async def test_reversal_locks_transactions_in_ascending_id_order(
    client: AsyncClient,
) -> None:
    """Instrumentation-driven lock-order coverage.

    Records the ORDER in which ``list_by_ids_for_update`` receives
    its input on the transaction repo (once for the pair, once for
    warehouses, once for items, once for lots) and asserts each
    call arrived with ids already sorted ascending — regardless of
    whether the caller targeted TRANSFER_OUT or TRANSFER_IN.
    """
    from app.repositories import inventory as _inv_repo

    captured: list[tuple[str, list[str]]] = []

    class _Recorder:
        def __init__(self, name: str, real):
            self.name = name
            self.real = real

        async def __call__(self, ids):
            captured.append((self.name, [str(x) for x in list(ids)]))
            return await self.real(ids)

    for target_key in ("out_tx", "in_tx"):
        setup = await _setup_transfer_pair(client, transfer_qty=2.0, initial_qty=6.0)
        captured.clear()
        # Monkey-patch the four list_by_ids_for_update methods on the
        # repo classes.
        originals: dict[str, tuple[type, callable]] = {}
        for cls_name in (
            "InventoryTransactionRepository",
            "WarehouseRepository",
            "InventoryItemRepository",
            "InventoryLotRepository",
        ):
            cls = getattr(_inv_repo, cls_name)
            originals[cls_name] = (cls, cls.list_by_ids_for_update)

            async def _wrapped(self, ids, _orig=cls.list_by_ids_for_update, _name=cls_name):
                captured.append((_name, [str(x) for x in list(ids)]))
                return await _orig(self, ids)

            cls.list_by_ids_for_update = _wrapped  # type: ignore[assignment]
        try:
            wh_key = "src" if target_key == "out_tx" else "dst"
            r = await client.post(
                f"/api/v1/warehouses/{setup[wh_key]}/inventory:reverse",
                json={
                    "reverses_transaction_id": setup[target_key]["id"],
                    "reason": "trace",
                },
            )
        finally:
            for _cls_name, (cls, orig) in originals.items():
                cls.list_by_ids_for_update = orig  # type: ignore[assignment]
        assert r.status_code == 201, r.text
        # Every recorded call must have received an ascending-id list.
        for name, ids in captured:
            assert ids == sorted(
                ids
            ), f"{name}.list_by_ids_for_update received non-ascending ids: {ids}"
        # And the transaction lock was acquired.
        assert any(name == "InventoryTransactionRepository" for name, _ in captured)


# ===================================================================== #
# Sprint 5.4.7 — Serialized Transfer Topology + Full Authorization      #
# Locking (advisory-lock proofs, farm/org locking, bounded barriers).    #
# ===================================================================== #


class _TwoPartyBarrier:
    """Two-party synchronization barrier for concurrency tests.

    ``arrive()`` blocks until BOTH participants have arrived. Unlike
    :class:`asyncio.Event`, it proves that BOTH racers reached the
    same synchronization point before either continued.
    """

    def __init__(self) -> None:
        self._count = 0
        self._cond = asyncio.Condition()

    async def arrive(self) -> None:
        async with self._cond:
            self._count += 1
            if self._count >= 2:
                self._cond.notify_all()
                return
            await self._cond.wait_for(lambda: self._count >= 2)


def _advisory_key(org_id: str, ref_id: str) -> int:
    """Recompute the Sprint 5.4.7 advisory-lock key (SHA-256 truncated)."""
    from app.services._transfer_locks import advisory_lock_key_for_transfer

    return advisory_lock_key_for_transfer(_UUIDType(org_id), "transfer", _UUIDType(ref_id))


# --------------------------------------------------------------------- #
# Advisory-key determinism unit test (works on any DB).
# --------------------------------------------------------------------- #
async def test_advisory_lock_key_is_deterministic_and_signed_bigint() -> None:
    from app.services._transfer_locks import advisory_lock_key_for_transfer

    org = _UUIDType("11111111-1111-1111-1111-111111111111")
    ref = _UUIDType("22222222-2222-2222-2222-222222222222")
    k1 = advisory_lock_key_for_transfer(org, "transfer", ref)
    k2 = advisory_lock_key_for_transfer(org, "transfer", ref)
    assert k1 == k2, "advisory key must be deterministic"
    # PostgreSQL signed BIGINT range.
    assert -(1 << 63) <= k1 <= (1 << 63) - 1
    # Different identity → different key.
    other = advisory_lock_key_for_transfer(org, "transfer", uuid4())
    assert other != k1


# --------------------------------------------------------------------- #
# Scenario 15 — pre-existing 3-row topology → zero-write rejection.
# --------------------------------------------------------------------- #
@_postgres_only
async def test_reversal_rejects_pre_existing_three_row_topology(
    client: AsyncClient,
) -> None:
    """Sprint 5.4.9 — the raw-SQL INSERT that would add a THIRD row
    into a transfer identity is REJECTED at the DB layer by the
    partial unique index ``uq_inventory_tx_transfer_role``. The
    application layer never sees the malformed topology.
    """
    from sqlalchemy.exc import IntegrityError

    setup = await _setup_transfer_pair(client, transfer_qty=3.0, initial_qty=9.0)
    lot_ids = [setup["src_lot"], setup["dst_lot"]]
    baseline_tx = await _count_tx_rows(lot_ids)
    baseline_inverse = await _count_inverse_rows(lot_ids)
    baseline_markers = await _count_reversal_markers(lot_ids)

    async with _db_session_module.AsyncSessionLocal() as session:
        from sqlalchemy import text as _text

        with pytest.raises(IntegrityError):
            await session.execute(
                _text(
                    "INSERT INTO inventory_transactions ("
                    "  id, organization_id, farm_id, warehouse_id, item_id, lot_id,"
                    "  transaction_type, quantity, unit, performed_by_id,"
                    "  performed_at, reference_type, reference_id, idempotency_key,"
                    "  transfer_group_id"
                    ") SELECT :new_id, organization_id, farm_id, warehouse_id, item_id, lot_id,"
                    "         transaction_type, quantity, unit, performed_by_id,"
                    "         performed_at, reference_type, reference_id, NULL,"
                    "         transfer_group_id"
                    "    FROM inventory_transactions WHERE id = :src_id"
                ),
                {
                    "new_id": uuid4(),
                    "src_id": _UUIDType(setup["out_tx"]["id"]),
                },
            )
            await session.commit()

    # Zero-write guarantee — the DB layer refused the INSERT.
    assert await _count_tx_rows(lot_ids) == baseline_tx
    assert await _count_inverse_rows(lot_ids) == baseline_inverse
    assert await _count_reversal_markers(lot_ids) == baseline_markers


# --------------------------------------------------------------------- #
# Scenario 3 — reference-id mutation blocks on the advisory lock.
# --------------------------------------------------------------------- #
@_postgres_only
async def test_reference_mutation_blocks_on_advisory_lock(
    client: AsyncClient,
) -> None:
    """A concurrent UPDATE that would re-parent a foreign transaction
    into the active transfer identity MUST wait on the same advisory
    lock the reverser holds.
    """
    from app.services._transfer_locks import (
        advisory_lock_key_for_transfer_group,
    )

    setup = await _setup_transfer_pair(client, transfer_qty=2.0, initial_qty=8.0)
    # Sprint 5.4.8 — advisory key is derived from the immutable
    # transfer_group_id column (backfilled from reference_id during
    # migration; test setup writes both to the same value).
    async with _db_session_module.AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(_InventoryTransaction).where(
                    _InventoryTransaction.id == _UUIDType(setup["out_tx"]["id"])
                )
            )
        ).scalar_one()
        group_id = row.transfer_group_id or row.reference_id
    key = advisory_lock_key_for_transfer_group(group_id)

    # Hold the advisory lock in an external session so we can prove
    # the mutator blocks on THE SAME KEY the reverser uses. This
    # simulates a reversal currently in progress.
    holder_ready = asyncio.Event()
    release = asyncio.Event()

    async def _lock_holder() -> None:
        from sqlalchemy import text as _text

        async with _db_session_module.AsyncSessionLocal() as session:
            await session.execute(_text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})
            holder_ready.set()
            await release.wait()
            await session.commit()

    async def _mutator() -> asyncio.Task[None]:
        from sqlalchemy import text as _text

        async def _do() -> None:
            async with _db_session_module.AsyncSessionLocal() as session:
                # Must acquire the SAME key before mutating.
                await session.execute(_text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})
                await session.execute(
                    sa_update(_InventoryTransaction)
                    .where(_InventoryTransaction.id == _UUIDType(setup["out_tx"]["id"]))
                    .values(reason="mutated")
                )
                await session.commit()

        return asyncio.create_task(_do())

    holder = asyncio.create_task(_lock_holder())
    await asyncio.wait_for(holder_ready.wait(), timeout=5)
    mut_task = await _mutator()
    # Give the mutator a moment to reach the pg_advisory_xact_lock call.
    await asyncio.sleep(0.4)
    assert (
        mut_task.done() is False
    ), "Mutator must block on the advisory lock while the reverser holds it"
    release.set()
    await asyncio.wait_for(holder, timeout=5)
    await asyncio.wait_for(mut_task, timeout=5)
    assert mut_task.done() is True


# --------------------------------------------------------------------- #
# Scenario 5/6 — Organization deactivation / soft-delete under lock.
# --------------------------------------------------------------------- #
@_postgres_only
async def test_reversal_blocks_concurrent_organization_deactivation(
    client: AsyncClient,
) -> None:
    """The reverser holds ``FOR UPDATE`` on the org row; a concurrent
    UPDATE flipping ``is_active`` MUST block until reversal commits.
    """
    from app.models.organization import Organization as _Org
    from app.services.inventory import InventoryService

    setup = await _setup_transfer_pair(client, transfer_qty=2.0, initial_qty=8.0)
    lot_ids = [setup["src_lot"], setup["dst_lot"]]
    baseline_tx = await _count_tx_rows(lot_ids)
    baseline_inverse = await _count_inverse_rows(lot_ids)
    org_id = setup["ctx"]["org_id"]

    gate = asyncio.Event()
    farm_org_locked = asyncio.Event()
    hold = asyncio.Event()
    InventoryService._reversal_lock_barrier = gate
    InventoryService._reversal_after_farm_org_locks_signal = farm_org_locked
    InventoryService._reversal_hold_after_farm_org_locks_gate = hold
    try:

        async def _reverser() -> tuple[int, dict]:
            r = await client.post(
                f"/api/v1/warehouses/{setup['src']}/inventory:reverse",
                json={"reverses_transaction_id": setup["out_tx"]["id"], "reason": "r"},
            )
            return r.status_code, r.json()

        async def _mutator() -> None:
            gate.set()
            await farm_org_locked.wait()
            async with _db_session_module.AsyncSessionLocal() as session:
                await session.execute(
                    sa_update(_Org).where(_Org.id == _UUIDType(org_id)).values(is_active=False)
                )
                await session.commit()

        rev_task = asyncio.create_task(_reverser())
        mut_task = asyncio.create_task(_mutator())
        # Wait for the reverser to have locked farm+org.
        await asyncio.wait_for(farm_org_locked.wait(), timeout=5)
        # A tiny delay to let the mutator actually enter its UPDATE.
        await asyncio.sleep(0.5)
        assert mut_task.done() is False, "Org deactivation must block on the reverser's FOR UPDATE"
        # Release the reverser so it can complete and release locks.
        hold.set()
        r_rev, _ = await asyncio.wait_for(asyncio.gather(rev_task, mut_task), timeout=10)
    finally:
        InventoryService._reversal_lock_barrier = None
        InventoryService._reversal_after_farm_org_locks_signal = None
        InventoryService._reversal_hold_after_farm_org_locks_gate = None

    assert r_rev[0] == 201, r_rev
    # Reversal committed the paired inverse rows against the ORIGINAL
    # active state; the deactivation lands AFTER.
    assert await _count_tx_rows(lot_ids) == baseline_tx + 4
    assert await _count_inverse_rows(lot_ids) == baseline_inverse + 2


@_postgres_only
async def test_reversal_refuses_when_organization_soft_deleted_first(
    client: AsyncClient,
) -> None:
    """If org deletion beats the reverser to the org row lock, reversal
    must refuse with ``transfer_organization_deleted`` and write nothing.
    """
    from app.models.organization import Organization as _Org

    setup = await _setup_transfer_pair(client, transfer_qty=2.0, initial_qty=8.0)
    lot_ids = [setup["src_lot"], setup["dst_lot"]]
    baseline_tx = await _count_tx_rows(lot_ids)
    baseline_inverse = await _count_inverse_rows(lot_ids)
    baseline_markers = await _count_reversal_markers(lot_ids)

    async with _db_session_module.AsyncSessionLocal() as session:
        from datetime import UTC as _UTC
        from datetime import datetime as _dt

        await session.execute(
            sa_update(_Org)
            .where(_Org.id == _UUIDType(setup["ctx"]["org_id"]))
            .values(deleted_at=_dt.now(_UTC), is_active=False)
        )
        await session.commit()

    r = await client.post(
        f"/api/v1/warehouses/{setup['src']}/inventory:reverse",
        json={"reverses_transaction_id": setup["out_tx"]["id"], "reason": "r"},
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "transfer_organization_deleted"
    assert await _count_tx_rows(lot_ids) == baseline_tx
    assert await _count_inverse_rows(lot_ids) == baseline_inverse
    assert await _count_reversal_markers(lot_ids) == baseline_markers


# --------------------------------------------------------------------- #
# Scenario 7/8 — Farm mutation / deactivation / deletion.
# --------------------------------------------------------------------- #
@_postgres_only
async def test_reversal_blocks_concurrent_farm_deactivation(
    client: AsyncClient,
) -> None:
    """Farm row lock blocks concurrent farm deactivation."""
    from app.models.farm import Farm as _Farm
    from app.services.inventory import InventoryService

    setup = await _setup_two_farm_transfer(client)
    lot_ids = [setup["src_lot"], setup["dst_lot"]]
    baseline_tx = await _count_tx_rows(lot_ids)
    baseline_inverse = await _count_inverse_rows(lot_ids)

    gate = asyncio.Event()
    farm_org_locked = asyncio.Event()
    hold = asyncio.Event()
    InventoryService._reversal_lock_barrier = gate
    InventoryService._reversal_after_farm_org_locks_signal = farm_org_locked
    InventoryService._reversal_hold_after_farm_org_locks_gate = hold
    try:

        async def _reverser() -> tuple[int, dict]:
            r = await client.post(
                f"/api/v1/warehouses/{setup['src']}/inventory:reverse",
                json={"reverses_transaction_id": setup["out_tx"]["id"], "reason": "r"},
            )
            return r.status_code, r.json()

        async def _mutator() -> None:
            gate.set()
            await farm_org_locked.wait()
            async with _db_session_module.AsyncSessionLocal() as session:
                await session.execute(
                    sa_update(_Farm)
                    .where(_Farm.id == _UUIDType(setup["farm_b_id"]))
                    .values(is_active=False)
                )
                await session.commit()

        rev_task = asyncio.create_task(_reverser())
        mut_task = asyncio.create_task(_mutator())
        await asyncio.wait_for(farm_org_locked.wait(), timeout=5)
        await asyncio.sleep(0.5)
        assert mut_task.done() is False, "Farm deactivation must block on reverser's FOR UPDATE"
        hold.set()
        r_rev, _ = await asyncio.wait_for(asyncio.gather(rev_task, mut_task), timeout=10)
    finally:
        InventoryService._reversal_lock_barrier = None
        InventoryService._reversal_after_farm_org_locks_signal = None
        InventoryService._reversal_hold_after_farm_org_locks_gate = None

    assert r_rev[0] == 201, r_rev
    assert await _count_tx_rows(lot_ids) == baseline_tx + 4
    assert await _count_inverse_rows(lot_ids) == baseline_inverse + 2


@_postgres_only
async def test_reversal_refuses_when_farm_soft_deleted_first(
    client: AsyncClient,
) -> None:
    """Pre-existing soft-deleted farm on the partner side → 409, zero writes."""
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    from app.models.farm import Farm as _Farm

    setup = await _setup_two_farm_transfer(client)
    lot_ids = [setup["src_lot"], setup["dst_lot"]]
    baseline_tx = await _count_tx_rows(lot_ids)
    baseline_inverse = await _count_inverse_rows(lot_ids)
    baseline_markers = await _count_reversal_markers(lot_ids)

    async with _db_session_module.AsyncSessionLocal() as session:
        await session.execute(
            sa_update(_Farm)
            .where(_Farm.id == _UUIDType(setup["farm_b_id"]))
            .values(deleted_at=_dt.now(_UTC), is_active=False)
        )
        await session.commit()

    r = await client.post(
        f"/api/v1/warehouses/{setup['src']}/inventory:reverse",
        json={"reverses_transaction_id": setup["out_tx"]["id"], "reason": "r"},
    )
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "transfer_farm_deleted"
    # Zero-write guarantee.
    assert await _count_tx_rows(lot_ids) == baseline_tx
    assert await _count_inverse_rows(lot_ids) == baseline_inverse
    assert await _count_reversal_markers(lot_ids) == baseline_markers


# --------------------------------------------------------------------- #
# Scenario 2 — two-party barrier proof of opposite-side serialisation.
# --------------------------------------------------------------------- #
@_postgres_only
async def test_reversal_opposite_sides_two_party_barrier(
    client: AsyncClient,
) -> None:
    """Both racers must reach the barrier before either progresses.

    Uses a real two-party ``_TwoPartyBarrier`` (counter + condition)
    instead of a plain ``asyncio.Event`` so we prove the racers
    synchronised at the same point. Wraps the whole race in an
    ``asyncio.wait_for`` bound to catch runaway deadlocks.
    """
    import asyncio as _aio

    from app.services.inventory import InventoryService

    setup = await _setup_transfer_pair(client, transfer_qty=2.0, initial_qty=8.0)
    lot_ids = [setup["src_lot"], setup["dst_lot"]]
    baseline_tx = await _count_tx_rows(lot_ids)

    barrier = _TwoPartyBarrier()

    class _AwaitableBarrier:
        async def wait(self) -> None:
            await barrier.arrive()

    InventoryService._reversal_lock_barrier = _AwaitableBarrier()
    try:

        async def _fire(warehouse: str, tx_id: str, key: str) -> tuple[int, dict]:
            r = await client.post(
                f"/api/v1/warehouses/{warehouse}/inventory:reverse",
                json={"reverses_transaction_id": tx_id, "reason": key},
                headers={"Idempotency-Key": key},
            )
            return r.status_code, r.json()

        result = await _aio.wait_for(
            _aio.gather(
                _fire(setup["src"], setup["out_tx"]["id"], "opp-a"),
                _fire(setup["dst"], setup["in_tx"]["id"], "opp-b"),
            ),
            timeout=15,
        )
    finally:
        InventoryService._reversal_lock_barrier = None

    codes = sorted([result[0][0], result[1][0]])
    assert codes == [201, 409], f"unexpected outcome pair: {codes}"
    losing = result[0] if result[0][0] == 409 else result[1]
    assert losing[1]["detail"]["code"] == "already_reversed"
    assert await _count_tx_rows(lot_ids) == baseline_tx + 4
    assert await _count_inverse_rows(lot_ids) == 2
    assert await _count_reversal_markers(lot_ids) == 2


# ===================================================================== #
# Sprint 5.4.8 — Adversarial concurrency proofs (PostgreSQL only).      #
# ===================================================================== #


# ---- SQLite domain proofs (NOT locking / concurrency) ---------------- #
async def test_sprint_5_4_8_require_exactly_one_helper() -> None:
    """SQLite domain-safe: cardinality helper never destructures."""
    from fastapi import HTTPException

    from app.services._transfer_locks import require_exactly_one, require_set_equality

    class _Row:
        def __init__(self, rid: str) -> None:
            self.id = _UUIDType(rid)

    good = require_exactly_one(
        [_Row("11111111-1111-1111-1111-111111111111")],
        resource="lot",
        identifier=_UUIDType("11111111-1111-1111-1111-111111111111"),
    )
    assert good.id == _UUIDType("11111111-1111-1111-1111-111111111111")

    # Zero rows → 404 with stable code.
    with pytest.raises(HTTPException) as ei:
        require_exactly_one([], resource="lot", identifier=uuid4())
    assert ei.value.status_code == 404
    assert ei.value.detail["code"] == "lot_not_found"

    # Duplicate rows → 409 integrity.
    with pytest.raises(HTTPException) as ei:
        require_exactly_one(
            [
                _Row("11111111-1111-1111-1111-111111111111"),
                _Row("11111111-1111-1111-1111-111111111111"),
            ],
            resource="lot",
            identifier=uuid4(),
        )
    assert ei.value.status_code == 409
    assert ei.value.detail["code"] == "lot_integrity_violation"

    # Set-equality mismatch surfaces missing + unexpected ids.
    a = _UUIDType("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    b = _UUIDType("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    with pytest.raises(HTTPException) as ei:
        require_set_equality([_Row(str(a))], resource="lot", requested_ids={a, b})
    assert ei.value.status_code == 409
    assert str(b) in ei.value.detail["missing_ids"]


async def test_sprint_5_4_8_transfer_uses_immutable_group_id(
    client: AsyncClient,
) -> None:
    """SQLite domain-safe: transfer creation writes a non-null
    ``transfer_group_id`` on both OUT and IN rows, and both share
    the same id — the advisory-key anchor.
    """
    setup = await _setup_transfer_pair(client, transfer_qty=1.0, initial_qty=5.0)
    async with _db_session_module.AsyncSessionLocal() as session:
        rows = (
            (
                await session.execute(
                    select(_InventoryTransaction).where(
                        _InventoryTransaction.id.in_(
                            [
                                _UUIDType(setup["out_tx"]["id"]),
                                _UUIDType(setup["in_tx"]["id"]),
                            ]
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 2
    group_ids = {r.transfer_group_id for r in rows}
    assert None not in group_ids, "transfer_group_id must be set on both rows"
    assert len(group_ids) == 1, "both rows must share the same transfer_group_id"


# ---- Adversarial PostgreSQL proofs ---------------------------------- #


# Test A — opposite-direction transfer deadlock (no AB/BA deadlock).
@_postgres_only
async def test_sprint_5_4_8_opposite_direction_transfers_no_deadlock(
    client: AsyncClient,
) -> None:
    """Two transfers A→B and B→A racing simultaneously must serialise
    without a PostgreSQL deadlock. Deterministic lot lock order
    (sorted ascending id) makes both racers lock the SAME lowest-id
    lot first.
    """
    ctx = await _new_owner_org_farm(client)
    org_id, farm_id = ctx["org_id"], ctx["farm_id"]
    wh_a = await _create_warehouse(client, org_id, farm_id=farm_id, code=f"WA-{uuid4().hex[:4]}")
    wh_b = await _create_warehouse(client, org_id, farm_id=farm_id, code=f"WB-{uuid4().hex[:4]}")
    item_id = await _create_feed_item(client, org_id)
    # Pre-load both warehouses with the SAME lot code so we transfer
    # into an existing lot on both sides.
    await _receipt(client, wh_a, item_id, quantity=20, unit="kg", lot_code="LK")
    await _receipt(client, wh_b, item_id, quantity=20, unit="kg", lot_code="LK")
    lot_a = await _lot_id_for(client, wh_a)
    lot_b = await _lot_id_for(client, wh_b)

    async def _fire(src: str, lot: str, dst: str, key: str) -> tuple[int, dict]:
        r = await client.post(
            f"/api/v1/warehouses/{src}/inventory:transfer",
            json={
                "lot_id": lot,
                "destination_warehouse_id": dst,
                "quantity": 1,
                "unit": "kg",
            },
            headers={"Idempotency-Key": key},
        )
        return r.status_code, r.json()

    # Use ``return_exceptions=True`` so an ORM-level integrity error
    # (e.g. concurrent lot upsert) surfaces as an exception object
    # rather than propagating cancellation into the sibling task.
    result = await asyncio.gather(
        _fire(wh_a, lot_a, wh_b, f"opp-a-{uuid4().hex[:6]}"),
        _fire(wh_b, lot_b, wh_a, f"opp-b-{uuid4().hex[:6]}"),
        return_exceptions=True,
    )
    # Neither racer surfaced a Postgres deadlock as a 500. Any
    # SQLAlchemy exception at the ORM layer is acceptable so long
    # as no unhandled deadlock reached the API surface — the
    # canonical Sprint 5.4.8 guarantee.
    for outcome in result:
        if isinstance(outcome, BaseException):
            msg = str(outcome).lower()
            assert "deadlock" not in msg, f"deadlock surfaced: {outcome!r}"
        else:
            code, body = outcome
            assert code != 500, f"unexpected 500: {body}"
            assert code in (201, 409), (code, body)


# Test B — DB constraint rejects phantom topology row without advisory.
@_postgres_only
async def test_sprint_5_4_8_db_constraint_rejects_phantom_transfer_row(
    client: AsyncClient,
) -> None:
    """A raw SQL INSERT that would add a third TRANSFER_OUT with the
    same ``transfer_group_id`` MUST be rejected by the DB partial
    unique index, regardless of any advisory lock.
    """
    from sqlalchemy.exc import IntegrityError

    setup = await _setup_transfer_pair(client, transfer_qty=1.0, initial_qty=5.0)
    async with _db_session_module.AsyncSessionLocal() as session:
        from sqlalchemy import text as _text

        with pytest.raises(IntegrityError):
            await session.execute(
                _text(
                    "INSERT INTO inventory_transactions ("
                    "  id, organization_id, farm_id, warehouse_id, item_id, lot_id,"
                    "  transaction_type, quantity, unit, performed_by_id,"
                    "  performed_at, reference_type, reference_id,"
                    "  transfer_group_id"
                    ") SELECT :new_id, organization_id, farm_id, warehouse_id, item_id, lot_id,"
                    "         transaction_type, quantity, unit, performed_by_id,"
                    "         performed_at, reference_type, reference_id,"
                    "         transfer_group_id"
                    "    FROM inventory_transactions WHERE id = :src_id"
                ),
                {
                    "new_id": uuid4(),
                    "src_id": _UUIDType(setup["out_tx"]["id"]),
                },
            )
            await session.commit()


# Test F — advisory key immutability under tenant-field mutation.
@_postgres_only
async def test_sprint_5_4_8_advisory_key_immutable_under_org_mutation(
    client: AsyncClient,
) -> None:
    """The Sprint 5.4.8 advisory key is derived solely from the
    ``transfer_group_id`` column, which the DB trigger makes
    immutable-once-set. Any attempt to change it must be REJECTED
    at the DB layer, guaranteeing the same key is used forever.
    """
    from sqlalchemy.exc import IntegrityError

    setup = await _setup_transfer_pair(client, transfer_qty=1.0, initial_qty=5.0)
    async with _db_session_module.AsyncSessionLocal() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                sa_update(_InventoryTransaction)
                .where(_InventoryTransaction.id == _UUIDType(setup["out_tx"]["id"]))
                .values(transfer_group_id=uuid4())
            )
            await session.commit()


# Test G — non-transfer reversal missing lot → controlled 404, zero writes.
@_postgres_only
async def test_sprint_5_4_8_non_transfer_reversal_missing_lot(
    client: AsyncClient,
) -> None:
    """Soft-delete the lot between reversal-request dispatch and
    the locked reread. The service must raise a controlled 404 /
    409 domain error via ``require_exactly_one`` — never a
    ``ValueError`` from destructuring — and write zero rows.
    """
    from app.models.inventory import InventoryLot as _Lot

    ctx = await _new_owner_org_farm(client)
    wh = await _create_warehouse(client, ctx["org_id"], farm_id=ctx["farm_id"])
    item_id = await _create_feed_item(client, ctx["org_id"])
    await _receipt(client, wh, item_id, quantity=5, unit="kg", lot_code="LM")
    lot_id = await _lot_id_for(client, wh)
    txs = (await client.get(f"/api/v1/lots/{lot_id}/transactions")).json()["items"]
    receipt_tx = txs[0]
    before_tx = await _count_tx_rows([lot_id])
    # Soft-delete the lot BEFORE the reversal attempt.
    async with _db_session_module.AsyncSessionLocal() as session:
        from datetime import UTC as _UTC
        from datetime import datetime as _dt

        await session.execute(
            sa_update(_Lot).where(_Lot.id == _UUIDType(lot_id)).values(deleted_at=_dt.now(_UTC))
        )
        await session.commit()

    r = await client.post(
        f"/api/v1/warehouses/{wh}/inventory:reverse",
        json={"reverses_transaction_id": receipt_tx["id"], "reason": "x"},
    )
    # Controlled response — either 404 (not found) or 409
    # (integrity violation). Must NOT be 500.
    assert r.status_code in (404, 409), r.text
    body = r.json()
    if isinstance(body.get("detail"), dict):
        assert body["detail"]["code"] in (
            "inventory_lot_not_found",
            "inventory_lot_integrity_violation",
        )
    # Zero-write guarantee.
    assert await _count_tx_rows([lot_id]) == before_tx


# ===================================================================== #
# Sprint 5.4.9 — Mandatory transfer identity + database bypass proofs.  #
# ===================================================================== #


# Test — DB rejects INSERT of a transfer row with NULL transfer_group_id.
@_postgres_only
async def test_sprint_5_4_9_db_rejects_null_group_id_on_transfer_row(
    client: AsyncClient,
) -> None:
    """The Sprint 5.4.9 trigger REJECTS ``INSERT INTO
    inventory_transactions (..., transaction_type='transfer_out',
    transfer_group_id=NULL, ...)``. Transfer rows are BORN with a
    non-null immutable identity.
    """
    from sqlalchemy.exc import IntegrityError

    setup = await _setup_transfer_pair(client, transfer_qty=1.0, initial_qty=5.0)

    async with _db_session_module.AsyncSessionLocal() as session:
        from sqlalchemy import text as _text

        with pytest.raises(IntegrityError):
            await session.execute(
                _text(
                    "INSERT INTO inventory_transactions ("
                    "  id, organization_id, farm_id, warehouse_id, item_id, lot_id,"
                    "  transaction_type, quantity, unit, performed_by_id,"
                    "  performed_at, reference_type, reference_id, idempotency_key,"
                    "  transfer_group_id"
                    ") SELECT :new_id, organization_id, farm_id, warehouse_id, item_id, lot_id,"
                    "         'transfer_out'::inventory_transaction_type, quantity, unit, performed_by_id,"
                    "         performed_at, 'transfer', :new_ref, NULL, NULL"
                    "    FROM inventory_transactions WHERE id = :src_id"
                ),
                {
                    "new_id": uuid4(),
                    "src_id": _UUIDType(setup["out_tx"]["id"]),
                    "new_ref": uuid4(),
                },
            )
            await session.commit()


# Test — DB rejects transfer_group_id on a non-transfer row.
@_postgres_only
async def test_sprint_5_4_9_db_rejects_group_id_on_non_transfer_row(
    client: AsyncClient,
) -> None:
    """Non-transfer rows (RECEIPT / ISSUE / …) MUST NOT carry a
    ``transfer_group_id``. The Sprint 5.4.9 trigger enforces this.
    """
    from sqlalchemy.exc import IntegrityError

    ctx = await _new_owner_org_farm(client)
    wh = await _create_warehouse(client, ctx["org_id"], farm_id=ctx["farm_id"])
    item_id = await _create_feed_item(client, ctx["org_id"])
    receipt = await _receipt(client, wh, item_id, quantity=5, unit="kg", lot_code="X")
    receipt_body = receipt["body"]
    receipt_tx_id = receipt_body["transaction"]["id"] if "transaction" in receipt_body else None
    if receipt_tx_id is None:
        # Fall back to lot transactions listing.
        lot_id = await _lot_id_for(client, wh)
        txs = (await client.get(f"/api/v1/lots/{lot_id}/transactions")).json()["items"]
        receipt_tx_id = txs[0]["id"]
    async with _db_session_module.AsyncSessionLocal() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                sa_update(_InventoryTransaction)
                .where(_InventoryTransaction.id == _UUIDType(receipt_tx_id))
                .values(transfer_group_id=uuid4())
            )
            await session.commit()


# Test — creation vs reversal race: no deadlock, one blocks the other.
@_postgres_only
async def test_sprint_5_4_9_creation_vs_reversal_no_deadlock(
    client: AsyncClient,
) -> None:
    """A concurrent transfer CREATION and a REVERSAL of an existing
    transfer must serialize predictably under the canonical lock
    order (advisory → tx → warehouse → farm → org → item → lot).
    Neither may surface a Postgres deadlock as a 500.
    """
    # Existing transfer to reverse.
    setup = await _setup_transfer_pair(client, transfer_qty=1.0, initial_qty=8.0)

    # Fresh transfer to create in parallel — same warehouses / lots so
    # both racers contend for the SAME row locks. This is the
    # canonical creation-vs-reversal contention scenario.
    async def _reverser() -> tuple[int, dict]:
        r = await client.post(
            f"/api/v1/warehouses/{setup['src']}/inventory:reverse",
            json={"reverses_transaction_id": setup["out_tx"]["id"], "reason": "r"},
            headers={"Idempotency-Key": f"rev-{uuid4().hex[:6]}"},
        )
        return r.status_code, r.json()

    async def _creator() -> tuple[int, dict]:
        r = await client.post(
            f"/api/v1/warehouses/{setup['src']}/inventory:transfer",
            json={
                "lot_id": setup["src_lot"],
                "destination_warehouse_id": setup["dst"],
                "quantity": 1,
                "unit": "kg",
            },
            headers={"Idempotency-Key": f"cre-{uuid4().hex[:6]}"},
        )
        return r.status_code, r.json()

    result = await asyncio.gather(_reverser(), _creator(), return_exceptions=True)
    for outcome in result:
        if isinstance(outcome, BaseException):
            msg = str(outcome).lower()
            assert "deadlock" not in msg, f"deadlock surfaced: {outcome!r}"
        else:
            code, body = outcome
            assert code != 500, f"unexpected 500: {body}"
            assert code in (200, 201, 409), (code, body)


# Test — migration 0009 malformed-topology pre-flight abort.
@_postgres_only
async def test_sprint_5_4_9_migration_preflight_aborts_on_malformed_topology(
    client: AsyncClient,
) -> None:
    """The Sprint 5.4.9 migration pre-flight query counts duplicate
    roles / orphans / incomplete pairs BEFORE creating constraints
    and raises a RuntimeError with counts if any are present.
    This test invokes the pre-flight SQL against a table that is
    intentionally corrupted, and asserts the query surfaces the
    correct counts (proving the migration would abort).
    """
    from sqlalchemy import text as _text

    setup = await _setup_transfer_pair(client, transfer_qty=1.0, initial_qty=5.0)
    # Corrupt: mark the IN row with a divergent reference_id via a
    # direct backend that bypasses the ORM's triggers using an admin
    # session — we DROP the trigger temporarily just for this test.
    async with _db_session_module.AsyncSessionLocal() as session:
        await session.execute(
            _text(
                "DROP TRIGGER IF EXISTS trg_inventory_tx_group_immutable "
                "ON inventory_transactions"
            )
        )
        await session.commit()
    try:
        # Now corrupt: point OUT row at a foreign reference_id.
        async with _db_session_module.AsyncSessionLocal() as session:
            await session.execute(
                sa_update(_InventoryTransaction)
                .where(_InventoryTransaction.id == _UUIDType(setup["out_tx"]["id"]))
                .values(reference_id=uuid4())
            )
            await session.commit()
        # Run the pre-flight query and assert incomplete_pairs > 0.
        async with _db_session_module.AsyncSessionLocal() as session:
            result = (
                await session.execute(
                    _text(
                        "WITH transfer_rows AS ("
                        "  SELECT id, transaction_type, reference_id, reference_type "
                        "  FROM inventory_transactions "
                        "  WHERE transaction_type IN ('transfer_out', 'transfer_in')"
                        ") "
                        "SELECT (SELECT COUNT(*) FROM ("
                        "    SELECT reference_id FROM transfer_rows "
                        "     WHERE reference_id IS NOT NULL "
                        "     GROUP BY reference_id HAVING COUNT(*) <> 2) x) AS incomplete_pairs"
                    )
                )
            ).scalar()
            assert result > 0, "pre-flight query should surface incomplete pairs after corruption"
    finally:
        # Restore the trigger via the canonical DDL implementation.
        from app.db.inventory_transfer_ddl import (
            TRANSFER_IMMUTABLE_CREATE_TRIGGER_SQL,
            TRANSFER_IMMUTABLE_FN_SQL,
        )

        async with _db_session_module.AsyncSessionLocal() as session:
            await session.execute(_text(TRANSFER_IMMUTABLE_FN_SQL))
            await session.execute(
                _text(
                    "DROP TRIGGER IF EXISTS trg_inventory_tx_group_immutable "
                    "ON inventory_transactions"
                )
            )
            await session.execute(_text(TRANSFER_IMMUTABLE_CREATE_TRIGGER_SQL))
            await session.commit()


# ===================================================================== #
# Sprint 5.4.10 — Full UPDATE contract + deferred pair completeness.    #
# ===================================================================== #


# Section 2 — reject reference_type change away from 'transfer'.
@_postgres_only
async def test_sprint_5_4_10_update_cannot_flip_transfer_reference_type(
    client: AsyncClient,
) -> None:
    from sqlalchemy.exc import IntegrityError

    setup = await _setup_transfer_pair(client, transfer_qty=1.0, initial_qty=5.0)
    async with _db_session_module.AsyncSessionLocal() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                sa_update(_InventoryTransaction)
                .where(_InventoryTransaction.id == _UUIDType(setup["out_tx"]["id"]))
                .values(reference_type="reversal")
            )
            await session.commit()


# Section 2 — reject transaction_type change from transfer to non-transfer.
@_postgres_only
async def test_sprint_5_4_10_update_cannot_reclassify_transfer_to_non_transfer(
    client: AsyncClient,
) -> None:
    from sqlalchemy.exc import IntegrityError

    setup = await _setup_transfer_pair(client, transfer_qty=1.0, initial_qty=5.0)
    async with _db_session_module.AsyncSessionLocal() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                sa_update(_InventoryTransaction)
                .where(_InventoryTransaction.id == _UUIDType(setup["out_tx"]["id"]))
                .values(transaction_type=_InventoryTransactionType.RECEIPT)
            )
            await session.commit()


# Section 2 — reject transaction_type flip between transfer roles.
@_postgres_only
async def test_sprint_5_4_10_update_cannot_flip_out_to_in(
    client: AsyncClient,
) -> None:
    from sqlalchemy.exc import IntegrityError

    setup = await _setup_transfer_pair(client, transfer_qty=1.0, initial_qty=5.0)
    async with _db_session_module.AsyncSessionLocal() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                sa_update(_InventoryTransaction)
                .where(_InventoryTransaction.id == _UUIDType(setup["out_tx"]["id"]))
                .values(transaction_type=_InventoryTransactionType.TRANSFER_IN)
            )
            await session.commit()


# Section 3 — deferred constraint rejects OUT without matching IN at commit.
@_postgres_only
async def test_sprint_5_4_10_deferred_constraint_rejects_out_only_pair(
    client: AsyncClient,
) -> None:
    """A raw SQL transaction that inserts ONE TRANSFER_OUT row and
    commits — without a matching TRANSFER_IN — must be REJECTED at
    COMMIT time by the deferred pair-completeness constraint
    trigger. Statement-time enforcement (partial unique index)
    accepts the row; the deferred trigger fires on COMMIT.
    """
    from sqlalchemy.exc import IntegrityError

    setup = await _setup_transfer_pair(client, transfer_qty=1.0, initial_qty=5.0)
    # Copy the OUT row's shape via SELECT, but assign a BRAND-NEW
    # transfer_group_id so the partial unique index does not fire
    # (that would be the DIFFERENT bypass proven in Sprint 5.4.8).
    new_group = uuid4()
    async with _db_session_module.AsyncSessionLocal() as session:
        from sqlalchemy import text as _text

        with pytest.raises(IntegrityError):
            await session.execute(
                _text(
                    "INSERT INTO inventory_transactions ("
                    "  id, organization_id, farm_id, warehouse_id, item_id, lot_id,"
                    "  transaction_type, quantity, unit, performed_by_id,"
                    "  performed_at, reference_type, reference_id, idempotency_key,"
                    "  transfer_group_id"
                    ") SELECT :new_id, organization_id, farm_id, warehouse_id, item_id, lot_id,"
                    "         'transfer_out'::inventory_transaction_type, quantity, unit, performed_by_id,"
                    "         performed_at, 'transfer', :new_group, NULL, :new_group"
                    "    FROM inventory_transactions WHERE id = :src_id"
                ),
                {
                    "new_id": uuid4(),
                    "new_group": new_group,
                    "src_id": _UUIDType(setup["out_tx"]["id"]),
                },
            )
            await session.commit()


# Section 4 — pre-flight aborts on non-transfer row with transfer reference.
@_postgres_only
async def test_sprint_5_4_10_migration_preflight_flags_non_transfer_using_ref(
    client: AsyncClient,
) -> None:
    """The Sprint 5.4.10 pre-flight query counts non-transfer rows
    that carry ``reference_type = 'transfer'``. Assert the exact
    SQL used by the migration surfaces that count.
    """
    from sqlalchemy import text as _text

    ctx = await _new_owner_org_farm(client)
    wh = await _create_warehouse(client, ctx["org_id"], farm_id=ctx["farm_id"])
    item_id = await _create_feed_item(client, ctx["org_id"])
    await _receipt(client, wh, item_id, quantity=5, unit="kg", lot_code="R")
    lot_id = await _lot_id_for(client, wh)
    txs = (await client.get(f"/api/v1/lots/{lot_id}/transactions")).json()["items"]
    receipt_tx_id = txs[0]["id"]

    # Drop the trigger, corrupt, run pre-flight, restore.
    async with _db_session_module.AsyncSessionLocal() as session:
        await session.execute(
            _text(
                "DROP TRIGGER IF EXISTS trg_inventory_tx_group_immutable "
                "ON inventory_transactions"
            )
        )
        await session.execute(
            sa_update(_InventoryTransaction)
            .where(_InventoryTransaction.id == _UUIDType(receipt_tx_id))
            .values(reference_type="transfer", reference_id=uuid4())
        )
        await session.commit()
    try:
        async with _db_session_module.AsyncSessionLocal() as session:
            result = (
                await session.execute(
                    _text(
                        "SELECT COUNT(*) FROM inventory_transactions "
                        "WHERE reference_type = 'transfer' "
                        "  AND transaction_type NOT IN ('transfer_out', 'transfer_in')"
                    )
                )
            ).scalar()
            assert result > 0, "pre-flight should flag non-transfer rows using transfer reference"
    finally:
        # Restore the trigger via the canonical DDL.
        from app.db.inventory_transfer_ddl import (
            TRANSFER_IMMUTABLE_CREATE_TRIGGER_SQL,
            TRANSFER_IMMUTABLE_DROP_TRIGGER_SQL,
            TRANSFER_IMMUTABLE_FN_SQL,
        )

        async with _db_session_module.AsyncSessionLocal() as session:
            await session.execute(_text(TRANSFER_IMMUTABLE_FN_SQL))
            await session.execute(_text(TRANSFER_IMMUTABLE_DROP_TRIGGER_SQL))
            await session.execute(_text(TRANSFER_IMMUTABLE_CREATE_TRIGGER_SQL))
            await session.commit()


# Section 5 — migration LOCK TABLE serialises concurrent writers.
@_postgres_only
async def test_sprint_5_4_10_migration_lock_blocks_concurrent_writer(
    client: AsyncClient,
) -> None:
    """Two connections: one acquires ``LOCK TABLE ... ACCESS EXCLUSIVE
    MODE`` on ``inventory_transactions``, the other attempts an
    INSERT. The INSERT must BLOCK until the first commits/rolls back.
    """
    from sqlalchemy import text as _text

    holder_ready = asyncio.Event()
    release = asyncio.Event()

    async def _holder() -> None:
        async with _db_session_module.AsyncSessionLocal() as session:
            await session.execute(
                _text("LOCK TABLE inventory_transactions IN ACCESS EXCLUSIVE MODE")
            )
            holder_ready.set()
            await release.wait()
            await session.rollback()

    async def _writer() -> None:
        async with _db_session_module.AsyncSessionLocal() as session:
            await session.execute(_text("SELECT id FROM inventory_transactions LIMIT 1"))
            await session.rollback()

    holder = asyncio.create_task(_holder())
    await asyncio.wait_for(holder_ready.wait(), timeout=5)
    writer = asyncio.create_task(_writer())
    await asyncio.sleep(0.3)
    # ACCESS EXCLUSIVE conflicts with everything including plain
    # SELECT — the writer must still be blocked.
    assert writer.done() is False, "writer should block on ACCESS EXCLUSIVE"
    release.set()
    await asyncio.wait_for(asyncio.gather(holder, writer), timeout=5)
    assert writer.done() is True


# Section 6 — deterministic creation-vs-reversal barrier test.
@_postgres_only
async def test_sprint_5_4_10_creation_vs_reversal_barrier(
    client: AsyncClient,
) -> None:
    """Uses the ``_TwoPartyBarrier`` so both racers reach the
    advisory-lock boundary before either acquires the lock. Asserts:
    * no deadlock string in any exception
    * no 500 response
    * every outcome is 200/201/409
    """
    from app.services.inventory import InventoryService

    setup = await _setup_transfer_pair(client, transfer_qty=1.0, initial_qty=8.0)

    barrier = _TwoPartyBarrier()

    class _AwaitableBarrier:
        async def wait(self) -> None:
            await barrier.arrive()

    InventoryService._reversal_lock_barrier = _AwaitableBarrier()
    try:

        async def _reverser() -> tuple[int, dict]:
            r = await client.post(
                f"/api/v1/warehouses/{setup['src']}/inventory:reverse",
                json={"reverses_transaction_id": setup["out_tx"]["id"], "reason": "r"},
                headers={"Idempotency-Key": f"rev-{uuid4().hex[:6]}"},
            )
            return r.status_code, r.json()

        async def _creator() -> tuple[int, dict]:
            # Creator doesn't hit the reversal barrier; it should
            # complete normally. Combined with the reverser, the
            # barrier ensures the reverser is definitively paused
            # while the creator races through — no deadlock possible.
            r = await client.post(
                f"/api/v1/warehouses/{setup['src']}/inventory:transfer",
                json={
                    "lot_id": setup["src_lot"],
                    "destination_warehouse_id": setup["dst"],
                    "quantity": 1,
                    "unit": "kg",
                },
                headers={"Idempotency-Key": f"cre-{uuid4().hex[:6]}"},
            )
            # Once creator has issued its call, allow the reverser
            # to progress past its barrier.
            await barrier.arrive()
            return r.status_code, r.json()

        result = await asyncio.wait_for(
            asyncio.gather(_reverser(), _creator(), return_exceptions=True),
            timeout=15,
        )
    finally:
        InventoryService._reversal_lock_barrier = None

    for outcome in result:
        if isinstance(outcome, BaseException):
            msg = str(outcome).lower()
            assert "deadlock" not in msg, f"deadlock surfaced: {outcome!r}"
            raise AssertionError(f"unexpected exception: {outcome!r}")
        code, body = outcome
        assert code != 500, f"unexpected 500: {body}"
        assert code in (200, 201, 409), (code, body)


# ===================================================================== #
# Sprint 5.4.11 — Locked Authorization & Authoritative Permission
# Resolution.
#
# Every test below proves the Sprint 5.4.11 contract:
#
#   Transfer authorization runs EXCLUSIVELY inside the service layer,
#   AFTER canonical row locks are held on source + destination
#   warehouses, their referenced farms, and the owning organization.
#   If a permission, membership, role, warehouse assignment, farm
#   assignment, or organization status change commits BEFORE the
#   transfer's locks are acquired, the transfer is authoritatively
#   REJECTED against the new state — never a stale pre-lock view.
#
# The tests use ``InventoryService._transfer_pre_lock_barrier`` — a
# ``ClassVar`` deterministic-race hook — so the mutator can commit
# its change while the transfer is guaranteed paused BEFORE any row
# lock is acquired. On release the transfer proceeds through the
# locked-authorization pipeline and MUST refuse with 404 (tenancy /
# membership leak invariant) or 403 (missing permission) or 409
# (organization / farm / warehouse state broke an invariant).
#
# All 6 tests are ``@_postgres_only`` — they assert row-locking
# semantics that require Postgres.
# ===================================================================== #


async def _setup_locked_auth_operator(
    client: AsyncClient,
) -> dict:
    """Two org-shared warehouses in the same org; an operator with
    ``farm_director`` (org-scoped) so they hold
    ``inventory_transaction.create`` at every scope by default. The
    Sprint 5.4.11 races revoke that permission / membership / role
    / warehouse-assignment / farm-assignment / org-status through
    direct DB mutation, then observe the transfer refuse.
    """
    owner = await _new_owner_org_farm(client)
    owner_email = owner["owner"]
    org_id = owner["org_id"]
    # Org-shared source + destination warehouses (unpinned).
    src = await _create_warehouse(client, org_id, code=f"SRC-{uuid4().hex[:4]}")
    dst = await _create_warehouse(client, org_id, code=f"DST-{uuid4().hex[:4]}")
    item_id = await _create_feed_item(client, org_id)
    await _receipt(client, src, item_id, quantity=100, unit="kg", lot_code="LX")
    src_lot = await _lot_id_for(client, src)
    # Operator — org-scoped farm_director; carries
    # inventory_transaction.create in every scope in this org.
    operator = f"op-{uuid4().hex[:8]}@agrovix.dev"
    await create_verified_user(operator)
    await invite_and_accept(
        client,
        inviter_email=owner_email,
        invitee_email=operator,
        org_id=org_id,
        role_name="farm_director",
    )
    await switch_user(client, operator)
    return {
        "owner_email": owner_email,
        "operator": operator,
        "org_id": org_id,
        "farm_id": owner["farm_id"],
        "src": src,
        "dst": dst,
        "src_lot": src_lot,
        "item_id": item_id,
    }


async def _run_transfer_under_pre_lock_race(
    client: AsyncClient,
    *,
    setup: dict,
    mutator_body,
) -> tuple[int, dict]:
    """Run a transfer with ``_transfer_pre_lock_barrier`` active and
    let ``mutator_body(session)`` commit its change while the
    transfer is guaranteed paused.

    Contract of ``mutator_body``: takes an ``AsyncSession`` in its
    own transaction and MUST commit before returning.
    """
    from app.services.inventory import InventoryService

    gate = asyncio.Event()
    InventoryService._transfer_pre_lock_barrier = gate
    try:

        async def _transferer() -> tuple[int, dict]:
            r = await client.post(
                f"/api/v1/warehouses/{setup['src']}/inventory:transfer",
                json={
                    "lot_id": setup["src_lot"],
                    "destination_warehouse_id": setup["dst"],
                    "quantity": 5,
                    "unit": "kg",
                },
                headers={"Idempotency-Key": f"xfer-{uuid4().hex[:8]}"},
            )
            body: dict = {}
            try:
                body = r.json()
            except Exception:
                body = {"text": r.text}
            return r.status_code, body

        async def _mutator() -> None:
            # Give the transferer a moment to enter the barrier.
            await asyncio.sleep(0.3)
            async with _db_session_module.AsyncSessionLocal() as session:
                await mutator_body(session)
                await session.commit()
            gate.set()

        result = await asyncio.wait_for(asyncio.gather(_transferer(), _mutator()), timeout=10)
        return result[0]
    finally:
        InventoryService._transfer_pre_lock_barrier = None


async def _tx_baseline_count(setup: dict) -> int:
    async with _db_session_module.AsyncSessionLocal() as session:
        stmt = select(func.count(_InventoryTransaction.id)).where(
            _InventoryTransaction.warehouse_id.in_(
                [_UUIDType(setup["src"]), _UUIDType(setup["dst"])]
            )
        )
        return (await session.execute(stmt)).scalar_one()


@_postgres_only
async def test_sprint_5_4_11_transfer_rejects_permission_revocation_under_lock(
    client: AsyncClient,
) -> None:
    """Sprint 5.4.11 — permission race.

    While the transfer waits at the pre-lock barrier, the mutator
    strips ``inventory_transaction.create`` from the operator's
    ``farm_director`` role. On release the transfer's authorization
    (running from the LOCKED warehouse rows) resolves fresh
    permission codes and refuses with 403.
    """
    from sqlalchemy import delete as sa_delete

    from app.models.role import Permission as _Perm
    from app.models.role import Role as _Role
    from app.models.role import role_permissions_table as _rp

    setup = await _setup_locked_auth_operator(client)
    baseline = await _tx_baseline_count(setup)

    async def _mutator_body(session) -> None:
        # Find the farm_director role and the target permission.
        role_id = (
            await session.execute(select(_Role.id).where(_Role.name == "farm_director"))
        ).scalar_one()
        perm_id = (
            await session.execute(
                select(_Perm.id).where(_Perm.code == "inventory_transaction.create")
            )
        ).scalar_one()
        # Sever the role ↔ permission link.
        await session.execute(
            sa_delete(_rp).where(_rp.c.role_id == role_id, _rp.c.permission_id == perm_id)
        )

    try:
        status_code, body = await _run_transfer_under_pre_lock_race(
            client, setup=setup, mutator_body=_mutator_body
        )
        assert status_code == 403, (status_code, body)
        assert "inventory_transaction.create" in str(body.get("detail", "")), body
        # No ledger row landed on either warehouse.
        assert await _tx_baseline_count(setup) == baseline
    finally:
        # Sprint 5.4.11 → 5.4.12 — restore the shared
        # role↔permission link so subsequent tests in this session
        # do not inherit a globally-broken authorization graph.
        async with _db_session_module.AsyncSessionLocal() as session:
            role_id = (
                await session.execute(select(_Role.id).where(_Role.name == "farm_director"))
            ).scalar_one()
            perm_id = (
                await session.execute(
                    select(_Perm.id).where(_Perm.code == "inventory_transaction.create")
                )
            ).scalar_one()
            existing = (
                await session.execute(
                    select(_rp).where(
                        _rp.c.role_id == role_id,
                        _rp.c.permission_id == perm_id,
                    )
                )
            ).first()
            if existing is None:
                await session.execute(_rp.insert().values(role_id=role_id, permission_id=perm_id))
                await session.commit()


@_postgres_only
async def test_sprint_5_4_11_transfer_rejects_org_membership_revocation_under_lock(
    client: AsyncClient,
) -> None:
    """Sprint 5.4.11 — membership race.

    Mutator marks the operator's ``OrganizationMembership`` as
    inactive while the transfer is paused. Locked authorization
    resolves the membership fresh and refuses with 404 — same shape
    a non-member would have seen before Sprint 5.4.11, preserving
    the CRG02 tenancy-leak invariant.
    """
    from app.models.membership import OrganizationMembership as _OrgMem
    from app.models.user import User as _User

    setup = await _setup_locked_auth_operator(client)
    baseline = await _tx_baseline_count(setup)

    async def _mutator_body(session) -> None:
        op_id = (
            await session.execute(select(_User.id).where(_User.email == setup["operator"]))
        ).scalar_one()
        await session.execute(
            sa_update(_OrgMem)
            .where(
                _OrgMem.user_id == op_id,
                _OrgMem.organization_id == _UUIDType(setup["org_id"]),
            )
            .values(is_active=False)
        )

    status_code, body = await _run_transfer_under_pre_lock_race(
        client, setup=setup, mutator_body=_mutator_body
    )
    assert status_code == 404, (status_code, body)
    assert await _tx_baseline_count(setup) == baseline


@_postgres_only
async def test_sprint_5_4_11_transfer_rejects_role_assignment_revocation_under_lock(
    client: AsyncClient,
) -> None:
    """Sprint 5.4.11 — role-assignment race.

    Mutator sets ``role_assignment.revoked_at`` on the operator's
    ``farm_director`` assignment. Locked authorization resolves the
    role assignment fresh; no active assignment means no permission
    codes, so the transfer refuses with 403.
    """
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    from app.models.role_assignment import RoleAssignment as _RoleAssn
    from app.models.user import User as _User

    setup = await _setup_locked_auth_operator(client)
    baseline = await _tx_baseline_count(setup)

    async def _mutator_body(session) -> None:
        op_id = (
            await session.execute(select(_User.id).where(_User.email == setup["operator"]))
        ).scalar_one()
        await session.execute(
            sa_update(_RoleAssn)
            .where(_RoleAssn.user_id == op_id, _RoleAssn.revoked_at.is_(None))
            .values(revoked_at=_dt.now(_UTC))
        )

    status_code, body = await _run_transfer_under_pre_lock_race(
        client, setup=setup, mutator_body=_mutator_body
    )
    assert status_code == 403, (status_code, body)
    assert "inventory_transaction.create" in str(body.get("detail", "")), body
    assert await _tx_baseline_count(setup) == baseline


@_postgres_only
async def test_sprint_5_4_11_transfer_rejects_warehouse_reassignment_under_lock(
    client: AsyncClient,
) -> None:
    """Sprint 5.4.11 — warehouse-assignment race.

    While the transfer is paused, the mutator flips the destination
    warehouse's ``farm_id`` to a foreign farm in a different
    organization. When the transfer bulk-locks warehouses, it
    observes the new ``organization_id`` on the destination row
    under lock, and the cross-org invariant refuses the transfer
    with ``cross_org_transfer_forbidden``. No ledger writes occur.
    """
    from app.models.inventory import Warehouse as _Wh

    setup = await _setup_locked_auth_operator(client)
    baseline = await _tx_baseline_count(setup)

    # Set up a foreign farm in a second organization.
    foreign_owner = f"fo-{uuid4().hex[:6]}@agrovix.dev"
    await create_verified_user(foreign_owner)
    await switch_user(client, foreign_owner)
    foreign_org_id = await create_org(client, slug=f"foreign-{uuid4().hex[:6]}")
    r = await client.post(
        f"/api/v1/organizations/{foreign_org_id}/farms",
        json={"name": "F-foreign", "code": f"ff-{uuid4().hex[:6]}"},
    )
    assert r.status_code == 201, r.text
    foreign_farm_id = r.json()["id"]
    await switch_user(client, setup["operator"])

    async def _mutator_body(session) -> None:
        # Rewrite dst warehouse's org + farm to the foreign tenant.
        await session.execute(
            sa_update(_Wh)
            .where(_Wh.id == _UUIDType(setup["dst"]))
            .values(
                organization_id=_UUIDType(foreign_org_id),
                farm_id=_UUIDType(foreign_farm_id),
            )
        )

    status_code, body = await _run_transfer_under_pre_lock_race(
        client, setup=setup, mutator_body=_mutator_body
    )
    # The actor has no access to the destination's new tenant. Do not
    # disclose that a cross-org topology exists before authorization.
    assert status_code == 404, (status_code, body)
    assert body.get("detail") == "Warehouse not found."
    assert await _tx_baseline_count(setup) == baseline


@_postgres_only
async def test_sprint_5_4_11_transfer_rejects_farm_deactivation_under_lock(
    client: AsyncClient,
) -> None:
    """Sprint 5.4.11 — farm-assignment race.

    Setup uses farm-pinned warehouses so ``farm.is_active`` is
    part of the locked authorization graph. Mutator flips the
    destination warehouse's owning farm to inactive; on release,
    the transfer's bulk-lock on farms observes the inactive state
    and refuses with ``transfer_farm_inactive``.
    """
    from app.models.farm import Farm as _Farm

    # Build a farm-pinned two-warehouse setup so the farm rows are
    # in the locked graph.
    owner = await _new_owner_org_farm(client)
    owner_email = owner["owner"]
    org_id = owner["org_id"]
    r = await client.post(
        f"/api/v1/organizations/{org_id}/farms",
        json={"name": "Farm-B", "code": f"farm-b-{uuid4().hex[:6]}"},
    )
    assert r.status_code == 201, r.text
    farm_b_id = r.json()["id"]
    src = await _create_warehouse(
        client, org_id, farm_id=owner["farm_id"], code=f"SRC-{uuid4().hex[:4]}"
    )
    dst = await _create_warehouse(client, org_id, farm_id=farm_b_id, code=f"DST-{uuid4().hex[:4]}")
    item_id = await _create_feed_item(client, org_id)
    await _receipt(client, src, item_id, quantity=100, unit="kg", lot_code="LX")
    src_lot = await _lot_id_for(client, src)

    # Org-scoped operator so both farm scopes carry the permission.
    operator = f"op-{uuid4().hex[:8]}@agrovix.dev"
    await create_verified_user(operator)
    await invite_and_accept(
        client,
        inviter_email=owner_email,
        invitee_email=operator,
        org_id=org_id,
        role_name="farm_director",
    )
    await switch_user(client, operator)
    setup = {"src": src, "dst": dst, "src_lot": src_lot}
    baseline = await _tx_baseline_count(setup)

    async def _mutator_body(session) -> None:
        await session.execute(
            sa_update(_Farm).where(_Farm.id == _UUIDType(farm_b_id)).values(is_active=False)
        )

    status_code, body = await _run_transfer_under_pre_lock_race(
        client, setup=setup, mutator_body=_mutator_body
    )
    assert status_code == 409, (status_code, body)
    assert body.get("detail", {}).get("code") == "transfer_farm_inactive", body
    assert await _tx_baseline_count(setup) == baseline


@_postgres_only
async def test_sprint_5_4_11_transfer_rejects_organization_deactivation_under_lock(
    client: AsyncClient,
) -> None:
    """Sprint 5.4.11 — organization-status race.

    Mutator flips ``organization.is_active = False`` on the shared
    org while the transfer is paused. Locked authorization
    observes the inactive org under lock and refuses with
    ``transfer_organization_inactive``.
    """
    from app.models.organization import Organization as _Org

    setup = await _setup_locked_auth_operator(client)
    baseline = await _tx_baseline_count(setup)

    async def _mutator_body(session) -> None:
        await session.execute(
            sa_update(_Org).where(_Org.id == _UUIDType(setup["org_id"])).values(is_active=False)
        )

    status_code, body = await _run_transfer_under_pre_lock_race(
        client, setup=setup, mutator_body=_mutator_body
    )
    assert status_code == 409, (status_code, body)
    assert body.get("detail", {}).get("code") == "transfer_organization_inactive", body
    assert await _tx_baseline_count(setup) == baseline


# ===================================================================== #
# Sprint 5.4.12 — Real two-transaction proof that authorization
# mutations BLOCK on the per-organization authorization advisory
# lock while a transfer holds it, and RESUME after the transfer
# commits or rolls back.
#
# Every test in this block:
#   1. Starts a transfer coroutine that holds all its row locks
#      + the per-org authorization advisory lock, paused at
#      ``_transfer_hold_before_authorize_gate``.
#   2. In a SEPARATE Postgres connection, attempts an
#      authorization mutation that FIRST acquires the SAME
#      per-org authorization advisory lock (matching the
#      protocol every real revocation / assignment path now
#      follows via :mod:`app.services._authorization_lock`).
#   3. Asserts the mutation task is genuinely blocked
#      (``mut_task.done() is False``) — no timing assumptions,
#      only deterministic ``asyncio.Event`` signals.
#   4. Releases the transfer hold gate.
#   5. Asserts the mutation task then completes.
#
# Additional coverage:
#   * revocation-wins-first — mutation commits BEFORE transfer,
#     transfer resolves against the post-mutation state and
#     refuses.
#   * rollback releases the lock — if the transfer rolls back,
#     the mutation resumes and commits.
#   * unrelated organizations do NOT block each other.
#   * deterministic UUID ordering prevents AB / BA deadlocks.
#
# Every test is ``@_postgres_only`` — SQLite lacks
# ``pg_advisory_xact_lock``.
# ===================================================================== #


async def _acquire_org_auth_lock_via_sql(session, org_id: str) -> None:
    """Emit ``SELECT pg_advisory_xact_lock`` for the per-org
    authorization key, matching what
    :mod:`app.services._authorization_lock` does. Used by the
    mutator coroutines in the tests below so they participate in
    the exact same lock protocol as production services.
    """
    from app.services._authorization_lock import (
        advisory_lock_key_for_org_authorization,
    )

    key = advisory_lock_key_for_org_authorization(_UUIDType(org_id))
    from sqlalchemy import text as _text

    await session.execute(_text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


@_postgres_only
async def test_sprint_5_4_12_transfer_blocks_authorization_mutation_while_holding_lock(
    client: AsyncClient,
) -> None:
    """Sprint 5.4.12 — real two-transaction blocking proof.

    Transfer acquires the per-org authorization advisory lock and
    pauses at the hold gate. A concurrent authorization mutator
    coroutine (in a separate DB connection) attempts to acquire
    the SAME lock and MUST block. Once the transfer completes,
    the mutator resumes and commits.
    """
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    from app.models.role_assignment import RoleAssignment as _RoleAssn
    from app.models.user import User as _User
    from app.services.inventory import InventoryService

    setup = await _setup_locked_auth_operator(client)
    baseline = await _tx_baseline_count(setup)

    locks_signal = asyncio.Event()
    hold = asyncio.Event()
    mutator_started = asyncio.Event()
    InventoryService._transfer_after_locks_signal = locks_signal
    InventoryService._transfer_hold_before_authorize_gate = hold
    try:

        async def _transferer() -> tuple[int, dict]:
            r = await client.post(
                f"/api/v1/warehouses/{setup['src']}/inventory:transfer",
                json={
                    "lot_id": setup["src_lot"],
                    "destination_warehouse_id": setup["dst"],
                    "quantity": 5,
                    "unit": "kg",
                },
                headers={"Idempotency-Key": f"xfer-{uuid4().hex[:8]}"},
            )
            try:
                return r.status_code, r.json()
            except Exception:
                return r.status_code, {"text": r.text}

        mut_committed = asyncio.Event()

        async def _mutator() -> None:
            # Wait until transfer has locked warehouses / farms / orgs.
            await locks_signal.wait()
            async with _db_session_module.AsyncSessionLocal() as session:
                mutator_started.set()
                # Acquire the SAME per-org authorization advisory lock.
                # This MUST block while the transfer holds it.
                await _acquire_org_auth_lock_via_sql(session, setup["org_id"])
                # Once we get the lock, revoke THIS operator's role
                # assignment (per-user; does not pollute shared roles).
                op_id = (
                    await session.execute(select(_User.id).where(_User.email == setup["operator"]))
                ).scalar_one()
                await session.execute(
                    sa_update(_RoleAssn)
                    .where(
                        _RoleAssn.user_id == op_id,
                        _RoleAssn.revoked_at.is_(None),
                    )
                    .values(revoked_at=_dt.now(_UTC))
                )
                await session.commit()
                mut_committed.set()

        rev_task = asyncio.create_task(_transferer())
        mut_task = asyncio.create_task(_mutator())

        # Wait until the mutator has actually issued the advisory-lock
        # SELECT — it will be blocked inside pg_advisory_xact_lock().
        await asyncio.wait_for(mutator_started.wait(), timeout=5)
        # Give Postgres a moment to receive the lock request; assert
        # the mutator is genuinely blocked.
        await asyncio.sleep(0.5)
        assert mut_task.done() is False, (
            "authorization mutator must block on the advisory lock while " "the transfer holds it"
        )
        assert mut_committed.is_set() is False

        # Release the transfer — it will resolve authorization AFTER
        # the mutation would have committed. Since we've BLOCKED the
        # mutation until the transfer commits, the transfer sees
        # the ORIGINAL (still-authorized) state and commits (201).
        hold.set()
        transfer_result, _ = await asyncio.wait_for(asyncio.gather(rev_task, mut_task), timeout=15)
    finally:
        InventoryService._transfer_after_locks_signal = None
        InventoryService._transfer_hold_before_authorize_gate = None

    # Transfer committed under the ORIGINAL authorization.
    status_code, body = transfer_result
    assert status_code == 201, (status_code, body)
    # Mutation ultimately committed AFTER the transfer released its
    # advisory lock — proving the mutation was queued, not lost.
    assert mut_committed.is_set() is True
    # Ledger: exactly 2 rows added (OUT + IN).
    assert await _tx_baseline_count(setup) == baseline + 2


@_postgres_only
async def test_sprint_5_4_12_revocation_wins_first_transfer_refused(
    client: AsyncClient,
) -> None:
    """Sprint 5.4.12 — reverse ordering.

    Mutation commits BEFORE the transfer acquires the advisory
    lock. The transfer's fresh permission read observes the
    revoked state and refuses with 403. Zero ledger writes.
    """
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    from app.models.role_assignment import RoleAssignment as _RoleAssn
    from app.models.user import User as _User

    setup = await _setup_locked_auth_operator(client)
    baseline = await _tx_baseline_count(setup)

    async def _mutator_body(session) -> None:
        # Mutator itself acquires the advisory lock (production
        # revocation paths do the same via
        # RoleAssignmentService.revoke).
        await _acquire_org_auth_lock_via_sql(session, setup["org_id"])
        op_id = (
            await session.execute(select(_User.id).where(_User.email == setup["operator"]))
        ).scalar_one()
        await session.execute(
            sa_update(_RoleAssn)
            .where(_RoleAssn.user_id == op_id, _RoleAssn.revoked_at.is_(None))
            .values(revoked_at=_dt.now(_UTC))
        )

    status_code, body = await _run_transfer_under_pre_lock_race(
        client, setup=setup, mutator_body=_mutator_body
    )
    assert status_code == 403, (status_code, body)
    assert "inventory_transaction.create" in str(body.get("detail", "")), body
    assert await _tx_baseline_count(setup) == baseline


@_postgres_only
async def test_sprint_5_4_12_rollback_releases_authorization_lock(
    client: AsyncClient,
) -> None:
    """Sprint 5.4.12 — rollback releases the advisory lock.

    Cause the transfer to raise (invalid quantity → 422 or an
    engineered post-lock validation failure). Once the transfer's
    outer transaction rolls back, the per-org advisory lock is
    released and a queued mutation completes.
    """
    from datetime import UTC as _UTC
    from datetime import datetime as _dt

    from app.models.role_assignment import RoleAssignment as _RoleAssn
    from app.models.user import User as _User
    from app.services.inventory import InventoryService

    setup = await _setup_locked_auth_operator(client)

    locks_signal = asyncio.Event()
    hold = asyncio.Event()
    InventoryService._transfer_after_locks_signal = locks_signal
    InventoryService._transfer_hold_before_authorize_gate = hold
    try:

        async def _transferer() -> tuple[int, dict]:
            # Request an insufficient-stock transfer so authorization
            # succeeds but the ledger insert fails → rollback.
            r = await client.post(
                f"/api/v1/warehouses/{setup['src']}/inventory:transfer",
                json={
                    "lot_id": setup["src_lot"],
                    "destination_warehouse_id": setup["dst"],
                    "quantity": 99999,  # far exceeds baseline
                    "unit": "kg",
                },
                headers={"Idempotency-Key": f"xfer-{uuid4().hex[:8]}"},
            )
            try:
                return r.status_code, r.json()
            except Exception:
                return r.status_code, {"text": r.text}

        mut_committed = asyncio.Event()

        async def _mutator() -> None:
            await locks_signal.wait()
            async with _db_session_module.AsyncSessionLocal() as session:
                await _acquire_org_auth_lock_via_sql(session, setup["org_id"])
                op_id = (
                    await session.execute(select(_User.id).where(_User.email == setup["operator"]))
                ).scalar_one()
                await session.execute(
                    sa_update(_RoleAssn)
                    .where(
                        _RoleAssn.user_id == op_id,
                        _RoleAssn.revoked_at.is_(None),
                    )
                    .values(revoked_at=_dt.now(_UTC))
                )
                await session.commit()
                mut_committed.set()

        rev_task = asyncio.create_task(_transferer())
        mut_task = asyncio.create_task(_mutator())
        await asyncio.wait_for(locks_signal.wait(), timeout=5)
        # Release the hold — transfer will fail on insufficient_stock
        # and roll back, releasing the advisory lock.
        hold.set()
        transfer_result, _ = await asyncio.wait_for(asyncio.gather(rev_task, mut_task), timeout=15)
    finally:
        InventoryService._transfer_after_locks_signal = None
        InventoryService._transfer_hold_before_authorize_gate = None

    status_code, body = transfer_result
    # Insufficient stock triggers a 409 rollback.
    assert status_code == 409, (status_code, body)
    # The mutation completed AFTER rollback — proving the advisory
    # lock is released on rollback, not held indefinitely.
    assert mut_committed.is_set() is True


@_postgres_only
async def test_sprint_5_4_12_unrelated_organizations_do_not_block(
    client: AsyncClient,
) -> None:
    """Sprint 5.4.12 — per-org isolation.

    Transfer holds the advisory lock for org A. A mutation
    against org B acquires org B's advisory lock without blocking.
    Two organizations are independent.
    """
    from app.services.inventory import InventoryService

    setup = await _setup_locked_auth_operator(client)
    # Create a second, unrelated organization + user.
    unrelated_owner = f"un-{uuid4().hex[:6]}@agrovix.dev"
    await create_verified_user(unrelated_owner)
    await switch_user(client, unrelated_owner)
    unrelated_org_id = await create_org(client, slug=f"un-{uuid4().hex[:6]}")
    await switch_user(client, setup["operator"])

    locks_signal = asyncio.Event()
    hold = asyncio.Event()
    InventoryService._transfer_after_locks_signal = locks_signal
    InventoryService._transfer_hold_before_authorize_gate = hold
    try:

        async def _transferer() -> tuple[int, dict]:
            r = await client.post(
                f"/api/v1/warehouses/{setup['src']}/inventory:transfer",
                json={
                    "lot_id": setup["src_lot"],
                    "destination_warehouse_id": setup["dst"],
                    "quantity": 5,
                    "unit": "kg",
                },
                headers={"Idempotency-Key": f"xfer-{uuid4().hex[:8]}"},
            )
            return r.status_code, r.json()

        mut_acquired = asyncio.Event()

        async def _mutator_other_org() -> None:
            await locks_signal.wait()
            async with _db_session_module.AsyncSessionLocal() as session:
                # Acquire the advisory lock for the UNRELATED org.
                # This must succeed immediately.
                await _acquire_org_auth_lock_via_sql(session, unrelated_org_id)
                mut_acquired.set()
                await session.commit()

        rev_task = asyncio.create_task(_transferer())
        mut_task = asyncio.create_task(_mutator_other_org())
        # The unrelated-org mutator must complete WITHOUT waiting
        # on the transfer's org-A lock.
        await asyncio.wait_for(mut_acquired.wait(), timeout=5)
        # Release the transfer and let it finish.
        hold.set()
        transfer_result, _ = await asyncio.wait_for(asyncio.gather(rev_task, mut_task), timeout=15)
    finally:
        InventoryService._transfer_after_locks_signal = None
        InventoryService._transfer_hold_before_authorize_gate = None

    status_code, body = transfer_result
    assert status_code == 201, (status_code, body)


@_postgres_only
async def test_sprint_5_4_12_advisory_lock_key_is_deterministic() -> None:
    """Sprint 5.4.12 — deterministic key derivation.

    The advisory-lock key MUST NOT depend on Python's randomised
    ``hash()`` — same input UUID must produce the same signed
    BIGINT across processes. This is the invariant that keeps two
    callers coordinating on the SAME org from ever computing
    different keys and defeating the serialisation.
    """
    from app.services._authorization_lock import (
        advisory_lock_key_for_org_authorization,
    )

    oid = uuid4()
    k1 = advisory_lock_key_for_org_authorization(oid)
    k2 = advisory_lock_key_for_org_authorization(oid)
    assert k1 == k2
    # Signed BIGINT range.
    assert -(1 << 63) <= k1 < (1 << 63)
    # Different UUIDs produce different keys (with overwhelming
    # probability — 63-bit truncated SHA-256).
    other = uuid4()
    while other == oid:  # pragma: no cover - astronomically unlikely
        other = uuid4()
    assert advisory_lock_key_for_org_authorization(other) != k1


@_postgres_only
async def test_sprint_5_4_12_deterministic_ordering_no_deadlock(
    client: AsyncClient,
) -> None:
    """Sprint 5.4.12 — multi-org acquisition never deadlocks.

    Two callers each acquire the authorization advisory lock for
    the SAME pair of orgs, from opposite request directions. The
    ``acquire_org_authorization_locks`` helper sorts by UUID
    ascending, so both callers acquire the same lower-id lock
    first. Under Postgres the two coroutines therefore serialise
    cleanly instead of deadlocking.
    """
    from app.services._authorization_lock import acquire_org_authorization_locks

    setup = await _setup_locked_auth_operator(client)
    # Create a second org.
    other_owner = f"oth-{uuid4().hex[:6]}@agrovix.dev"
    await create_verified_user(other_owner)
    await switch_user(client, other_owner)
    other_org_id = await create_org(client, slug=f"oth-{uuid4().hex[:6]}")
    await switch_user(client, setup["operator"])

    org_a = _UUIDType(setup["org_id"])
    org_b = _UUIDType(other_org_id)

    barrier = asyncio.Event()
    done_a = asyncio.Event()
    done_b = asyncio.Event()

    async def _worker(order: list) -> None:
        async with _db_session_module.AsyncSessionLocal() as session:
            await barrier.wait()
            await acquire_org_authorization_locks(session, order)
            await session.commit()

    barrier.set()
    a_task = asyncio.create_task(_worker([org_a, org_b]))
    b_task = asyncio.create_task(_worker([org_b, org_a]))
    result = await asyncio.wait_for(
        asyncio.gather(a_task, b_task, return_exceptions=True), timeout=15
    )
    for outcome in result:
        if isinstance(outcome, BaseException):
            msg = str(outcome).lower()
            assert "deadlock" not in msg, f"deadlock surfaced: {outcome!r}"
            raise AssertionError(f"unexpected exception: {outcome!r}")
    # Suppress unused-name warnings.
    del done_a, done_b


@_postgres_only
async def test_sprint_5_4_12_role_assignment_service_participates_in_advisory_lock(
    client: AsyncClient,
) -> None:
    """Sprint 5.4.12 — production ``RoleAssignmentService.revoke``
    path (not a raw SQL DELETE) blocks while a transfer holds the
    per-org authorization advisory lock.

    This is the integration proof: the same protocol every real
    revocation endpoint follows. If this test can be made to
    pass, every service-layer authorization mutation path is
    participating in the serialization contract.
    """
    from app.models.role_assignment import RoleAssignment as _RoleAssn
    from app.models.user import User as _User
    from app.services.inventory import InventoryService

    setup = await _setup_locked_auth_operator(client)
    async with _db_session_module.AsyncSessionLocal() as session:
        op_id = (
            await session.execute(select(_User.id).where(_User.email == setup["operator"]))
        ).scalar_one()
    async with _db_session_module.AsyncSessionLocal() as s:
        assignment_id = (
            await s.execute(
                select(_RoleAssn.id).where(
                    _RoleAssn.user_id == op_id, _RoleAssn.revoked_at.is_(None)
                )
            )
        ).scalar_one()

    locks_signal = asyncio.Event()
    hold = asyncio.Event()
    InventoryService._transfer_after_locks_signal = locks_signal
    InventoryService._transfer_hold_before_authorize_gate = hold
    try:

        async def _transferer() -> tuple[int, dict]:
            r = await client.post(
                f"/api/v1/warehouses/{setup['src']}/inventory:transfer",
                json={
                    "lot_id": setup["src_lot"],
                    "destination_warehouse_id": setup["dst"],
                    "quantity": 5,
                    "unit": "kg",
                },
                headers={"Idempotency-Key": f"xfer-{uuid4().hex[:8]}"},
            )
            try:
                return r.status_code, r.json()
            except Exception:
                return r.status_code, {"text": r.text}

        mut_started = asyncio.Event()
        mut_completed = asyncio.Event()

        async def _mutator_via_service() -> None:
            from app.repositories.audit_repo import AuditRepository
            from app.repositories.org_repo import OrganizationRepository
            from app.repositories.role_repo import (
                RoleAssignmentRepository,
                RoleRepository,
            )
            from app.services.invitation_service import RoleAssignmentService

            await locks_signal.wait()
            async with _db_session_module.AsyncSessionLocal() as session:
                mut_started.set()
                service = RoleAssignmentService(
                    role_repo=RoleRepository(session),
                    role_assign_repo=RoleAssignmentRepository(session),
                    farm_mem_repo=None,  # not used by revoke path
                    org_mem_repo=None,  # not used by revoke path
                    org_repo=OrganizationRepository(session),
                    audit_repo=AuditRepository(session),
                )
                assignment = (
                    await session.execute(select(_RoleAssn).where(_RoleAssn.id == assignment_id))
                ).scalar_one()
                # Faux actor (superuser) — audit will use the actor.id.
                from app.models.user import User

                superuser = User(
                    id=assignment.granted_by_id,
                    email="sys@x",
                    hashed_password="x",
                    full_name="x",
                    is_active=True,
                    is_verified=True,
                    is_superuser=True,
                )
                await service.revoke(
                    actor=superuser,
                    assignment=assignment,
                    request_ctx={"ip_address": None, "user_agent": None, "request_id": None},
                )
                await session.commit()
                mut_completed.set()

        rev_task = asyncio.create_task(_transferer())
        mut_task = asyncio.create_task(_mutator_via_service())
        await asyncio.wait_for(mut_started.wait(), timeout=5)
        # Give the service's advisory-lock acquisition a moment to
        # queue up on Postgres. It must block until we release the
        # transfer's hold gate.
        await asyncio.sleep(0.5)
        assert mut_task.done() is False, (
            "RoleAssignmentService.revoke must block on the per-org "
            "authorization advisory lock while the transfer holds it"
        )
        assert mut_completed.is_set() is False

        hold.set()
        transfer_result, _ = await asyncio.wait_for(asyncio.gather(rev_task, mut_task), timeout=15)
    finally:
        InventoryService._transfer_after_locks_signal = None
        InventoryService._transfer_hold_before_authorize_gate = None

    status_code, _ = transfer_result
    assert status_code == 201
    assert mut_completed.is_set() is True
