"""Release 6.0.3 Purchase Order REST API contract tests."""

from __future__ import annotations

import os
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select

from app.db import session as _db
from app.models.audit import AuditEvent
from app.models.business_partner import (
    BusinessPartner,
    BusinessPartnerCapability,
    BusinessPartnerCapabilityCode,
    BusinessPartnerPreferenceTier,
    BusinessPartnerQualificationStatus,
    BusinessPartnerSupplierProfile,
)
from app.models.farm import Farm
from app.models.inventory import InventoryItem, InventoryItemCategory, StockUnit
from app.models.membership import FarmMembership, OrganizationMembership
from app.models.purchase_order import (
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderSequence,
    PurchaseOrderStatus,
    PurchaseOrderTransition,
)
from app.models.role import Permission, Role, RoleScope
from app.models.role_assignment import RoleAssignment
from app.models.user import User
from tests._helpers import create_org, create_verified_user, switch_user

pytestmark = pytest.mark.asyncio

TODAY = date(2026, 8, 8)


async def _owner_org(client: AsyncClient) -> tuple[str, UUID]:
    email = f"po-owner-{uuid4().hex[:8]}@agrovix.dev"
    await create_verified_user(email)
    await switch_user(client, email)
    return email, UUID(await create_org(client, slug=f"po-{uuid4().hex[:8]}"))


async def _dependencies(org_id: UUID, *, approved: bool = True) -> dict[str, UUID]:
    async with _db.AsyncSessionLocal() as session:
        partner = BusinessPartner(
            organization_id=org_id,
            code=f"SUP-{uuid4().hex[:6]}",
            legal_name="Frozen Supplier Ltd",
            trading_name="Frozen Supplier",
            is_active=True,
        )
        item = InventoryItem(
            organization_id=org_id,
            code=f"ITEM-{uuid4().hex[:6]}",
            name="Feed Item",
            sku=f"SKU-{uuid4().hex[:6]}",
            category=InventoryItemCategory.FEED,
            canonical_unit=StockUnit.KG,
            is_active=True,
        )
        farm = Farm(
            organization_id=org_id,
            name="PO Farm",
            code=f"POF-{uuid4().hex[:6]}",
            is_active=True,
        )
        session.add_all([partner, item, farm])
        await session.flush()
        session.add(
            BusinessPartnerCapability(
                business_partner_id=partner.id,
                capability=BusinessPartnerCapabilityCode.SUPPLIER,
            )
        )
        session.add(
            BusinessPartnerSupplierProfile(
                business_partner_id=partner.id,
                qualification_status=(
                    BusinessPartnerQualificationStatus.APPROVED
                    if approved
                    else BusinessPartnerQualificationStatus.UNQUALIFIED
                ),
                preference_tier=BusinessPartnerPreferenceTier.STANDARD,
            )
        )
        await session.commit()
        return {"partner": partner.id, "item": item.id, "farm": farm.id}


async def _assign_role(email: str, org_id: UUID, role_name: str, farm_id: UUID | None) -> None:
    async with _db.AsyncSessionLocal() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        role = (await session.execute(select(Role).where(Role.name == role_name))).scalar_one()
        membership = (
            await session.execute(
                select(OrganizationMembership).where(
                    OrganizationMembership.user_id == user.id,
                    OrganizationMembership.organization_id == org_id,
                )
            )
        ).scalar_one_or_none()
        if membership is None:
            session.add(
                OrganizationMembership(user_id=user.id, organization_id=org_id, is_active=True)
            )
        if farm_id is not None:
            session.add(FarmMembership(user_id=user.id, farm_id=farm_id, is_active=True))
        session.add(
            RoleAssignment(
                user_id=user.id,
                role_id=role.id,
                organization_id=org_id,
                farm_id=farm_id,
            )
        )
        await session.commit()


async def _assign_custom_role(
    email: str, org_id: UUID, permission_codes: set[str], *, farm_id: UUID | None = None
) -> None:
    async with _db.AsyncSessionLocal() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        permissions = list(
            (
                await session.execute(
                    select(Permission).where(Permission.code.in_(permission_codes))
                )
            ).scalars()
        )
        assert {permission.code for permission in permissions} == permission_codes
        role = Role(
            name=f"po-custom-{uuid4().hex}",
            description="Purchase Order authorization regression role",
            scope=RoleScope.FARM if farm_id is not None else RoleScope.ORGANIZATION,
            is_system=False,
            permissions=permissions,
        )
        session.add(role)
        session.add(OrganizationMembership(user_id=user.id, organization_id=org_id, is_active=True))
        if farm_id is not None:
            session.add(FarmMembership(user_id=user.id, farm_id=farm_id, is_active=True))
        await session.flush()
        session.add(
            RoleAssignment(
                user_id=user.id,
                role_id=role.id,
                organization_id=org_id,
                farm_id=farm_id,
            )
        )
        await session.commit()


def _line(item_id: UUID, *, quantity: str = "10.000000", price: str = "2.500000") -> dict:
    return {
        "inventory_item_id": str(item_id),
        "ordered_quantity": quantity,
        "ordered_unit": "kg",
        "unit_price": price,
        "description": "Frozen feed",
    }


async def _create_po(
    client: AsyncClient,
    org_id: UUID,
    deps: dict[str, UUID],
    *,
    lines: list[dict] | None = None,
    farm: bool = False,
) -> dict:
    body = {
        "business_partner_id": str(deps["partner"]),
        "currency_code": "USD",
        "order_date": TODAY.isoformat(),
        "supplier_reference": "SUP-REF-001",
        "lines": [_line(deps["item"])] if lines is None else lines,
    }
    if farm:
        body["farm_id"] = str(deps["farm"])
    response = await client.post(f"/api/v1/organizations/{org_id}/purchase-orders", json=body)
    assert response.status_code == 201, response.text
    return response.json()


async def _post_po(
    client: AsyncClient,
    org_id: UUID,
    deps: dict[str, UUID],
    **overrides,
):
    body = {
        "business_partner_id": str(deps["partner"]),
        "currency_code": "USD",
        "order_date": TODAY.isoformat(),
        "supplier_reference": "SUP-REF-001",
        "lines": [_line(deps["item"])],
        **overrides,
    }
    return await client.post(f"/api/v1/organizations/{org_id}/purchase-orders", json=body)


async def _set_superuser(email: str) -> UUID:
    async with _db.AsyncSessionLocal() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        user.is_superuser = True
        await session.commit()
        return user.id


