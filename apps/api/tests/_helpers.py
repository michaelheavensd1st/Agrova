"""Shared helpers for tenancy/farm/audit test suites.

Keeping the setup boilerplate in one place makes the individual tests
easier to read and prevents drift between test files.
"""

from __future__ import annotations

import hashlib
from datetime import UTC as _UTC
from datetime import datetime as _dt
from uuid import UUID, uuid4

from httpx import AsyncClient
from sqlalchemy import select

from app.core.config import get_settings
from app.core.security import create_token, hash_password
from app.models.invitation import Invitation
from app.models.user import User

DEFAULT_PW = "Sprint0ne!2026"


async def create_verified_user(email: str, password: str = DEFAULT_PW) -> User:
    from app.db import session as _db

    async with _db.AsyncSessionLocal() as session:
        user = User(
            email=email.lower(),
            hashed_password=hash_password(password),
            full_name="Test User",
            is_active=True,
            is_verified=True,
        )
        session.add(user)
        await session.commit()
        return user


async def login(client: AsyncClient, email: str, password: str = DEFAULT_PW) -> dict[str, str]:
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    settings = get_settings()
    return {
        settings.cookie_access_name: r.cookies.get(settings.cookie_access_name),
        settings.cookie_refresh_name: r.cookies.get(settings.cookie_refresh_name),
    }


async def switch_user(client: AsyncClient, email: str) -> None:
    client.cookies.clear()
    client.cookies.update(await login(client, email))


