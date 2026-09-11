"""Production Engine HTTP endpoints.

Kept in a single router module so the whole Sprint 2 bounded context
lives together. Sub-routers by resource keep the URL surface tidy:

    /api/v1/farms/{farm_id}/sites            POST | GET
    /api/v1/sites/{site_id}                  GET | PATCH | DELETE
    /api/v1/sites/{site_id}/restore          POST
    /api/v1/sites/{site_id}/units            POST | GET

    /api/v1/organizations/{org_id}/unit-types POST
    /api/v1/production-unit-types            GET

    /api/v1/units/{unit_id}                  GET | PATCH | DELETE
    /api/v1/units/{unit_id}/batches          POST | GET

    /api/v1/batches/{batch_id}               GET | PATCH
    /api/v1/batches/{batch_id}/transitions   POST | GET
    /api/v1/batches/{batch_id}/events        POST | GET

    /api/v1/production-events/catalog        GET (authenticated — reference data)
    /api/v1/events/{event_id}                GET
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status

from app.deps import (
    CurrentFarm,
    CurrentUser,
    DBSession,
    RequestCtx,
    get_audit_repository,
    require_permission,
)
from app.models.production import (
    ProductionBatch,
    ProductionSite,
    ProductionUnit,
    ProductionUnitType,
)
from app.production.event_catalog import CATALOG
from app.repositories.audit_repo import AuditRepository
from app.repositories.production import (
    ProductionBatchRepository,
    ProductionBatchTransitionRepository,
    ProductionEventRepository,
    ProductionSiteRepository,
    ProductionUnitRepository,
    ProductionUnitTypeRepository,
)
from app.schemas.production import (
    BatchProjectionsPublic,
    ProductionBatchCreate,
    ProductionBatchPublic,
    ProductionBatchTransitionPublic,
    ProductionBatchTransitionRequest,
    ProductionBatchUpdate,
    ProductionEventCatalogEntry,
    ProductionEventCatalogResponse,
    ProductionEventCreate,
    ProductionEventPage,
    ProductionEventPublic,
    ProductionSiteCreate,
    ProductionSitePublic,
    ProductionSiteUpdate,
    ProductionUnitCreate,
    ProductionUnitPublic,
    ProductionUnitTypeCreate,
    ProductionUnitTypePublic,
    ProductionUnitUpdate,
    TransferDestinationPublic,
)
from app.security.authorize import has_permission, resolve_permissions
from app.services.production import (
    ProductionBatchService,
    ProductionEventService,
    ProductionSiteService,
    ProductionUnitService,
    ProductionUnitTypeService,
)

router = APIRouter()


# --------------------------------------------------------------------- #
# Codex Review Gate 02 — production endpoint permission enforcement.
#
# Every APE endpoint below MUST call this helper with the caller's
# resolved organization / farm scope AFTER the tenancy load has
# returned 404 for non-members. Order is critical: tenancy 404 first,
# permission 403 second — otherwise the mere shape of the response
# tells outsiders whether a resource exists in another tenant.
# --------------------------------------------------------------------- #
async def _enforce_prod_permission(
    *,
    user,
    session,
    code: str,
    organization_id: uuid.UUID | None,
    farm_id: uuid.UUID | None,
) -> None:
    """Raise 403 if ``user`` lacks ``code`` for the supplied scope.

    Callers already validated tenancy (i.e. non-members got 404) so
    this only checks the RBAC scope. Superusers bypass; the shared
    ``resolve_permissions`` helper already handles that convention.
    """
    codes = await resolve_permissions(
        session, user, organization_id=organization_id, farm_id=farm_id
    )
    if not has_permission(codes, code):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing required permission: {code}",
        )


# --------------------------------------------------------------------- #
# DI helpers (kept local so the deps module doesn't bloat)
# --------------------------------------------------------------------- #
def get_site_repo(session: DBSession) -> ProductionSiteRepository:
    return ProductionSiteRepository(session)


def get_unit_type_repo(session: DBSession) -> ProductionUnitTypeRepository:
    return ProductionUnitTypeRepository(session)


def get_unit_repo(session: DBSession) -> ProductionUnitRepository:
    return ProductionUnitRepository(session)


def get_batch_repo(session: DBSession) -> ProductionBatchRepository:
    return ProductionBatchRepository(session)


def get_transition_repo(session: DBSession) -> ProductionBatchTransitionRepository:
    return ProductionBatchTransitionRepository(session)


def get_event_repo(session: DBSession) -> ProductionEventRepository:
    return ProductionEventRepository(session)


def get_site_service(
    site_repo: Annotated[ProductionSiteRepository, Depends(get_site_repo)],
    unit_repo: Annotated[ProductionUnitRepository, Depends(get_unit_repo)],
    audit_repo: Annotated[AuditRepository, Depends(get_audit_repository)],
) -> ProductionSiteService:
    return ProductionSiteService(site_repo=site_repo, unit_repo=unit_repo, audit_repo=audit_repo)


def get_unit_type_service(
    unit_type_repo: Annotated[ProductionUnitTypeRepository, Depends(get_unit_type_repo)],
    audit_repo: Annotated[AuditRepository, Depends(get_audit_repository)],
) -> ProductionUnitTypeService:
    return ProductionUnitTypeService(unit_type_repo=unit_type_repo, audit_repo=audit_repo)


def get_unit_service(
    unit_repo: Annotated[ProductionUnitRepository, Depends(get_unit_repo)],
    unit_type_repo: Annotated[ProductionUnitTypeRepository, Depends(get_unit_type_repo)],
    site_repo: Annotated[ProductionSiteRepository, Depends(get_site_repo)],
    audit_repo: Annotated[AuditRepository, Depends(get_audit_repository)],
) -> ProductionUnitService:
    return ProductionUnitService(
        unit_repo=unit_repo,
        unit_type_repo=unit_type_repo,
        site_repo=site_repo,
        audit_repo=audit_repo,
    )


def get_batch_service(
    batch_repo: Annotated[ProductionBatchRepository, Depends(get_batch_repo)],
    transition_repo: Annotated[ProductionBatchTransitionRepository, Depends(get_transition_repo)],
    unit_repo: Annotated[ProductionUnitRepository, Depends(get_unit_repo)],
    audit_repo: Annotated[AuditRepository, Depends(get_audit_repository)],
) -> ProductionBatchService:
    return ProductionBatchService(
        batch_repo=batch_repo,
        transition_repo=transition_repo,
        unit_repo=unit_repo,
        audit_repo=audit_repo,
    )


def get_event_service(
    event_repo: Annotated[ProductionEventRepository, Depends(get_event_repo)],
    batch_repo: Annotated[ProductionBatchRepository, Depends(get_batch_repo)],
    batch_service: Annotated[ProductionBatchService, Depends(get_batch_service)],
    unit_repo: Annotated[ProductionUnitRepository, Depends(get_unit_repo)],
    site_repo: Annotated[ProductionSiteRepository, Depends(get_site_repo)],
    audit_repo: Annotated[AuditRepository, Depends(get_audit_repository)],
) -> ProductionEventService:
    return ProductionEventService(
        event_repo=event_repo,
        batch_repo=batch_repo,
        batch_service=batch_service,
        unit_repo=unit_repo,
        site_repo=site_repo,
        audit_repo=audit_repo,
    )


# --------------------------------------------------------------------- #
# Cross-tenant loaders that walk the hierarchy up to the Farm, then
# reuse ``CurrentFarm``'s tenancy check (membership + non-deleted).
# --------------------------------------------------------------------- #
async def _load_site_and_farm(
    site_id: uuid.UUID,
    user: CurrentUser,
    site_repo: ProductionSiteRepository,
    session: DBSession,
) -> tuple[ProductionSite, any]:
    from app.repositories.org_repo import (
        FarmMembershipRepository,
        FarmRepository,
        OrganizationMembershipRepository,
    )
    from app.repositories.role_repo import RoleAssignmentRepository

    site = await site_repo.get_by_id_including_deleted(site_id)
    if site is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Site not found.")
    # Reuse the exact tenancy invariants that ``get_current_farm`` enforces.
    from app.deps import get_current_farm

    farm = await get_current_farm(
        farm_id=site.farm_id,
        user=user,
        farm_repo=FarmRepository(session),
        farm_mem_repo=FarmMembershipRepository(session),
        org_mem_repo=OrganizationMembershipRepository(session),
        role_assign_repo=RoleAssignmentRepository(session),
    )
    return site, farm


async def _load_unit(
    unit_id: uuid.UUID,
    user: CurrentUser,
    session: DBSession,
) -> tuple[ProductionUnit, ProductionSite, any]:
    unit_repo = ProductionUnitRepository(session)
    site_repo = ProductionSiteRepository(session)
    unit = await unit_repo.get_by_id(unit_id)
    if unit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Production unit not found.")
    site, farm = await _load_site_and_farm(unit.site_id, user, site_repo, session)
    return unit, site, farm


async def _load_batch(
    batch_id: uuid.UUID,
    user: CurrentUser,
    session: DBSession,
) -> tuple[ProductionBatch, ProductionUnit, ProductionSite, any]:
    batch_repo = ProductionBatchRepository(session)
    batch = await batch_repo.get_by_id(batch_id)
    if batch is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Batch not found.")
    unit, site, farm = await _load_unit(batch.unit_id, user, session)
    return batch, unit, site, farm


# ===================================================================== #
# ProductionSite endpoints
# ===================================================================== #
@router.post(
    "/farms/{farm_id}/sites",
    response_model=ProductionSitePublic,
    status_code=status.HTTP_201_CREATED,
    tags=["production-sites"],
)
async def create_site(
    payload: ProductionSiteCreate,
    farm: CurrentFarm,
    user: CurrentUser,
    session: DBSession,
    request_ctx: RequestCtx,
    service: Annotated[ProductionSiteService, Depends(get_site_service)],
) -> ProductionSitePublic:
    await _enforce_prod_permission(
        user=user,
        session=session,
        code="production_site.create",
        organization_id=farm.organization_id,
        farm_id=farm.id,
    )
    site = await service.create(
        actor=user,
        farm=farm,
        data=payload.model_dump(exclude_unset=False),
        request_ctx=request_ctx,
    )
    return ProductionSitePublic.model_validate(site)


@router.get(
    "/farms/{farm_id}/sites", response_model=list[ProductionSitePublic], tags=["production-sites"]
)
async def list_sites(
    farm: CurrentFarm,
    user: CurrentUser,
    session: DBSession,
    site_repo: Annotated[ProductionSiteRepository, Depends(get_site_repo)],
) -> list[ProductionSitePublic]:
    await _enforce_prod_permission(
        user=user,
        session=session,
        code="production_site.read",
        organization_id=farm.organization_id,
        farm_id=farm.id,
    )
    rows = await site_repo.list_for_farm(farm.id)
    return [ProductionSitePublic.model_validate(r) for r in rows]


@router.get("/sites/{site_id}", response_model=ProductionSitePublic, tags=["production-sites"])
async def get_site(
    site_id: uuid.UUID,
    user: CurrentUser,
    session: DBSession,
    site_repo: Annotated[ProductionSiteRepository, Depends(get_site_repo)],
) -> ProductionSitePublic:
    site = await site_repo.get_by_id(site_id)
    if site is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Site not found.")
    # Tenancy check via farm dep.
    _site, farm = await _load_site_and_farm(site.id, user, site_repo, session)
    await _enforce_prod_permission(
        user=user,
        session=session,
        code="production_site.read",
        organization_id=farm.organization_id,
        farm_id=farm.id,
    )
    return ProductionSitePublic.model_validate(site)


@router.patch("/sites/{site_id}", response_model=ProductionSitePublic, tags=["production-sites"])
async def update_site(
    site_id: uuid.UUID,
    payload: ProductionSiteUpdate,
    user: CurrentUser,
    session: DBSession,
    site_repo: Annotated[ProductionSiteRepository, Depends(get_site_repo)],
) -> ProductionSitePublic:
    site, farm = await _load_site_and_farm(site_id, user, site_repo, session)
    if site.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Site not found.")
    await _enforce_prod_permission(
        user=user,
        session=session,
        code="production_site.update",
        organization_id=farm.organization_id,
        farm_id=farm.id,
    )
    changed = payload.model_dump(exclude_unset=True)
    # Defence-in-depth: tenancy-critical fields cannot be reassigned via
    # PATCH regardless of what the update schema might grow to accept.
    for reserved in ("farm_id", "is_default", "deleted_at"):
        changed.pop(reserved, None)
    # Codex Review Gate 02 (final) — central lifecycle helper is the
    # single source of truth for CLOSED / MAINTENANCE update policy.
    from app.production.lifecycle_policy import assert_site_update_allowed

    assert_site_update_allowed(site, changed.keys())
    # Codex Review Gate 02: a site cannot transition to CLOSED while it
    # still contains active (planned / stocked / active / suspended)
    # batches. Terminal + HARVESTED batches are fine (harvest is the
    # exit gate); the soft-delete guard uses the same rule.
    if changed.get("status") == "closed":
        from sqlalchemy import func as _func
        from sqlalchemy import select as _select

        from app.models.production import (
            ProductionBatch as _Batch,
        )
        from app.models.production import (
            ProductionBatchState as _State,
        )
        from app.models.production import (
            ProductionUnit as _Unit,
        )

        active_batches = int(
            (
                await session.execute(
                    _select(_func.count(_Batch.id))
                    .join(_Unit, _Unit.id == _Batch.unit_id)
                    .where(
                        _Unit.site_id == site.id,
                        _Batch.deleted_at.is_(None),
                        _Batch.state.notin_(
                            [_State.CLOSED, _State.CANCELLED, _State.FAILED, _State.HARVESTED]
                        ),
                    )
                )
            ).scalar_one()
        )
        if active_batches > 0:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {
                    "code": "site_close_blocked_by_active_batches",
                    "message": (
                        f"Cannot close a site with {active_batches} active batch(es). "
                        "Transfer, harvest, cancel or fail the batches first."
                    ),
                },
            )
    for k, v in changed.items():
        setattr(site, k, v)
    await session.flush()
    await session.refresh(site)
    return ProductionSitePublic.model_validate(site)


@router.delete("/sites/{site_id}", response_model=ProductionSitePublic, tags=["production-sites"])
async def delete_site(
    site_id: uuid.UUID,
    user: CurrentUser,
    session: DBSession,
    request_ctx: RequestCtx,
    site_repo: Annotated[ProductionSiteRepository, Depends(get_site_repo)],
    service: Annotated[ProductionSiteService, Depends(get_site_service)],
) -> ProductionSitePublic:
    site, farm = await _load_site_and_farm(site_id, user, site_repo, session)
    if site.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Site not found.")
    await _enforce_prod_permission(
        user=user,
        session=session,
        code="production_site.delete",
        organization_id=farm.organization_id,
        farm_id=farm.id,
    )
    # Codex Review Gate 02 follow-up — delete is a write, and CLOSED
    # is read-only until an explicit reopen. Route through the
    # central lifecycle helper so the invariant is enforced everywhere.
    from app.production.lifecycle_policy import assert_site_delete_allowed

    assert_site_delete_allowed(site)
    await service.soft_delete(actor=user, site=site, farm=farm, request_ctx=request_ctx)
    return ProductionSitePublic.model_validate(site)


@router.post(
    "/sites/{site_id}/restore", response_model=ProductionSitePublic, tags=["production-sites"]
)
async def restore_site(
    site_id: uuid.UUID,
    user: CurrentUser,
    session: DBSession,
    request_ctx: RequestCtx,
    site_repo: Annotated[ProductionSiteRepository, Depends(get_site_repo)],
    service: Annotated[ProductionSiteService, Depends(get_site_service)],
) -> ProductionSitePublic:
    site, farm = await _load_site_and_farm(site_id, user, site_repo, session)
    await _enforce_prod_permission(
        user=user,
        session=session,
        code="production_site.restore",
        organization_id=farm.organization_id,
        farm_id=farm.id,
    )
    await service.restore(actor=user, site=site, farm=farm, request_ctx=request_ctx)
    return ProductionSitePublic.model_validate(site)


# ===================================================================== #
# ProductionUnitType endpoints
# ===================================================================== #
@router.get(
    "/production-unit-types",
    response_model=list[ProductionUnitTypePublic],
    tags=["production-unit-types"],
)
async def list_unit_types(
    user: CurrentUser,
    session: DBSession,
    organization_id: uuid.UUID | None = Query(
        default=None,
        description=(
            "Restrict to a single organization the caller belongs to. "
            "If the caller is not a member of the org, custom types for "
            "that org are NOT returned (see docs/audits/codex-review-gate-01.md)."
        ),
    ),
    unit_type_repo: Annotated[ProductionUnitTypeRepository, Depends(get_unit_type_repo)] = None,  # type: ignore
) -> list[ProductionUnitTypePublic]:
    """List production unit types visible to the caller.

    Visibility rules (Codex Review Gate 01, finding CRG01-1):
    - System-owned types are always visible.
    - Org-custom types are visible ONLY when the caller is an active
      member of the owning organization.
    - The ``organization_id`` filter is validated against the caller's
      memberships; unknown / non-member org ids are silently ignored so
      that they cannot be used to probe for the existence of custom
      types in other tenants.

    Permission (Codex Review Gate 02 follow-up):
    - With ``organization_id``: membership check runs first (404 on
      non-member) then ``production_unit_type.read`` is required at
      that scope (403 otherwise).
    - Without ``organization_id``: caller must hold
      ``production_unit_type.read`` at some scope they own — pure
      authentication is not enough.
    """
    from sqlalchemy import select

    from app.models.membership import OrganizationMembership

    # --- Permission gate (tenancy 404 first, permission 403 second) --- #
    if organization_id is not None:
        # Tenancy 404 first — non-members must not learn whether the
        # organization exists. Membership check reuses the same
        # active-membership guard as the org-scoped tenant deps.
        from sqlalchemy import select as _select

        from app.models.membership import OrganizationMembership as _Mem

        if not user.is_superuser:
            mem = (
                await session.execute(
                    _select(_Mem).where(
                        _Mem.user_id == user.id,
                        _Mem.organization_id == organization_id,
                        _Mem.is_active.is_(True),
                        _Mem.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if mem is None:
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND,
                    "Organization not found.",
                )
        await _enforce_prod_permission(
            user=user,
            session=session,
            code="production_unit_type.read",
            organization_id=organization_id,
            farm_id=None,
        )
    else:
        # No org scope — require the permission through the caller's
        # existing role assignments (platform-scoped OR any
        # org-scoped grant they hold). Authentication alone is not
        # enough.
        await _enforce_prod_permission(
            user=user,
            session=session,
            code="production_unit_type.read",
            organization_id=None,
            farm_id=None,
        )

    org_ids: list[uuid.UUID]
    if user.is_superuser:
        # Superusers see all custom types.
        stmt = (
            select(ProductionUnitType.organization_id)
            .where(ProductionUnitType.organization_id.is_not(None))
            .distinct()
        )
        org_ids = [row for row in (await session.execute(stmt)).scalars().all() if row]
    else:
        mem_stmt = select(OrganizationMembership.organization_id).where(
            OrganizationMembership.user_id == user.id,
            OrganizationMembership.is_active.is_(True),
            OrganizationMembership.deleted_at.is_(None),
        )
        org_ids = list((await session.execute(mem_stmt)).scalars().all())

    # Intersect the requested filter with the caller's accessible orgs.
    # Non-members receive system types only — no leak of which orgs
    # own custom types. When no ``organization_id`` is provided the
    # endpoint returns system-only types by policy (see docstring).
    org_ids = [o for o in org_ids if o == organization_id] if organization_id is not None else []

    rows = await unit_type_repo.list_visible(organization_ids=org_ids)
    return [ProductionUnitTypePublic.model_validate(r) for r in rows]


@router.post(
    "/organizations/{organization_id}/production-unit-types",
    response_model=ProductionUnitTypePublic,
    status_code=status.HTTP_201_CREATED,
    tags=["production-unit-types"],
    dependencies=[Depends(require_permission("production_unit_type.create"))],
)
async def create_custom_unit_type(
    organization_id: uuid.UUID,
    payload: ProductionUnitTypeCreate,
    user: CurrentUser,
    request_ctx: RequestCtx,
    service: Annotated[ProductionUnitTypeService, Depends(get_unit_type_service)],
) -> ProductionUnitTypePublic:
    row = await service.create_custom(
        actor=user,
        organization_id=organization_id,
        data=payload.model_dump(),
        request_ctx=request_ctx,
    )
    return ProductionUnitTypePublic.model_validate(row)


@router.delete(
    "/production-unit-types/{type_id}",
    response_model=ProductionUnitTypePublic,
    tags=["production-unit-types"],
)
async def delete_custom_unit_type(
    type_id: uuid.UUID,
    user: CurrentUser,
    request_ctx: RequestCtx,
    session: DBSession,
    unit_type_repo: Annotated[ProductionUnitTypeRepository, Depends(get_unit_type_repo)],
    service: Annotated[ProductionUnitTypeService, Depends(get_unit_type_service)],
) -> ProductionUnitTypePublic:
    row = await unit_type_repo.get_by_id(type_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unit type not found.")
    if row.is_system:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "System unit types cannot be deleted.")
    # Only owners of the parent org can delete their custom types.
    from app.repositories.org_repo import OrganizationMembershipRepository

    if not user.is_superuser:
        mem = await OrganizationMembershipRepository(session).get(user.id, row.organization_id)
        if mem is None or not mem.is_active:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Unit type not found.")
    await _enforce_prod_permission(
        user=user,
        session=session,
        code="production_unit_type.delete",
        organization_id=row.organization_id,
        farm_id=None,
    )
    await service.delete_custom(actor=user, row=row, request_ctx=request_ctx)
    return ProductionUnitTypePublic.model_validate(row)


# ===================================================================== #
# ProductionUnit endpoints
# ===================================================================== #
@router.post(
    "/sites/{site_id}/units",
    response_model=ProductionUnitPublic,
    status_code=status.HTTP_201_CREATED,
    tags=["production-units"],
)
async def create_unit(
    site_id: uuid.UUID,
    payload: ProductionUnitCreate,
    user: CurrentUser,
    session: DBSession,
    request_ctx: RequestCtx,
    site_repo: Annotated[ProductionSiteRepository, Depends(get_site_repo)],
    service: Annotated[ProductionUnitService, Depends(get_unit_service)],
) -> ProductionUnitPublic:
    site, farm = await _load_site_and_farm(site_id, user, site_repo, session)
    await _enforce_prod_permission(
        user=user,
        session=session,
        code="production_unit.create",
        organization_id=farm.organization_id,
        farm_id=farm.id,
    )
    unit = await service.create(
        actor=user,
        site=site,
        farm=farm,
        data=payload.model_dump(),
        request_ctx=request_ctx,
    )
    return ProductionUnitPublic.model_validate(unit)


@router.get(
    "/sites/{site_id}/units", response_model=list[ProductionUnitPublic], tags=["production-units"]
)
async def list_units(
    site_id: uuid.UUID,
    user: CurrentUser,
    session: DBSession,
    site_repo: Annotated[ProductionSiteRepository, Depends(get_site_repo)],
    unit_repo: Annotated[ProductionUnitRepository, Depends(get_unit_repo)],
) -> list[ProductionUnitPublic]:
    site, farm = await _load_site_and_farm(site_id, user, site_repo, session)
    if site.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Site not found.")
    await _enforce_prod_permission(
        user=user,
        session=session,
        code="production_unit.read",
        organization_id=farm.organization_id,
        farm_id=farm.id,
    )
    rows = await unit_repo.list_for_site(site.id)
    return [ProductionUnitPublic.model_validate(r) for r in rows]


@router.get("/units/{unit_id}", response_model=ProductionUnitPublic, tags=["production-units"])
async def get_unit(
    unit_id: uuid.UUID, user: CurrentUser, session: DBSession
) -> ProductionUnitPublic:
    unit, _, farm = await _load_unit(unit_id, user, session)
    await _enforce_prod_permission(
        user=user,
        session=session,
        code="production_unit.read",
        organization_id=farm.organization_id,
        farm_id=farm.id,
    )
    return ProductionUnitPublic.model_validate(unit)


@router.patch("/units/{unit_id}", response_model=ProductionUnitPublic, tags=["production-units"])
async def update_unit(
    unit_id: uuid.UUID,
    payload: ProductionUnitUpdate,
    user: CurrentUser,
    session: DBSession,
) -> ProductionUnitPublic:
    unit, _, farm = await _load_unit(unit_id, user, session)
    await _enforce_prod_permission(
        user=user,
        session=session,
        code="production_unit.update",
        organization_id=farm.organization_id,
        farm_id=farm.id,
    )
    changed = payload.model_dump(exclude_unset=True)
    # Defence-in-depth: never allow a PATCH to relocate a unit across
    # sites or change its type — those are lifecycle events, not
    # updates.
    for reserved in ("site_id", "unit_type_id", "deleted_at"):
        changed.pop(reserved, None)
    # Codex Review Gate 02 (final) — central lifecycle helper.
    from app.production.lifecycle_policy import assert_unit_update_allowed

    assert_unit_update_allowed(unit, changed.keys())
    # Codex Review Gate 02: a unit cannot transition to CLOSED while
    # it still contains active (planned / stocked / active / suspended)
    # batches. HARVESTED batches allow close (final harvest IS the
    # exit gate); CLOSED / CANCELLED / FAILED terminal states are also
    # fine.
    if changed.get("status") == "closed":
        from sqlalchemy import func as _func
        from sqlalchemy import select as _select

        from app.models.production import (
            ProductionBatch as _Batch,
        )
        from app.models.production import (
            ProductionBatchState as _State,
        )

        active_batches = int(
            (
                await session.execute(
                    _select(_func.count(_Batch.id)).where(
                        _Batch.unit_id == unit.id,
                        _Batch.deleted_at.is_(None),
                        _Batch.state.notin_(
                            [
                                _State.CLOSED,
                                _State.CANCELLED,
                                _State.FAILED,
                                _State.HARVESTED,
                            ]
                        ),
                    )
                )
            ).scalar_one()
        )
        if active_batches > 0:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {
                    "code": "unit_close_blocked_by_active_batches",
                    "message": (
                        f"Cannot close a unit with {active_batches} active batch(es). "
                        "Transfer, harvest, cancel or fail the batches first."
                    ),
                },
            )
    for k, v in changed.items():
        setattr(unit, k, v)
    await session.flush()
    await session.refresh(unit)
    return ProductionUnitPublic.model_validate(unit)


@router.delete("/units/{unit_id}", response_model=ProductionUnitPublic, tags=["production-units"])
async def delete_unit(
    unit_id: uuid.UUID,
    user: CurrentUser,
    session: DBSession,
    request_ctx: RequestCtx,
    service: Annotated[ProductionUnitService, Depends(get_unit_service)],
) -> ProductionUnitPublic:
    unit, _, farm = await _load_unit(unit_id, user, session)
    await _enforce_prod_permission(
        user=user,
        session=session,
        code="production_unit.delete",
        organization_id=farm.organization_id,
        farm_id=farm.id,
    )
    # Codex Review Gate 02 follow-up — CLOSED unit must be reopened
    # under normal safeguards before deletion.
    from app.production.lifecycle_policy import assert_unit_delete_allowed

    assert_unit_delete_allowed(unit)
    await service.soft_delete(actor=user, unit=unit, farm=farm, request_ctx=request_ctx)
    return ProductionUnitPublic.model_validate(unit)


# ===================================================================== #
# ProductionBatch endpoints
# ===================================================================== #
@router.post(
    "/units/{unit_id}/batches",
    response_model=ProductionBatchPublic,
    status_code=status.HTTP_201_CREATED,
    tags=["production-batches"],
)
async def create_batch(
    unit_id: uuid.UUID,
    payload: ProductionBatchCreate,
    user: CurrentUser,
    session: DBSession,
    request_ctx: RequestCtx,
    service: Annotated[ProductionBatchService, Depends(get_batch_service)],
) -> ProductionBatchPublic:
    unit, site, farm = await _load_unit(unit_id, user, session)
    await _enforce_prod_permission(
        user=user,
        session=session,
        code="production_batch.create",
        organization_id=farm.organization_id,
        farm_id=farm.id,
    )
    batch = await service.create(
        actor=user,
        unit=unit,
        site=site,
        farm=farm,
        data=payload.model_dump(),
        request_ctx=request_ctx,
    )
    return ProductionBatchPublic.model_validate(batch)


@router.get(
    "/units/{unit_id}/batches",
    response_model=list[ProductionBatchPublic],
    tags=["production-batches"],
)
async def list_batches(
    unit_id: uuid.UUID,
    user: CurrentUser,
    session: DBSession,
    batch_repo: Annotated[ProductionBatchRepository, Depends(get_batch_repo)],
) -> list[ProductionBatchPublic]:
    unit, _, farm = await _load_unit(unit_id, user, session)
    await _enforce_prod_permission(
        user=user,
        session=session,
        code="production_batch.read",
        organization_id=farm.organization_id,
        farm_id=farm.id,
    )
    rows = await batch_repo.list_for_unit(unit.id)
    return [ProductionBatchPublic.model_validate(b) for b in rows]


@router.get(
    "/batches/{batch_id}", response_model=ProductionBatchPublic, tags=["production-batches"]
)
async def get_batch(
    batch_id: uuid.UUID, user: CurrentUser, session: DBSession
) -> ProductionBatchPublic:
    batch, _, _, farm = await _load_batch(batch_id, user, session)
    await _enforce_prod_permission(
        user=user,
        session=session,
        code="production_batch.read",
        organization_id=farm.organization_id,
        farm_id=farm.id,
    )
    return ProductionBatchPublic.model_validate(batch)


@router.patch(
    "/batches/{batch_id}", response_model=ProductionBatchPublic, tags=["production-batches"]
)
async def update_batch(
    batch_id: uuid.UUID,
    payload: ProductionBatchUpdate,
    user: CurrentUser,
    session: DBSession,
) -> ProductionBatchPublic:
    batch, unit, site, farm = await _load_batch(batch_id, user, session)
    await _enforce_prod_permission(
        user=user,
        session=session,
        code="production_batch.update",
        organization_id=farm.organization_id,
        farm_id=farm.id,
    )
    # Codex Review Gate 02 follow-up — batch updates are also writes;
    # parent site + unit must be ACTIVE. Delegates to the central
    # lifecycle helper so the semantics stay unified.
    from app.production.lifecycle_policy import assert_batch_update_allowed

    assert_batch_update_allowed(site, unit)
    changed = payload.model_dump(exclude_unset=True)
    # Defence-in-depth: state changes MUST go through /transitions —
    # never allow a PATCH to touch state / lifecycle timestamps even if
    # the schema is misconfigured in a future PR (see Sprint 2 code
    # review action item).
    for reserved in ("state", "stocked_at", "harvested_at", "closed_at"):
        changed.pop(reserved, None)
    for k, v in changed.items():
        setattr(batch, k, v)
    await session.flush()
    await session.refresh(batch)
    return ProductionBatchPublic.model_validate(batch)


@router.post(
    "/batches/{batch_id}/transitions",
    response_model=ProductionBatchTransitionPublic,
    tags=["production-batches"],
)
async def transition_batch(
    batch_id: uuid.UUID,
    payload: ProductionBatchTransitionRequest,
    user: CurrentUser,
    session: DBSession,
    request_ctx: RequestCtx,
    service: Annotated[ProductionBatchService, Depends(get_batch_service)],
    transition_repo: Annotated[ProductionBatchTransitionRepository, Depends(get_transition_repo)],
) -> ProductionBatchTransitionPublic:
    batch, unit, site, farm = await _load_batch(batch_id, user, session)
    await _enforce_prod_permission(
        user=user,
        session=session,
        code="production_batch.transition",
        organization_id=farm.organization_id,
        farm_id=farm.id,
    )
    await service.transition(
        actor=user,
        batch=batch,
        farm=farm,
        target_state=payload.target_state,
        reason=payload.reason,
        request_ctx=request_ctx,
        metadata=payload.metadata_json,
        site=site,
        unit=unit,
    )
    # Return the most recent transition row.
    rows = await transition_repo.list_for_batch(batch.id)
    return ProductionBatchTransitionPublic.model_validate(rows[-1])


@router.get(
    "/batches/{batch_id}/transitions",
    response_model=list[ProductionBatchTransitionPublic],
    tags=["production-batches"],
)
async def list_batch_transitions(
    batch_id: uuid.UUID,
    user: CurrentUser,
    session: DBSession,
    transition_repo: Annotated[ProductionBatchTransitionRepository, Depends(get_transition_repo)],
) -> list[ProductionBatchTransitionPublic]:
    batch, _, _, farm = await _load_batch(batch_id, user, session)
    await _enforce_prod_permission(
        user=user,
        session=session,
        code="production_batch.read",
        organization_id=farm.organization_id,
        farm_id=farm.id,
    )
    rows = await transition_repo.list_for_batch(batch.id)
    return [ProductionBatchTransitionPublic.model_validate(r) for r in rows]


# ===================================================================== #
# ProductionEvent endpoints
# ===================================================================== #
@router.get(
    "/production-events/catalog",
    response_model=ProductionEventCatalogResponse,
    tags=["production-events"],
)
async def get_event_catalog(
    user: CurrentUser,
    session: DBSession,
) -> ProductionEventCatalogResponse:
    """Return the registered production-event catalog.

    Codex Review Gate 02 follow-up: no longer authentication-only.
    Callers must hold ``production_event.read`` at some scope (platform
    admin or via any of their org / farm role assignments). Non-tenant
    users therefore cannot enumerate the event surface.
    """
    await _enforce_prod_permission(
        user=user,
        session=session,
        code="production_event.read",
        organization_id=None,
        farm_id=None,
    )
    return ProductionEventCatalogResponse(
        entries=[
            ProductionEventCatalogEntry.model_validate(e) for e in CATALOG.as_openapi_catalog()
        ],
    )


@router.post(
    "/batches/{batch_id}/events",
    response_model=ProductionEventPublic,
    tags=["production-events"],
    responses={
        201: {"description": "Event created."},
        200: {"description": "Idempotent replay — existing event returned."},
        409: {"description": "Terminal batch state OR idempotency-key payload conflict."},
    },
)
async def create_event(
    batch_id: uuid.UUID,
    payload: ProductionEventCreate,
    user: CurrentUser,
    session: DBSession,
    request_ctx: RequestCtx,
    response: Response,
    service: Annotated[ProductionEventService, Depends(get_event_service)],
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ProductionEventPublic:
    """Create a production event.

    Optional ``Idempotency-Key`` header (Codex Review Gate 01, CRG01-2):

    - When present, ``(batch_id, Idempotency-Key)`` is enforced unique
      by a partial index. Replaying the SAME key with the SAME payload
      returns the original event with HTTP **200** and an
      ``X-Idempotent-Replay: true`` header — no new event is written
      and no batch transition is retriggered.
    - Replaying with a DIFFERENT payload returns **409** with error
      code ``idempotency_key_payload_conflict``.
    """
    batch, unit, site, farm = await _load_batch(batch_id, user, session)
    await _enforce_prod_permission(
        user=user,
        session=session,
        code="production_event.create",
        organization_id=farm.organization_id,
        farm_id=farm.id,
    )
    event, is_replay = await service.create(
        actor=user,
        batch=batch,
        unit=unit,
        site=site,
        farm=farm,
        payload=payload.model_dump(exclude_unset=False),
        request_ctx=request_ctx,
        idempotency_key=idempotency_key,
    )
    if is_replay:
        response.status_code = status.HTTP_200_OK
        response.headers["X-Idempotent-Replay"] = "true"
    else:
        response.status_code = status.HTTP_201_CREATED
    return ProductionEventPublic.model_validate(event)


@router.get(
    "/batches/{batch_id}/transfer-destinations",
    response_model=list[TransferDestinationPublic],
    tags=["production-events"],
)
async def list_transfer_destinations(
    batch_id: uuid.UUID,
    user: CurrentUser,
    session: DBSession,
    service: Annotated[ProductionEventService, Depends(get_event_service)],
) -> list[TransferDestinationPublic]:
    """List eligible destinations using the source batch as the authority boundary."""
    batch, unit, _site, farm = await _load_batch(batch_id, user, session)
    await _enforce_prod_permission(
        user=user,
        session=session,
        code="production_event.create",
        organization_id=farm.organization_id,
        farm_id=farm.id,
    )
    destinations = await service.list_transfer_destinations(batch=batch, unit=unit, farm=farm)
    return [TransferDestinationPublic(**destination) for destination in destinations]


@router.get(
    "/batches/{batch_id}/events",
    response_model=ProductionEventPage,
    tags=["production-events"],
)
async def list_events(
    batch_id: uuid.UUID,
    user: CurrentUser,
    session: DBSession,
    limit: int = Query(default=50, ge=1, le=500),
    cursor: str | None = Query(default=None),
    event_type: str | None = Query(default=None),
    service: Annotated[ProductionEventService, Depends(get_event_service)] = None,  # type: ignore
) -> ProductionEventPage:
    batch, _, _, farm = await _load_batch(batch_id, user, session)
    await _enforce_prod_permission(
        user=user,
        session=session,
        code="production_event.read",
        organization_id=farm.organization_id,
        farm_id=farm.id,
    )
    rows, next_cursor = await service.list_for_batch(
        batch,
        limit=limit,
        cursor=cursor,
        event_type=event_type,
    )
    return ProductionEventPage(
        items=[ProductionEventPublic.model_validate(r) for r in rows],
        next_cursor=next_cursor,
        limit=limit,
    )


@router.get("/events/{event_id}", response_model=ProductionEventPublic, tags=["production-events"])
async def get_event(
    event_id: uuid.UUID,
    user: CurrentUser,
    session: DBSession,
    event_repo: Annotated[ProductionEventRepository, Depends(get_event_repo)],
) -> ProductionEventPublic:
    event = await event_repo.get_by_id(event_id)
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event not found.")
    # Tenant check via the parent batch.
    _batch, _unit, _site, farm = await _load_batch(event.batch_id, user, session)
    await _enforce_prod_permission(
        user=user,
        session=session,
        code="production_event.read",
        organization_id=farm.organization_id,
        farm_id=farm.id,
    )
    return ProductionEventPublic.model_validate(event)


# ===================================================================== #
# APE Batch Projections — derived, read-only aggregates
# ===================================================================== #
@router.get(
    "/batches/{batch_id}/projections",
    response_model=BatchProjectionsPublic,
    tags=["production-batches"],
)
async def get_batch_projections(
    batch_id: uuid.UUID,
    user: CurrentUser,
    session: DBSession,
    event_repo: Annotated[ProductionEventRepository, Depends(get_event_repo)],
) -> BatchProjectionsPublic:
    """Return read-only aggregates for a single batch.

    Nothing here is stored as an editable field — every value is
    derived from the append-only event stream on demand. See
    :mod:`app.services.projections`.
    """
    from app.services.projections import compute_batch_projections

    batch, _unit, _site, farm = await _load_batch(batch_id, user, session)
    await _enforce_prod_permission(
        user=user,
        session=session,
        code="production_batch.read",
        organization_id=farm.organization_id,
        farm_id=farm.id,
    )
    events = await event_repo.list_all_for_batch_asc(batch.id)
    projections = compute_batch_projections(batch, events)
    return BatchProjectionsPublic.model_validate(projections.as_dict())