async def _fresh_po(po_id: str) -> tuple[PurchaseOrder, list[PurchaseOrderLine]]:
    async with _db.AsyncSessionLocal() as session:
        po = await session.get(PurchaseOrder, UUID(po_id))
        lines = list(
            (
                await session.execute(
                    select(PurchaseOrderLine)
                    .where(PurchaseOrderLine.purchase_order_id == po.id)
                    .order_by(PurchaseOrderLine.line_number)
                )
            )
            .scalars()
            .all()
        )
        session.expunge(po)
        for line in lines:
            session.expunge(line)
        return po, lines


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/organizations/00000000-0000-0000-0000-000000000001/purchase-orders"),
        ("POST", "/api/v1/organizations/00000000-0000-0000-0000-000000000001/purchase-orders"),
        ("GET", "/api/v1/purchase-orders/00000000-0000-0000-0000-000000000001"),
        ("PATCH", "/api/v1/purchase-orders/00000000-0000-0000-0000-000000000001"),
        ("POST", "/api/v1/purchase-orders/00000000-0000-0000-0000-000000000001/submit"),
        ("POST", "/api/v1/purchase-orders/00000000-0000-0000-0000-000000000001/withdraw"),
        ("POST", "/api/v1/purchase-orders/00000000-0000-0000-0000-000000000001/approve"),
        ("POST", "/api/v1/purchase-orders/00000000-0000-0000-0000-000000000001/reject"),
        ("POST", "/api/v1/purchase-orders/00000000-0000-0000-0000-000000000001/revise"),
        ("POST", "/api/v1/purchase-orders/00000000-0000-0000-0000-000000000001/cancel"),
        ("GET", "/api/v1/purchase-orders/00000000-0000-0000-0000-000000000001/transitions"),
    ],
)
async def test_purchase_order_routes_require_authentication(
    client: AsyncClient, method: str, path: str
) -> None:
    response = await client.request(method, path, json={})
    assert response.status_code == 401


async def test_create_detail_decimal_snapshot_and_number(client: AsyncClient) -> None:
    _email, org_id = await _owner_org(client)
    deps = await _dependencies(org_id)
    po = await _create_po(client, org_id, deps, farm=True)
    assert po["status"] == "DRAFT"
    assert po["version"] == 1
    assert po["po_number"] == f"PO-{TODAY.year}-000001"
    assert po["supplier_legal_name"] == "Frozen Supplier Ltd"
    assert po["subtotal"] == "25.000000"
    assert po["lines"][0]["ordered_quantity"] == "10.000000"
    assert po["lines"][0]["unit_price"] == "2.500000"
    assert po["lines"][0]["extended_amount"] == "25.000000"

    detail = await client.get(f"/api/v1/purchase-orders/{po['id']}")
    assert detail.status_code == 200
    current = detail.json()
    assert current["id"] == po["id"]
    assert current["po_number"] == po["po_number"]
    assert current["supplier_legal_name"] == po["supplier_legal_name"]
    assert current["subtotal"] == po["subtotal"]
    assert current["lines"][0]["id"] == po["lines"][0]["id"]


