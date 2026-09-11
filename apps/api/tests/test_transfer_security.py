"""Security regressions for batch-scoped aquaculture TRANSFER destinations."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.db import session as db_session_module
from app.models.membership import FarmMembership
from app.models.production import ProductionSite, ProductionUnit
from app.models.role import Permission, Role, RoleScope
from app.models.role_assignment import RoleAssignment
from app.models.user import User
from tests._helpers import (
    create_farm,
    create_verified_user,
    invite_and_accept,
    switch_user,
    transfer_payload,
)
from tests.test_codex_review_gate_02 import _prepare_active_batch, _prepare_receiving_batch
from tests.test_production_engine import _create_unit

pytestmark = pytest.mark.asyncio


async def _destination(client: AsyncClient, site_id: str, unit_type_id: str) -> str:
    return str(await _create_unit(client, site_id, unit_type_id))


async def _site(client: AsyncClient, farm_id: str, label: str) -> str:
    response = await client.post(
        f"/api/v1/farms/{farm_id}/sites",
        json={"name": label, "code": f"{label[:2].upper()}-{uuid4().hex[:6]}"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _post_transfer(
    client: AsyncClient,
    *,
    batch_id: str,
    source_id: str,
    destination_id: str,
    destination_batch_id: str | None = None,
):
    return await client.post(
        f"/api/v1/batches/{batch_id}/events",
        json={
            "event_type": "TRANSFER",
            "data": transfer_payload(
                source_unit_id=source_id,
                destination_unit_id=destination_id,
                destination_batch_id=destination_batch_id,
                quantity=1,
            ),
        },
    )


async def _assign_event_create_only_role(email: str, org_id: str, farm_id: str) -> None:
    async with db_session_module.AsyncSessionLocal() as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        permission = (
            await session.execute(
                select(Permission).where(Permission.code == "production_event.create")
            )
        ).scalar_one()
        role = Role(
            name=f"transfer-event-only-{uuid4().hex}",
            description="Test-only TRANSFER event creator",
            scope=RoleScope.FARM,
            permissions=[permission],
        )
        session.add(role)
        await session.flush()
        assignments = (
            await session.execute(
                select(RoleAssignment).where(
                    RoleAssignment.user_id == user.id,
                    RoleAssignment.organization_id == UUID(org_id),
                )
            )
        ).scalars()
        for assignment in assignments:
            await session.delete(assignment)
        session.add(
            RoleAssignment(
                user_id=user.id,
                role_id=role.id,
                organization_id=UUID(org_id),
                farm_id=UUID(farm_id),
            )
        )
        session.add(FarmMembership(user_id=user.id, farm_id=UUID(farm_id)))
        await session.commit()


async def test_event_create_only_caller_can_discover_and_transfer(client: AsyncClient) -> None:
    ctx = await _prepare_active_batch(client, quantity=10)
    destination_id = await _destination(client, ctx["site_id"], ctx["unit_type_id"])
    destination_batch_id = await _prepare_receiving_batch(client, destination_id)
    operator = f"transfer-only-{uuid4().hex[:8]}@agrovix.dev"
    await create_verified_user(operator)
    await invite_and_accept(
        client,
        inviter_email=ctx["owner"],
        invitee_email=operator,
        org_id=ctx["org_id"],
        role_name="viewer",
    )
    await _assign_event_create_only_role(operator, ctx["org_id"], ctx["farm_id"])

    discovery = await client.get(f"/api/v1/batches/{ctx['batch_id']}/transfer-destinations")
    assert discovery.status_code == 200, discovery.text
    assert [row["id"] for row in discovery.json()] == [destination_batch_id]
    assert discovery.json()[0]["unit_id"] == destination_id
    assert discovery.json()[0]["label"]

    response = await _post_transfer(
        client,
        batch_id=ctx["batch_id"],
        source_id=ctx["unit_id"],
        destination_id=destination_id,
        destination_batch_id=destination_batch_id,
    )
    assert response.status_code == 201, response.text


async def test_random_cross_farm_and_cross_org_destinations_are_indistinguishable(
    client: AsyncClient,
) -> None:
    source = await _prepare_active_batch(client, quantity=10)
    owner = source["owner"]

    farm_2 = await create_farm(client, source["org_id"], name="Other farm")
    site_2 = await _site(client, farm_2, "Other site")
    cross_farm = await _destination(client, site_2, source["unit_type_id"])

    other = await _prepare_active_batch(client, quantity=10)
    cross_org = other["unit_id"]
    await switch_user(client, owner)

    responses = [
        await _post_transfer(
            client,
            batch_id=source["batch_id"],
            source_id=source["unit_id"],
            destination_id=destination_id,
        )
        for destination_id in (str(uuid4()), cross_farm, cross_org)
    ]
    assert {response.status_code for response in responses} == {422}
    details = [response.json()["detail"] for response in responses]
    assert details == [details[0]] * len(details)
    assert details[0] == {
        "code": "transfer_destination_ineligible",
        "message": "The selected destination is not eligible for this transfer.",
    }
    combined = " ".join(response.text for response in responses)
    assert cross_farm not in combined
    assert cross_org not in combined
    assert source["farm_id"] not in combined
    assert farm_2 not in combined


@pytest.mark.parametrize(
    ("resource_kind", "resource_status"),
    [
        ("unit", "maintenance"),
        ("unit", "closed"),
        ("site", "maintenance"),
        ("site", "closed"),
    ],
)
async def test_inactive_destination_has_the_same_safe_error(
    client: AsyncClient, resource_kind: str, resource_status: str
) -> None:
    ctx = await _prepare_active_batch(client, quantity=10)
    destination_site = await _site(client, ctx["farm_id"], "Destination")
    destination_id = await _destination(client, destination_site, ctx["unit_type_id"])
    response = await client.patch(
        (
            f"/api/v1/units/{destination_id}"
            if resource_kind == "unit"
            else f"/api/v1/sites/{destination_site}"
        ),
        json={"status": resource_status},
    )
    assert response.status_code == 200, response.text

    transfer = await _post_transfer(
        client,
        batch_id=ctx["batch_id"],
        source_id=ctx["unit_id"],
        destination_id=destination_id,
    )
    assert transfer.status_code == 422
    assert transfer.json()["detail"]["code"] == "transfer_destination_ineligible"
    assert resource_status not in transfer.text.lower()


@pytest.mark.parametrize("deleted_resource", ["unit", "site"])
async def test_deleted_destination_has_the_same_safe_error(
    client: AsyncClient, deleted_resource: str
) -> None:
    ctx = await _prepare_active_batch(client, quantity=10)
    destination_site = await _site(client, ctx["farm_id"], "Destination")
    destination_id = await _destination(client, destination_site, ctx["unit_type_id"])
    async with db_session_module.AsyncSessionLocal() as session:
        if deleted_resource == "unit":
            resource = await session.get(ProductionUnit, UUID(destination_id))
        else:
            resource = await session.get(ProductionSite, UUID(destination_site))
        assert resource is not None
        resource.deleted_at = datetime.now(UTC)
        await session.commit()

    transfer = await _post_transfer(
        client,
        batch_id=ctx["batch_id"],
        source_id=ctx["unit_id"],
        destination_id=destination_id,
    )
    assert transfer.status_code == 422
    assert transfer.json()["detail"] == {
        "code": "transfer_destination_ineligible",
        "message": "The selected destination is not eligible for this transfer.",
    }


async def test_source_mismatch_error_contains_no_identifiers(client: AsyncClient) -> None:
    ctx = await _prepare_active_batch(client, quantity=10)
    supplied_source = str(uuid4())
    supplied_destination = str(uuid4())
    response = await _post_transfer(
        client,
        batch_id=ctx["batch_id"],
        source_id=supplied_source,
        destination_id=supplied_destination,
    )
    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "transfer_source_changed",
        "message": "The batch's source unit changed. Refresh and try again.",
    }
    assert supplied_source not in response.text
    assert supplied_destination not in response.text
    assert ctx["unit_id"] not in response.text


async def test_discovery_is_403_without_permission_and_404_for_non_member(
    client: AsyncClient,
) -> None:
    ctx = await _prepare_active_batch(client, quantity=10)
    viewer = f"transfer-viewer-{uuid4().hex[:8]}@agrovix.dev"
    await create_verified_user(viewer)
    await invite_and_accept(
        client,
        inviter_email=ctx["owner"],
        invitee_email=viewer,
        org_id=ctx["org_id"],
        role_name="viewer",
    )
    forbidden = await client.get(f"/api/v1/batches/{ctx['batch_id']}/transfer-destinations")
    assert forbidden.status_code == 403

    outsider = f"transfer-outsider-{uuid4().hex[:8]}@agrovix.dev"
    await create_verified_user(outsider)
    await switch_user(client, outsider)
    not_found = await client.get(f"/api/v1/batches/{ctx['batch_id']}/transfer-destinations")
    assert not_found.status_code == 404