async def create_org(client: AsyncClient, name: str = "Tenant Co", slug: str | None = None) -> str:
    slug = slug or f"org-{uuid4().hex[:8]}"
    r = await client.post("/api/v1/organizations", json={"name": name, "slug": slug})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def create_farm(
    client: AsyncClient, org_id: str, name: str = "Farm A", code: str | None = None
) -> str:
    code = code or f"F-{uuid4().hex[:6]}"
    r = await client.post(
        f"/api/v1/organizations/{org_id}/farms",
        json={"name": name, "code": code},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def invite_and_accept(
    client: AsyncClient,
    *,
    inviter_email: str,
    invitee_email: str,
    org_id: str,
    role_name: str,
    farm_id: str | None = None,
) -> None:
    """Full invite → mint synthetic token → accept flow.

    The dev EmailSender only logs the raw token, so tests rewrite the
    stored token_hash to a value they own (this is the same trick used
    by ``test_tenancy.py``).
    """
    # Inviter creates invitation
    await switch_user(client, inviter_email)
    r = await client.post(
        f"/api/v1/organizations/{org_id}/invitations",
        json={
            "email": invitee_email,
            "role_name": role_name,
            **({"farm_id": farm_id} if farm_id else {}),
        },
    )
    assert r.status_code == 201, r.text
    invitation_id = UUID(r.json()["id"])

    # Rewrite the stored token to a known raw token.
    from app.db import session as _db

    raw_token, _ = create_token(
        subject=uuid4(),
        token_type="invite",
        extra_claims={"org_id": org_id, "email": invitee_email.lower()},
    )
    async with _db.AsyncSessionLocal() as session:
        inv = (
            await session.execute(select(Invitation).where(Invitation.id == invitation_id))
        ).scalar_one()
        inv.token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        session.add(inv)
        await session.commit()

    # Invitee accepts
    await switch_user(client, invitee_email)
    r = await client.post("/api/v1/invitations/accept", json={"token": raw_token})
    assert r.status_code == 200, r.text


# --------------------------------------------------------------------- #
# Sprint 3 — canonical valid payload builders (aquaculture slice 01)
#
# Tests that exercise the event pipeline should use these instead of
# hand-crafting dicts. This keeps every suite aligned with the
# EventCatalog schema; changes there ripple through automatically.
# --------------------------------------------------------------------- #
def _now_iso() -> str:
    return _dt.now(_UTC).isoformat()


def stocking_payload(
    *,
    quantity: int = 10_000,
    species_code: str = "WHITE_SHRIMP",
    average_weight: float = 0.2,
    weight_unit: str = "g",
    source: str = "Central Hatchery",
    notes: str | None = None,
) -> dict:
    return {
        "species_code": species_code,
        "quantity": quantity,
        "average_weight": average_weight,
        "weight_unit": weight_unit,
        "source": source,
        "stocked_at": _now_iso(),
        **({"notes": notes} if notes else {}),
    }


def feeding_payload(
    *,
    quantity: float = 2.5,
    unit: str = "kg",
    feed_description: str = "Grower crumble 35%",
    feeding_method: str = "broadcast",
    feeding_round: int | None = 1,
) -> dict:
    body: dict = {
        "feed_description": feed_description,
        "quantity": quantity,
        "unit": unit,
        "feeding_method": feeding_method,
    }
    if feeding_round is not None:
        body["feeding_round"] = feeding_round
    return body


def mortality_payload(
    *,
    count: int = 10,
    suspected_cause: str | None = "low DO overnight",
    disposal_method: str | None = "burial",
) -> dict:
    body: dict = {"count": count, "observed_at": _now_iso()}
    if suspected_cause is not None:
        body["suspected_cause"] = suspected_cause
    if disposal_method is not None:
        body["disposal_method"] = disposal_method
    return body


def sampling_payload(
    *,
    sample_size: int = 30,
    average_weight: float = 4.8,
    minimum_weight: float | None = 3.9,
    maximum_weight: float | None = 5.7,
    weight_unit: str = "g",
    estimated_population: int | None = None,
) -> dict:
    body: dict = {
        "sample_size": sample_size,
        "average_weight": average_weight,
        "weight_unit": weight_unit,
    }
    if minimum_weight is not None:
        body["minimum_weight"] = minimum_weight
    if maximum_weight is not None:
        body["maximum_weight"] = maximum_weight
    if estimated_population is not None:
        body["estimated_population"] = estimated_population
    return body


def water_quality_payload(
    *,
    temperature: float | None = 29.4,
    ph: float | None = 7.9,
    dissolved_oxygen: float | None = 5.6,
    ammonia: float | None = 0.15,
    nitrite: float | None = 0.02,
    turbidity: float | None = 42,
) -> dict:
    return {
        "temperature": temperature,
        "ph": ph,
        "dissolved_oxygen": dissolved_oxygen,
        "ammonia": ammonia,
        "nitrite": nitrite,
        "turbidity": turbidity,
        "measurement_units": {
            "temperature": "C",
            "dissolved_oxygen": "mg_l",
            "ammonia": "mg_l",
            "nitrite": "mg_l",
            "turbidity": "NTU",
        },
        "measured_at": _now_iso(),
    }


def transfer_payload(
    *,
    source_unit_id: str,
    destination_unit_id: str,
    destination_batch_id: str | None = None,
    quantity: int = 100,
    average_weight: float | None = 2.8,
    weight_unit: str = "g",
    transfer_loss: int = 0,
) -> dict:
    body: dict = {
        "source_unit_id": source_unit_id,
        "destination_unit_id": destination_unit_id,
        "destination_batch_id": destination_batch_id or str(uuid4()),
        "quantity": quantity,
        "weight_unit": weight_unit,
        "transfer_loss": transfer_loss,
        "transferred_at": _now_iso(),
    }
    if average_weight is not None:
        body["average_weight"] = average_weight
    return body


def harvest_payload(
    *,
    quantity: int = 9500,
    total_weight: float = 1200.0,
    average_weight: float | None = 0.126,
    weight_unit: str = "kg",
    harvest_type: str = "total",
    is_final: bool = True,
) -> dict:
    body: dict = {
        "quantity": quantity,
        "total_weight": total_weight,
        "weight_unit": weight_unit,
        "harvest_type": harvest_type,
        "is_final": is_final,
        "harvested_at": _now_iso(),
    }
    if average_weight is not None:
        body["average_weight"] = average_weight
    return body