@pytest.mark.skipif(
    os.environ.get("DATABASE_URL", "sqlite").startswith("sqlite"),
    reason="SQLite cannot preserve maximum NUMERIC(18,6)/NUMERIC(20,6) values exactly.",
)
async def test_maximum_legal_decimals_serialize_across_shared_response_paths(
    client: AsyncClient,
) -> None:
    _email, org_id = await _owner_org(client)
    deps = await _dependencies(org_id)
    expected = "99999999999999999899000000.000000"
    po = await _create_po(
        client,
        org_id,
        deps,
        lines=[
            _line(
                deps["item"],
                quantity="999999999999.999999",
                price="99999999999999.999999",
            )
        ],
    )
    assert po["lines"][0]["extended_amount"] == expected
    assert po["subtotal"] == expected

    detail = await client.get(f"/api/v1/purchase-orders/{po['id']}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["lines"][0]["extended_amount"] == expected
    assert detail.json()["subtotal"] == expected

    listing = await client.get(f"/api/v1/organizations/{org_id}/purchase-orders")
    assert listing.status_code == 200, listing.text
    listed = next(item for item in listing.json()["items"] if item["id"] == po["id"])
    assert listed["lines"][0]["extended_amount"] == expected
    assert listed["subtotal"] == expected

    patched = await client.patch(
        f"/api/v1/purchase-orders/{po['id']}",
        json={"expected_version": po["version"], "notes": "boundary response"},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["lines"][0]["extended_amount"] == expected
    assert patched.json()["subtotal"] == expected


async def test_empty_draft_patch_stale_version_and_unknown_fields(client: AsyncClient) -> None:
    _email, org_id = await _owner_org(client)
    deps = await _dependencies(org_id)
    po = await _create_po(client, org_id, deps, lines=[])
    stale = await client.patch(
        f"/api/v1/purchase-orders/{po['id']}",
        json={"expected_version": 99, "notes": "stale"},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "purchase_order_version_conflict"

    unknown = await client.patch(
        f"/api/v1/purchase-orders/{po['id']}",
        json={"expected_version": 1, "status": "APPROVED"},
    )
    assert unknown.status_code == 422

    for field in ("business_partner_id", "currency_code", "order_date", "lines"):
        cleared = await client.patch(
            f"/api/v1/purchase-orders/{po['id']}",
            json={"expected_version": 1, field: None},
        )
        assert cleared.status_code == 422

    submit = await client.post(f"/api/v1/purchase-orders/{po['id']}/submit")
    assert submit.status_code == 409
    assert submit.json()["detail"]["code"] == "purchase_order_requires_line"


async def test_patch_preserves_line_identity_and_explicit_null(client: AsyncClient) -> None:
    _email, org_id = await _owner_org(client)
    deps = await _dependencies(org_id)
    po = await _create_po(client, org_id, deps)
    line = po["lines"][0]
    response = await client.patch(
        f"/api/v1/purchase-orders/{po['id']}",
        json={
            "expected_version": 1,
            "supplier_reference": None,
            "lines": [{**_line(deps["item"], quantity="11.000000"), "id": line["id"]}],
        },
    )
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["version"] == 2
    assert updated["supplier_reference"] is None
    assert updated["lines"][0]["id"] == line["id"]
    assert updated["lines"][0]["ordered_quantity"] == "11.000000"


async def test_lifecycle_replay_headers_and_transition_history(client: AsyncClient) -> None:
    _email, org_id = await _owner_org(client)
    deps = await _dependencies(org_id)
    po = await _create_po(client, org_id, deps)

    first = await client.post(f"/api/v1/purchase-orders/{po['id']}/submit")
    assert first.status_code == 200, first.text
    assert first.headers.get("X-Idempotent-Replay") is None
    replay = await client.post(f"/api/v1/purchase-orders/{po['id']}/submit")
    assert replay.status_code == 200
    assert replay.headers["X-Idempotent-Replay"] == "true"
    assert replay.json()["version"] == first.json()["version"] == 2

    withdrawn = await client.post(
        f"/api/v1/purchase-orders/{po['id']}/withdraw", json={"reason": "revise order"}
    )
    assert withdrawn.status_code == 200
    withdraw_replay = await client.post(
        f"/api/v1/purchase-orders/{po['id']}/withdraw", json={"reason": "revise order"}
    )
    assert withdraw_replay.headers["X-Idempotent-Replay"] == "true"

    history = await client.get(f"/api/v1/purchase-orders/{po['id']}/transitions?limit=2")
    assert history.status_code == 200
    page = history.json()
    assert [row["operation"] for row in page["items"]] == ["create", "submit"]
    assert page["next_cursor"] is not None
    second = await client.get(
        f"/api/v1/purchase-orders/{po['id']}/transitions",
        params={"cursor": page["next_cursor"]},
    )
    assert [row["operation"] for row in second.json()["items"]] == ["withdraw"]


async def test_self_approval_and_independent_approval(client: AsyncClient) -> None:
    creator_email, org_id = await _owner_org(client)
    deps = await _dependencies(org_id)
    po = await _create_po(client, org_id, deps)
    assert (await client.post(f"/api/v1/purchase-orders/{po['id']}/submit")).status_code == 200

    self_approval = await client.post(f"/api/v1/purchase-orders/{po['id']}/approve")
    assert self_approval.status_code == 409
    assert self_approval.json()["detail"]["code"] == "purchase_order_self_approval_forbidden"

    approver_email = f"po-admin-{uuid4().hex[:8]}@agrovix.dev"
    await create_verified_user(approver_email)
    async with _db.AsyncSessionLocal() as session:
        approver = (
            await session.execute(select(User).where(User.email == approver_email))
        ).scalar_one()
        approver.is_superuser = True
        await session.commit()
    await switch_user(client, approver_email)
    approved = await client.post(f"/api/v1/purchase-orders/{po['id']}/approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "APPROVED"
    assert approved.json()["approved_by_id"] != po["created_by_id"]

    await switch_user(client, creator_email)


async def test_reject_revise_and_cancel_rejected(client: AsyncClient) -> None:
    _email, org_id = await _owner_org(client)
    deps = await _dependencies(org_id)
    po = await _create_po(client, org_id, deps)
    await client.post(f"/api/v1/purchase-orders/{po['id']}/submit")
    rejected = await client.post(
        f"/api/v1/purchase-orders/{po['id']}/reject", json={"reason": "incorrect"}
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "REJECTED"
    revised = await client.post(
        f"/api/v1/purchase-orders/{po['id']}/revise", json={"reason": "correct it"}
    )
    assert revised.status_code == 200
    assert revised.json()["status"] == "DRAFT"

    await client.post(f"/api/v1/purchase-orders/{po['id']}/submit")
    await client.post(
        f"/api/v1/purchase-orders/{po['id']}/reject", json={"reason": "still incorrect"}
    )
    cancelled = await client.post(
        f"/api/v1/purchase-orders/{po['id']}/cancel", json={"reason": "stop"}
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"


async def test_list_filters_search_cursor_and_invalid_cursor(client: AsyncClient) -> None:
    _email, org_id = await _owner_org(client)
    deps = await _dependencies(org_id)
    first = await _create_po(client, org_id, deps, farm=True)
    second = await _create_po(client, org_id, deps)

    page = await client.get(
        f"/api/v1/organizations/{org_id}/purchase-orders",
        params=[("status", "DRAFT"), ("search", "SUP-REF"), ("limit", "1")],
    )
    assert page.status_code == 200, page.text
    assert len(page.json()["items"]) == 1
    assert page.json()["next_cursor"] is not None
    filtered = await client.get(
        f"/api/v1/organizations/{org_id}/purchase-orders",
        params={"farm_id": str(deps["farm"])},
    )
    assert [row["id"] for row in filtered.json()["items"]] == [first["id"]]
    assert second["id"] != first["id"]

    invalid = await client.get(
        f"/api/v1/organizations/{org_id}/purchase-orders", params={"cursor": "bad"}
    )
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "invalid_cursor"


async def test_cross_tenant_po_is_hidden(client: AsyncClient) -> None:
    owner_a, org_a = await _owner_org(client)
    deps_a = await _dependencies(org_a)
    po = await _create_po(client, org_a, deps_a)

    _owner_b, org_b = await _owner_org(client)
    hidden = await client.get(f"/api/v1/purchase-orders/{po['id']}")
    assert hidden.status_code == 404
    hidden_patch = await client.patch(
        f"/api/v1/purchase-orders/{po['id']}", json={"expected_version": 1, "notes": "leak"}
    )
    assert hidden_patch.status_code == 404
    foreign_list = await client.get(f"/api/v1/organizations/{org_a}/purchase-orders")
    assert foreign_list.status_code == 404
    assert org_a != org_b
    await switch_user(client, owner_a)


@pytest.mark.parametrize(
    ("role_name", "farm_scoped"),
    [
        ("farm_manager", True),
        ("supervisor", True),
        ("storekeeper", True),
        ("accountant", False),
        ("viewer", False),
    ],
)
async def test_frozen_read_roles_and_create_permission_boundary(
    client: AsyncClient, role_name: str, farm_scoped: bool
) -> None:
    owner_email, org_id = await _owner_org(client)
    deps = await _dependencies(org_id)
    farm_po = await _create_po(client, org_id, deps, farm=True)
    unassigned_po = await _create_po(client, org_id, deps)

    reader_email = f"po-{role_name}-{uuid4().hex[:8]}@agrovix.dev"
    await create_verified_user(reader_email)
    await _assign_role(
        reader_email,
        org_id,
        role_name,
        deps["farm"] if farm_scoped else None,
    )
    await switch_user(client, reader_email)

    visible = await client.get(f"/api/v1/purchase-orders/{farm_po['id']}")
    assert visible.status_code == 200
    unassigned = await client.get(f"/api/v1/purchase-orders/{unassigned_po['id']}")
    assert unassigned.status_code == (404 if farm_scoped else 200)

    create = await client.post(
        f"/api/v1/organizations/{org_id}/purchase-orders",
        json={
            "business_partner_id": str(deps["partner"]),
            "farm_id": str(deps["farm"]) if farm_scoped else None,
            "currency_code": "USD",
            "order_date": TODAY.isoformat(),
            "lines": [],
        },
    )
    assert create.status_code == (201 if role_name == "farm_manager" else 403)
    await switch_user(client, owner_email)


async def test_farm_manager_update_submit_and_decision_permissions(client: AsyncClient) -> None:
    owner_email, org_id = await _owner_org(client)
    deps = await _dependencies(org_id)
    manager_email = f"po-manager-{uuid4().hex[:8]}@agrovix.dev"
    await create_verified_user(manager_email)
    await _assign_role(manager_email, org_id, "farm_manager", deps["farm"])
    await switch_user(client, manager_email)

    po = await _create_po(client, org_id, deps, farm=True)
    updated = await client.patch(
        f"/api/v1/purchase-orders/{po['id']}",
        json={"expected_version": 1, "notes": "farm-scoped update"},
    )
    assert updated.status_code == 200
    submitted = await client.post(f"/api/v1/purchase-orders/{po['id']}/submit")
    assert submitted.status_code == 200

    for operation, payload in (
        ("approve", None),
        ("reject", {"reason": "not permitted"}),
        ("cancel", {"reason": "not permitted"}),
    ):
        response = await client.post(
            f"/api/v1/purchase-orders/{po['id']}/{operation}", json=payload
        )
        assert response.status_code == 403

    withdrawn = await client.post(
        f"/api/v1/purchase-orders/{po['id']}/withdraw", json={"reason": "edit again"}
    )
    assert withdrawn.status_code == 200
    await switch_user(client, owner_email)


async def test_farm_scoped_update_cannot_clear_or_cross_assign_farm(
    client: AsyncClient,
) -> None:
    owner_email, org_id = await _owner_org(client)
    deps = await _dependencies(org_id)
    other_deps = await _dependencies(org_id)
    po = await _create_po(client, org_id, deps, farm=True)

    manager_email = f"po-farm-update-{uuid4().hex[:8]}@agrovix.dev"
    await create_verified_user(manager_email)
    await _assign_custom_role(
        manager_email,
        org_id,
        {"purchase_order.read", "purchase_order.update"},
        farm_id=deps["farm"],
    )
    await switch_user(client, manager_email)

    allowed = await client.patch(
        f"/api/v1/purchase-orders/{po['id']}",
        json={"expected_version": 1, "notes": "allowed farm update"},
    )
    assert allowed.status_code == 200, allowed.text
    version = allowed.json()["version"]

    cross_farm = await client.patch(
        f"/api/v1/purchase-orders/{po['id']}",
        json={"expected_version": version, "farm_id": str(other_deps["farm"])},
    )
    assert cross_farm.status_code == 403

    cleared = await client.patch(
        f"/api/v1/purchase-orders/{po['id']}",
        json={"expected_version": version, "farm_id": None},
    )
    assert cleared.status_code == 403

    current, _ = await _fresh_po(po["id"])
    assert current.farm_id == deps["farm"]
    assert current.version == version
    assert current.notes == "allowed farm update"

    await switch_user(client, owner_email)
    org_clear = await client.patch(
        f"/api/v1/purchase-orders/{po['id']}",
        json={"expected_version": version, "farm_id": None},
    )
    assert org_clear.status_code == 200, org_clear.text
    assert org_clear.json()["farm_id"] is None


async def test_farm_destination_denials_are_indistinguishable_and_atomic(
    client: AsyncClient,
) -> None:
    owner_email, org_id = await _owner_org(client)
    deps = await _dependencies(org_id)
    other_deps = await _dependencies(org_id)
    inactive_deps = await _dependencies(org_id)
    async with _db.AsyncSessionLocal() as session:
        inactive_farm = await session.get(Farm, inactive_deps["farm"])
        inactive_farm.is_active = False
        await session.commit()

    _foreign_owner, foreign_org_id = await _owner_org(client)
    foreign_deps = await _dependencies(foreign_org_id)
    await switch_user(client, owner_email)
    po = await _create_po(client, org_id, deps, farm=True)

    manager_email = f"po-farm-update-{uuid4().hex[:8]}@agrovix.dev"
    await create_verified_user(manager_email)
    await _assign_custom_role(
        manager_email,
        org_id,
        {"purchase_order.read", "purchase_order.update"},
        farm_id=deps["farm"],
    )
    await switch_user(client, manager_email)

    allowed = await client.patch(
        f"/api/v1/purchase-orders/{po['id']}",
        json={"expected_version": 1, "notes": "allowed farm update"},
    )
    assert allowed.status_code == 200, allowed.text
    version = allowed.json()["version"]

    async with _db.AsyncSessionLocal() as session:
        transitions_before = list(
            (
                await session.execute(
                    select(PurchaseOrderTransition)
                    .where(PurchaseOrderTransition.purchase_order_id == UUID(po["id"]))
                    .order_by(PurchaseOrderTransition.id)
                )
            ).scalars()
        )
        audits_before = list(
            (
                await session.execute(
                    select(AuditEvent)
                    .where(AuditEvent.entity_id == po["id"])
                    .order_by(AuditEvent.id)
                )
            ).scalars()
        )
        transition_snapshot = [
            (row.id, row.from_status, row.to_status, row.reason, row.metadata_json)
            for row in transitions_before
        ]
        audit_snapshot = [
            (row.id, row.action, row.farm_id, row.metadata_json) for row in audits_before
        ]

    current_before, lines_before = await _fresh_po(po["id"])
    line_snapshot = [
        (
            line.id,
            line.line_number,
            line.inventory_item_id,
            line.ordered_quantity,
            line.unit_price,
            line.description,
        )
        for line in lines_before
    ]

    candidate_farm_ids = (
        other_deps["farm"],
        foreign_deps["farm"],
        uuid4(),
        inactive_deps["farm"],
    )
    denial_contracts = []
    for candidate_farm_id in candidate_farm_ids:
        denied = await client.patch(
            f"/api/v1/purchase-orders/{po['id']}",
            json={"expected_version": version, "farm_id": str(candidate_farm_id)},
        )
        denial_contracts.append((denied.status_code, denied.json()))
    assert all(contract == denial_contracts[0] for contract in denial_contracts)
    assert denial_contracts[0][0] == 403
    assert denial_contracts[0][1]["detail"]["code"] == "not_authorized"

    combined_denial = await client.patch(
        f"/api/v1/purchase-orders/{po['id']}",
        json={
            "expected_version": version,
            "farm_id": None,
            "notes": "must not persist",
            "currency_code": "EUR",
            "order_date": "2026-09-01",
            "expected_delivery_date": "2026-09-02",
            "supplier_reference": "must-not-persist",
            "lines": [
                {
                    **_line(deps["item"], quantity="99.000000", price="7.000000"),
                    "id": str(lines_before[0].id),
                }
            ],
        },
    )
    assert (combined_denial.status_code, combined_denial.json()) == denial_contracts[0]

    current, lines_after = await _fresh_po(po["id"])
    assert current.farm_id == deps["farm"]
    assert current.version == version
    assert current.notes == current_before.notes == "allowed farm update"
    assert current.currency_code == current_before.currency_code == "USD"
    assert current.order_date == current_before.order_date == TODAY
    assert current.expected_delivery_date == current_before.expected_delivery_date
    assert current.supplier_reference == current_before.supplier_reference == "SUP-REF-001"
    assert current.business_partner_id == current_before.business_partner_id
    assert current.status == current_before.status == PurchaseOrderStatus.DRAFT
    assert [
        (
            line.id,
            line.line_number,
            line.inventory_item_id,
            line.ordered_quantity,
            line.unit_price,
            line.description,
        )
        for line in lines_after
    ] == line_snapshot

    async with _db.AsyncSessionLocal() as session:
        transitions_after = list(
            (
                await session.execute(
                    select(PurchaseOrderTransition)
                    .where(PurchaseOrderTransition.purchase_order_id == UUID(po["id"]))
                    .order_by(PurchaseOrderTransition.id)
                )
            ).scalars()
        )
        audits_after = list(
            (
                await session.execute(
                    select(AuditEvent)
                    .where(AuditEvent.entity_id == po["id"])
                    .order_by(AuditEvent.id)
                )
            ).scalars()
        )
    assert [
        (row.id, row.from_status, row.to_status, row.reason, row.metadata_json)
        for row in transitions_after
    ] == transition_snapshot
    assert [
        (row.id, row.action, row.farm_id, row.metadata_json) for row in audits_after
    ] == audit_snapshot

    await switch_user(client, owner_email)
    org_clear = await client.patch(
        f"/api/v1/purchase-orders/{po['id']}",
        json={"expected_version": version, "farm_id": None},
    )
    assert org_clear.status_code == 200, org_clear.text
    assert org_clear.json()["farm_id"] is None

    org_reassign = await client.patch(
        f"/api/v1/purchase-orders/{po['id']}",
        json={
            "expected_version": org_clear.json()["version"],
            "farm_id": str(other_deps["farm"]),
        },
    )
    assert org_reassign.status_code == 200, org_reassign.text
    assert org_reassign.json()["farm_id"] == str(other_deps["farm"])


async def test_withdraw_requires_update_not_submit_permission(client: AsyncClient) -> None:
    owner_email, org_id = await _owner_org(client)
    deps = await _dependencies(org_id)
    po = await _create_po(client, org_id, deps)
    assert (await client.post(f"/api/v1/purchase-orders/{po['id']}/submit")).status_code == 200

    updater_email = f"po-withdraw-update-{uuid4().hex[:8]}@agrovix.dev"
    await create_verified_user(updater_email)
    await _assign_custom_role(
        updater_email, org_id, {"purchase_order.read", "purchase_order.update"}
    )
    await switch_user(client, updater_email)
    withdrawn = await client.post(
        f"/api/v1/purchase-orders/{po['id']}/withdraw", json={"reason": "update required"}
    )
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["status"] == "DRAFT"
    assert (await client.post(f"/api/v1/purchase-orders/{po['id']}/submit")).status_code == 403

    await switch_user(client, owner_email)
    assert (await client.post(f"/api/v1/purchase-orders/{po['id']}/submit")).status_code == 200

    submitter_email = f"po-withdraw-submit-{uuid4().hex[:8]}@agrovix.dev"
    await create_verified_user(submitter_email)
    await _assign_custom_role(
        submitter_email, org_id, {"purchase_order.read", "purchase_order.submit"}
    )
    await switch_user(client, submitter_email)
    denied = await client.post(
        f"/api/v1/purchase-orders/{po['id']}/withdraw", json={"reason": "not update"}
    )
    assert denied.status_code == 403

    current, _ = await _fresh_po(po["id"])
    assert current.status == PurchaseOrderStatus.SUBMITTED


async def test_supplier_qualification_is_revalidated_at_submit(client: AsyncClient) -> None:
    _email, org_id = await _owner_org(client)
    deps = await _dependencies(org_id, approved=False)
    po = await _create_po(client, org_id, deps)
    response = await client.post(f"/api/v1/purchase-orders/{po['id']}/submit")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "business_partner_not_approved"


async def test_invalid_decimal_foreign_supplier_and_stale_governance(client: AsyncClient) -> None:
    _email, org_id = await _owner_org(client)
    deps = await _dependencies(org_id)
    numeric = await client.post(
        f"/api/v1/organizations/{org_id}/purchase-orders",
        json={
            "business_partner_id": str(deps["partner"]),
            "currency_code": "USD",
            "order_date": TODAY.isoformat(),
            "lines": [_line(deps["item"]) | {"ordered_quantity": 10.0}],
        },
    )
    assert numeric.status_code == 422

    po = await _create_po(client, org_id, deps)
    async with _db.AsyncSessionLocal() as session:
        farm = await session.get(Farm, deps["farm"])
        farm.is_active = False
        item = await session.get(InventoryItem, deps["item"])
        item.is_active = False
        await session.commit()
    stale = await client.post(f"/api/v1/purchase-orders/{po['id']}/submit")
    assert stale.status_code == 404


async def test_order_year_validation_precedes_sequence_allocation(client: AsyncClient) -> None:
    _email, org_id = await _owner_org(client)
    deps = await _dependencies(org_id)
    async with _db.AsyncSessionLocal() as session:
        sequence_before = (
            await session.execute(select(func.count()).select_from(PurchaseOrderSequence))
        ).scalar_one()
        po_before = (
            await session.execute(select(func.count()).select_from(PurchaseOrder))
        ).scalar_one()

    rejected = await _post_po(client, org_id, deps, order_date="1999-12-31")
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["code"] == "invalid_order_date"
    assert rejected.json()["detail"]["message"] == "order_date year must be between 2000 and 9999."
    response_text = rejected.text.lower()
    assert all(marker not in response_text for marker in ("sql", "constraint", "driver", "trace"))

    async with _db.AsyncSessionLocal() as session:
        sequence_after = (
            await session.execute(select(func.count()).select_from(PurchaseOrderSequence))
        ).scalar_one()
        po_after = (
            await session.execute(select(func.count()).select_from(PurchaseOrder))
        ).scalar_one()
    assert sequence_after == sequence_before
    assert po_after == po_before

    lower = await _post_po(client, org_id, deps, order_date="2000-01-01")
    assert lower.status_code == 201, lower.text
    assert lower.json()["po_number"].startswith("PO-2000-")

    upper = await _post_po(client, org_id, deps, order_date="9999-12-31")
    assert upper.status_code == 201, upper.text
    assert upper.json()["po_number"].startswith("PO-9999-")


async def test_authorization_revoked_after_load_returns_forbidden(client: AsyncClient) -> None:
    email, org_id = await _owner_org(client)
    deps = await _dependencies(org_id)
    po = await _create_po(client, org_id, deps)
    async with _db.AsyncSessionLocal() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        membership = (
            await session.execute(
                select(OrganizationMembership).where(
                    OrganizationMembership.user_id == user.id,
                    OrganizationMembership.organization_id == org_id,
                )
            )
        ).scalar_one()
        membership.is_active = False
        await session.commit()
    hidden = await client.get(f"/api/v1/purchase-orders/{po['id']}")
    assert hidden.status_code == 404


async def test_no_forbidden_purchase_order_routes_are_registered(client: AsyncClient) -> None:
    _email, org_id = await _owner_org(client)
    deps = await _dependencies(org_id)
    po = await _create_po(client, org_id, deps)
    assert (await client.delete(f"/api/v1/purchase-orders/{po['id']}")).status_code == 405
    assert (await client.post(f"/api/v1/purchase-orders/{po['id']}/receive")).status_code == 404
    async with _db.AsyncSessionLocal() as session:
        persisted = await session.get(PurchaseOrder, UUID(po["id"]))
        assert persisted.status == PurchaseOrderStatus.DRAFT


async def test_foreign_create_dependencies_and_farm_scope_are_hidden(client: AsyncClient) -> None:
    owner_a, org_a = await _owner_org(client)
    deps_a = await _dependencies(org_a)
    _owner_b, org_b = await _owner_org(client)
    deps_b = await _dependencies(org_b)
    await switch_user(client, owner_a)

    for override in (
        {"business_partner_id": str(deps_b["partner"])},
        {"farm_id": str(deps_b["farm"])},
        {"lines": [_line(deps_b["item"])]},
    ):
        response = await _post_po(client, org_a, deps_a, **override)
        assert response.status_code == 404

    other_farm = await _dependencies(org_a)
    manager = f"po-scoped-{uuid4().hex[:8]}@agrovix.dev"
    await create_verified_user(manager)
    await _assign_role(manager, org_a, "farm_manager", deps_a["farm"])
    await switch_user(client, manager)
    inaccessible = await _post_po(
        client,
        org_a,
        deps_a,
        farm_id=str(other_farm["farm"]),
    )
    assert inaccessible.status_code == 404


async def test_create_http_validation_and_governance_boundaries(client: AsyncClient) -> None:
    _email, org_id = await _owner_org(client)
    deps = await _dependencies(org_id)

    invalid_cases = (
        ({"currency_code": "ZZZ"}, 422, "invalid_currency"),
        (
            {"expected_delivery_date": (TODAY - timedelta(days=1)).isoformat()},
            409,
            "purchase_order_invalid_delivery_date",
        ),
        ({"lines": [_line(deps["item"], quantity="0.000000")]}, 422, "invalid_quantity"),
        (
            {"lines": [_line(deps["item"], quantity="1000000000000.000000")]},
            422,
            "value_out_of_range",
        ),
        (
            {"lines": [_line(deps["item"], price="100000000000000.000000")]},
            422,
            "value_out_of_range",
        ),
        (
            {"lines": [_line(deps["item"], price="0.000000")]},
            409,
            "purchase_order_line_note_required",
        ),
        (
            {"lines": [_line(deps["item"]) | {"ordered_unit": "L"}]},
            409,
            "unit_incompatible",
        ),
    )
    for override, expected_status, expected_code in invalid_cases:
        response = await _post_po(client, org_id, deps, **override)
        assert response.status_code == expected_status, response.text
        assert response.json()["detail"]["code"] == expected_code

    async with _db.AsyncSessionLocal() as session:
        partner = await session.get(BusinessPartner, deps["partner"])
        partner.is_active = False
        await session.commit()
    inactive_supplier = await _post_po(client, org_id, deps)
    assert inactive_supplier.status_code == 409
    assert inactive_supplier.json()["detail"]["code"] == "business_partner_inactive"

    deps_no_capability = await _dependencies(org_id)
    async with _db.AsyncSessionLocal() as session:
        capability = (
            await session.execute(
                select(BusinessPartnerCapability).where(
                    BusinessPartnerCapability.business_partner_id == deps_no_capability["partner"]
                )
            )
        ).scalar_one()
        await session.delete(capability)
        await session.commit()
    no_capability = await _post_po(client, org_id, deps_no_capability)
    assert no_capability.status_code == 409
    assert no_capability.json()["detail"]["code"] == "business_partner_not_supplier"

    deps_inactive = await _dependencies(org_id)
    async with _db.AsyncSessionLocal() as session:
        farm = await session.get(Farm, deps_inactive["farm"])
        farm.is_active = False
        item = await session.get(InventoryItem, deps_inactive["item"])
        item.is_active = False
        await session.commit()
    assert (
        await _post_po(client, org_id, deps_inactive, farm_id=str(deps_inactive["farm"]))
    ).status_code == 404
    inactive_item = await _post_po(client, org_id, deps_inactive)
    assert inactive_item.status_code == 404


async def test_patch_line_boundaries_noop_and_failed_rollback(client: AsyncClient) -> None:
    _email, org_id = await _owner_org(client)
    deps = await _dependencies(org_id)
    po = await _create_po(client, org_id, deps)
    line = po["lines"][0]

    noop = await client.patch(
        f"/api/v1/purchase-orders/{po['id']}",
        json={"expected_version": 1, "notes": None},
    )
    assert noop.status_code == 200
    assert noop.json()["version"] == 1

    baseline_po, baseline_lines = await _fresh_po(po["id"])
    async with _db.AsyncSessionLocal() as session:
        audit_before = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(AuditEvent)
                    .where(AuditEvent.entity_id == po["id"])
                )
            ).scalar_one()
        )
        transitions_before = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(PurchaseOrderTransition)
                    .where(PurchaseOrderTransition.purchase_order_id == UUID(po["id"]))
                )
            ).scalar_one()
        )

    duplicate = await client.patch(
        f"/api/v1/purchase-orders/{po['id']}",
        json={
            "expected_version": 1,
            "notes": "must roll back",
            "lines": [
                {**_line(deps["item"]), "id": line["id"]},
                {**_line(deps["item"]), "id": line["id"]},
            ],
        },
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "duplicate_line_id"

    unknown = await client.patch(
        f"/api/v1/purchase-orders/{po['id']}",
        json={
            "expected_version": 1,
            "lines": [{**_line(deps["item"]), "id": str(uuid4())}],
        },
    )
    assert unknown.status_code == 409
    assert unknown.json()["detail"]["code"] == "unknown_line_id"

    current, current_lines = await _fresh_po(po["id"])
    assert current.version == baseline_po.version == 1
    assert current.notes == baseline_po.notes
    assert [(row.id, row.ordered_quantity) for row in current_lines] == [
        (row.id, row.ordered_quantity) for row in baseline_lines
    ]
    async with _db.AsyncSessionLocal() as session:
        assert (
            int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(AuditEvent)
                        .where(AuditEvent.entity_id == po["id"])
                    )
                ).scalar_one()
            )
            == audit_before
        )
        assert (
            int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(PurchaseOrderTransition)
                        .where(PurchaseOrderTransition.purchase_order_id == UUID(po["id"]))
                    )
                ).scalar_one()
            )
            == transitions_before
        )

    line_only = await client.patch(
        f"/api/v1/purchase-orders/{po['id']}",
        json={
            "expected_version": 1,
            "lines": [{**_line(deps["item"], quantity="12.000000"), "id": line["id"]}],
        },
    )
    assert line_only.status_code == 200
    assert line_only.json()["version"] == 2
    assert line_only.json()["lines"][0]["id"] == line["id"]
    assert line_only.json()["lines"][0]["ordered_quantity"] == "12.000000"

    submitted = await client.post(f"/api/v1/purchase-orders/{po['id']}/submit")
    assert submitted.status_code == 200
    immutable = await client.patch(
        f"/api/v1/purchase-orders/{po['id']}",
        json={"expected_version": 3, "notes": "forbidden"},
    )
    assert immutable.status_code == 409
    assert immutable.json()["detail"]["code"] == "invalid_purchase_order_transition"
    final, _ = await _fresh_po(po["id"])
    assert final.status == PurchaseOrderStatus.SUBMITTED
    assert final.version == 3
    assert final.notes is None


async def test_list_http_filters_and_real_cursor_traversal(client: AsyncClient) -> None:
    _email, org_id = await _owner_org(client)
    deps_a = await _dependencies(org_id)
    deps_b = await _dependencies(org_id)
    created: list[dict] = []
    for index, (deps, order_offset, delivery_offset, reference) in enumerate(
        (
            (deps_a, 0, 3, "ALPHA-REF"),
            (deps_b, 1, 4, "BETA-REF"),
            (deps_a, 2, 5, "GAMMA-REF"),
        )
    ):
        response = await _post_po(
            client,
            org_id,
            deps,
            order_date=(TODAY + timedelta(days=order_offset)).isoformat(),
            expected_delivery_date=(TODAY + timedelta(days=delivery_offset)).isoformat(),
            supplier_reference=reference,
            farm_id=str(deps_a["farm"]) if index != 1 else None,
        )
        assert response.status_code == 201, response.text
        created.append(response.json())

    by_partner = await client.get(
        f"/api/v1/organizations/{org_id}/purchase-orders",
        params={"business_partner_id": str(deps_b["partner"])},
    )
    assert [row["id"] for row in by_partner.json()["items"]] == [created[1]["id"]]

    repeated = await client.get(
        f"/api/v1/organizations/{org_id}/purchase-orders",
        params=[("status", "DRAFT"), ("status", "REJECTED")],
    )
    assert {row["id"] for row in repeated.json()["items"]} == {row["id"] for row in created}

    ranges = (
        (
            "order_date_from",
            (TODAY + timedelta(days=1)).isoformat(),
            {created[1]["id"], created[2]["id"]},
        ),
        (
            "order_date_to",
            (TODAY + timedelta(days=1)).isoformat(),
            {created[0]["id"], created[1]["id"]},
        ),
        (
            "expected_delivery_from",
            (TODAY + timedelta(days=4)).isoformat(),
            {created[1]["id"], created[2]["id"]},
        ),
        (
            "expected_delivery_to",
            (TODAY + timedelta(days=4)).isoformat(),
            {created[0]["id"], created[1]["id"]},
        ),
    )
    for key, value, expected_ids in ranges:
        response = await client.get(
            f"/api/v1/organizations/{org_id}/purchase-orders", params={key: value}
        )
        assert {row["id"] for row in response.json()["items"]} == expected_ids

    async with _db.AsyncSessionLocal() as session:
        supplier_code = (await session.get(BusinessPartner, deps_b["partner"])).code
    for search, expected_id in (
        ("BETA-REF", created[1]["id"]),
        (supplier_code, created[1]["id"]),
        ("Frozen Supplier Ltd", None),
    ):
        response = await client.get(
            f"/api/v1/organizations/{org_id}/purchase-orders", params={"search": search}
        )
        ids = {row["id"] for row in response.json()["items"]}
        if expected_id is not None:
            assert ids == {expected_id}
        else:
            assert ids

    farm_filtered = await client.get(
        f"/api/v1/organizations/{org_id}/purchase-orders",
        params={"farm_id": str(deps_a["farm"])},
    )
    assert {row["id"] for row in farm_filtered.json()["items"]} == {
        created[0]["id"],
        created[2]["id"],
    }

    seen: list[str] = []
    cursor = None
    while True:
        params = {"limit": 1}
        if cursor is not None:
            params["cursor"] = cursor
        page = await client.get(f"/api/v1/organizations/{org_id}/purchase-orders", params=params)
        assert page.status_code == 200
        items = page.json()["items"]
        assert len(items) == 1
        seen.append(items[0]["id"])
        cursor = page.json()["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == 3
    assert set(seen) == {row["id"] for row in created}

    default_page = await client.get(f"/api/v1/organizations/{org_id}/purchase-orders")
    assert len(default_page.json()["items"]) == 3
    too_large = await client.get(
        f"/api/v1/organizations/{org_id}/purchase-orders", params={"limit": 201}
    )
    assert too_large.status_code == 422


async def test_role_permission_boundaries_and_wildcard_self_approval(client: AsyncClient) -> None:
    owner, org_id = await _owner_org(client)
    deps = await _dependencies(org_id)
    owner_po = await _create_po(client, org_id, deps, farm=True)

    viewer = f"po-viewer-denials-{uuid4().hex[:8]}@agrovix.dev"
    await create_verified_user(viewer)
    await _assign_role(viewer, org_id, "viewer", None)
    await switch_user(client, viewer)
    assert (await client.get(f"/api/v1/purchase-orders/{owner_po['id']}")).status_code == 200
    denied = (
        ("PATCH", f"/api/v1/purchase-orders/{owner_po['id']}", {"expected_version": 1}),
        ("POST", f"/api/v1/purchase-orders/{owner_po['id']}/submit", None),
        ("POST", f"/api/v1/purchase-orders/{owner_po['id']}/withdraw", {"reason": "x"}),
        ("POST", f"/api/v1/purchase-orders/{owner_po['id']}/approve", None),
        ("POST", f"/api/v1/purchase-orders/{owner_po['id']}/reject", {"reason": "x"}),
        ("POST", f"/api/v1/purchase-orders/{owner_po['id']}/revise", {"reason": "x"}),
        ("POST", f"/api/v1/purchase-orders/{owner_po['id']}/cancel", {"reason": "x"}),
    )
    for method, path, body in denied:
        response = await client.request(method, path, json=body)
        assert response.status_code == 403, (method, path, response.text)
    assert (await _post_po(client, org_id, deps)).status_code == 403

    director = f"po-director-{uuid4().hex[:8]}@agrovix.dev"
    await create_verified_user(director)
    await _assign_role(director, org_id, "farm_director", None)
    await switch_user(client, director)
    director_po = await _create_po(client, org_id, deps)
    assert (
        await client.patch(
            f"/api/v1/purchase-orders/{director_po['id']}",
            json={"expected_version": 1, "notes": "director"},
        )
    ).status_code == 200
    assert (
        await client.post(f"/api/v1/purchase-orders/{director_po['id']}/submit")
    ).status_code == 200
    assert (
        await client.post(
            f"/api/v1/purchase-orders/{director_po['id']}/reject", json={"reason": "director"}
        )
    ).status_code == 200

    wildcard = f"po-wildcard-{uuid4().hex[:8]}@agrovix.dev"
    await create_verified_user(wildcard)
    wildcard_id = await _set_superuser(wildcard)
    await switch_user(client, wildcard)
    wildcard_po = await _create_po(client, org_id, deps)
    assert wildcard_po["created_by_id"] == str(wildcard_id)
    assert (
        await client.post(f"/api/v1/purchase-orders/{wildcard_po['id']}/submit")
    ).status_code == 200
    self_approve = await client.post(f"/api/v1/purchase-orders/{wildcard_po['id']}/approve")
    assert self_approve.status_code == 409
    assert self_approve.json()["detail"]["code"] == "purchase_order_self_approval_forbidden"
    current, _ = await _fresh_po(wildcard_po["id"])
    assert current.status == PurchaseOrderStatus.SUBMITTED
    assert current.approved_by_id is None
    await switch_user(client, owner)


async def test_all_cancellable_states_bind_to_cancel_http_route(client: AsyncClient) -> None:
    owner, org_id = await _owner_org(client)
    deps = await _dependencies(org_id)
    approver = f"po-cancel-approver-{uuid4().hex[:8]}@agrovix.dev"
    await create_verified_user(approver)
    await _set_superuser(approver)

    for source in (
        PurchaseOrderStatus.DRAFT,
        PurchaseOrderStatus.SUBMITTED,
        PurchaseOrderStatus.REJECTED,
        PurchaseOrderStatus.APPROVED,
    ):
        await switch_user(client, owner)
        po = await _create_po(client, org_id, deps)
        if source != PurchaseOrderStatus.DRAFT:
            assert (
                await client.post(f"/api/v1/purchase-orders/{po['id']}/submit")
            ).status_code == 200
        if source == PurchaseOrderStatus.REJECTED:
            assert (
                await client.post(
                    f"/api/v1/purchase-orders/{po['id']}/reject",
                    json={"reason": "reject before cancel"},
                )
            ).status_code == 200
        if source == PurchaseOrderStatus.APPROVED:
            await switch_user(client, approver)
            assert (
                await client.post(f"/api/v1/purchase-orders/{po['id']}/approve")
            ).status_code == 200
            await switch_user(client, owner)

        cancelled = await client.post(
            f"/api/v1/purchase-orders/{po['id']}/cancel",
            json={"reason": f"cancel {source.value.lower()}"},
        )
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["status"] == "CANCELLED"
        assert cancelled.json()["cancelled_by_id"] == po["created_by_id"]
        assert cancelled.json()["cancelled_at"] is not None


@pytest.mark.parametrize("accumulator", ["received_quantity", "received_quantity_canonical"])
async def test_cancel_http_route_guards_each_received_accumulator(
    client: AsyncClient, accumulator: str
) -> None:
    owner, org_id = await _owner_org(client)
    deps = await _dependencies(org_id)
    po = await _create_po(client, org_id, deps)
    await client.post(f"/api/v1/purchase-orders/{po['id']}/submit")
    approver = f"po-receipt-guard-{uuid4().hex[:8]}@agrovix.dev"
    await create_verified_user(approver)
    await _set_superuser(approver)
    await switch_user(client, approver)
    assert (await client.post(f"/api/v1/purchase-orders/{po['id']}/approve")).status_code == 200

    async with _db.AsyncSessionLocal() as session:
        line = (
            await session.execute(
                select(PurchaseOrderLine).where(
                    PurchaseOrderLine.purchase_order_id == UUID(po["id"])
                )
            )
        ).scalar_one()
        setattr(line, accumulator, Decimal("1.000000"))
        await session.commit()

    await switch_user(client, owner)
    blocked = await client.post(
        f"/api/v1/purchase-orders/{po['id']}/cancel", json={"reason": "must remain approved"}
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "purchase_order_has_receipts"
    current, lines = await _fresh_po(po["id"])
    assert current.status == PurchaseOrderStatus.APPROVED
    assert current.cancelled_by_id is None
    assert current.cancelled_at is None
    assert getattr(lines[0], accumulator) == Decimal("1.000000")
    async with _db.AsyncSessionLocal() as session:
        cancel_audits = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(AuditEvent)
                    .where(
                        AuditEvent.entity_id == po["id"],
                        AuditEvent.action == "purchase_order.cancel",
                    )
                )
            ).scalar_one()
        )
        cancel_transitions = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(PurchaseOrderTransition)
                    .where(
                        PurchaseOrderTransition.purchase_order_id == UUID(po["id"]),
                        PurchaseOrderTransition.to_status == PurchaseOrderStatus.CANCELLED,
                    )
                )
            ).scalar_one()
        )
    assert cancel_audits == cancel_transitions == 0


async def test_submit_replay_has_no_duplicate_database_side_effects(client: AsyncClient) -> None:
    _email, org_id = await _owner_org(client)
    deps = await _dependencies(org_id)
    po = await _create_po(client, org_id, deps)

    first = await client.post(f"/api/v1/purchase-orders/{po['id']}/submit")
    assert first.status_code == 200
    assert first.headers.get("X-Idempotent-Replay") is None
    first_body = first.json()
    replay = await client.post(f"/api/v1/purchase-orders/{po['id']}/submit")
    assert replay.status_code == 200
    assert replay.headers["X-Idempotent-Replay"] == "true"
    assert replay.json()["version"] == first_body["version"] == 2
    assert replay.json()["submitted_by_id"] == first_body["submitted_by_id"]
    assert replay.json()["submitted_at"] == first_body["submitted_at"]

    async with _db.AsyncSessionLocal() as session:
        transitions = list(
            (
                await session.execute(
                    select(PurchaseOrderTransition)
                    .where(PurchaseOrderTransition.purchase_order_id == UUID(po["id"]))
                    .order_by(PurchaseOrderTransition.occurred_at, PurchaseOrderTransition.id)
                )
            )
            .scalars()
            .all()
        )
        audits = list(
            (await session.execute(select(AuditEvent).where(AuditEvent.entity_id == po["id"])))
            .scalars()
            .all()
        )
        current = await session.get(PurchaseOrder, UUID(po["id"]))
    assert [row.metadata_json["operation"] for row in transitions] == ["create", "submit"]
    assert [row.actor_id for row in transitions] == [UUID(po["created_by_id"])] * 2
    assert sorted(row.action for row in audits) == [
        "purchase_order.create",
        "purchase_order.submit",
        "purchase_order.transition",
    ]
    assert current.version == 2
    assert current.submitted_by_id == UUID(first_body["submitted_by_id"])
    assert current.submitted_at.isoformat().replace("+00:00", "Z") == first_body["submitted_at"]


async def test_transition_history_fields_order_pagination_and_hiding(client: AsyncClient) -> None:
    owner_a, org_a = await _owner_org(client)
    deps_a = await _dependencies(org_a)
    po = await _create_po(client, org_a, deps_a, farm=True)
    await client.post(f"/api/v1/purchase-orders/{po['id']}/submit")
    await client.post(
        f"/api/v1/purchase-orders/{po['id']}/withdraw", json={"reason": "correct lines"}
    )

    async with _db.AsyncSessionLocal() as session:
        authoritative = list(
            (
                await session.execute(
                    select(PurchaseOrderTransition)
                    .where(PurchaseOrderTransition.purchase_order_id == UUID(po["id"]))
                    .order_by(PurchaseOrderTransition.occurred_at, PurchaseOrderTransition.id)
                )
            )
            .scalars()
            .all()
        )

    seen: list[dict] = []
    cursor = None
    while True:
        params = {"limit": 1}
        if cursor:
            params["cursor"] = cursor
        response = await client.get(
            f"/api/v1/purchase-orders/{po['id']}/transitions", params=params
        )
        assert response.status_code == 200
        seen.extend(response.json()["items"])
        cursor = response.json()["next_cursor"]
        if cursor is None:
            break

    assert [row["id"] for row in seen] == [str(row.id) for row in authoritative]
    for response_row, db_row in zip(seen, authoritative, strict=True):
        assert response_row["actor_id"] == str(db_row.actor_id)
        assert response_row["from_status"] == (
            db_row.from_status.value if db_row.from_status else None
        )
        assert response_row["to_status"] == db_row.to_status.value
        assert response_row["operation"] == db_row.metadata_json["operation"]
        assert response_row["reason"] == db_row.reason
        assert response_row["occurred_at"] is not None

    _owner_b, org_b = await _owner_org(client)
    assert org_b != org_a
    hidden = await client.get(f"/api/v1/purchase-orders/{po['id']}/transitions")
    assert hidden.status_code == 404
    await switch_user(client, owner_a)

    other_farm = await _dependencies(org_a)
    scoped = f"po-transition-scope-{uuid4().hex[:8]}@agrovix.dev"
    await create_verified_user(scoped)
    await _assign_role(scoped, org_a, "supervisor", other_farm["farm"])
    await switch_user(client, scoped)
    farm_hidden = await client.get(f"/api/v1/purchase-orders/{po['id']}/transitions")
    assert farm_hidden.status_code == 404
