"""Sprint 4 inventory endpoints — org-scoped + farm-scoping preserved.

All write endpoints require an ``Idempotency-Key`` header for safe
retries. Same key + same payload → 200 replay; same key + different
payload → 409 ``idempotency_key_payload_conflict``.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.endpoints.production import _enforce_prod_permission
from app.deps import CurrentUser, DBSession, RequestCtx, get_audit_repository, require_permission
from app.models.farm import Farm
from app.models.inventory import Warehouse
from app.models.membership import FarmMembership, OrganizationMembership
from app.repositories.audit_repo import AuditRepository
from app.repositories.inventory import (
    InventoryItemRepository,
    InventoryLotRepository,
    InventoryTransactionRepository,
    StorageLocationRepository,
    WarehouseRepository,
)
from app.repositories.org_repo import FarmRepository, OrganizationRepository
from app.schemas.inventory import (
    AdjustmentRequest,
    InventoryItemCreate,
    InventoryItemPublic,
    InventoryItemUpdate,
    InventoryLotPublic,
    InventoryLotWithBalance,
    InventoryTransactionPage,
    InventoryTransactionPublic,
    IssueRequest,
    ReceiptRequest,
    ReversalRequest,
    StorageLocationCreate,
    StorageLocationPublic,
    TransferRequest,
    WarehouseCreate,
    WarehousePublic,
    WarehouseUpdate,
)
from app.services.inventory import InventoryService

router = APIRouter()


# --------------------------------------------------------------------- #
# Dependency providers
# --------------------------------------------------------------------- #
def get_warehouse_repo(session: DBSession) -> WarehouseRepository:
    return WarehouseRepository(session)


def get_item_repo(session: DBSession) -> InventoryItemRepository:
    return InventoryItemRepository(session)


def get_lot_repo(session: DBSession) -> InventoryLotRepository:
    return InventoryLotRepository(session)


def get_tx_repo(session: DBSession) -> InventoryTransactionRepository:
    return InventoryTransactionRepository(session)


def get_location_repo(session: DBSession) -> StorageLocationRepository:
    return StorageLocationRepository(session)


def get_farm_repo(session: DBSession) -> FarmRepository:
    return FarmRepository(session)


def get_org_repo(session: DBSession) -> OrganizationRepository:
    return OrganizationRepository(session)


def get_inventory_service(
    session: DBSession,
    warehouse_repo: Annotated[WarehouseRepository, Depends(get_warehouse_repo)],
    item_repo: Annotated[InventoryItemRepository, Depends(get_item_repo)],
    lot_repo: Annotated[InventoryLotRepository, Depends(get_lot_repo)],
    tx_repo: Annotated[InventoryTransactionRepository, Depends(get_tx_repo)],
    location_repo: Annotated[StorageLocationRepository, Depends(get_location_repo)],
    audit_repo: Annotated[AuditRepository, Depends(get_audit_repository)],
    farm_repo: Annotated[FarmRepository, Depends(get_farm_repo)],
    org_repo: Annotated[OrganizationRepository, Depends(get_org_repo)],
) -> InventoryService:
    return InventoryService(
        session=session,
        warehouse_repo=warehouse_repo,
        item_repo=item_repo,
        lot_repo=lot_repo,
        tx_repo=tx_repo,
        location_repo=location_repo,
        audit_repo=audit_repo,
        farm_repo=farm_repo,
        org_repo=org_repo,
    )


# --------------------------------------------------------------------- #
# Tenancy helpers
# --------------------------------------------------------------------- #
async def _assert_org_membership(session: AsyncSession, user, organization_id: uuid.UUID) -> None:
    """404 for non-members. Superusers pass through."""
    if user.is_superuser:
        return
    mem = (
        await session.execute(
            select(OrganizationMembership).where(
                OrganizationMembership.user_id == user.id,
                OrganizationMembership.organization_id == organization_id,
                OrganizationMembership.is_active.is_(True),
                OrganizationMembership.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if mem is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Organization not found.")


async def _assert_farm_membership_or_org_access(session: AsyncSession, user, farm: Farm) -> None:
    """Farm access resolves to: (a) superuser, (b) org member, (c) farm
    member. Sprint 4 keeps the same rule as sites/units so warehouse
    farm-pinning behaves identically to the rest of the platform."""
    if user.is_superuser:
        return
    org_mem = (
        await session.execute(
            select(OrganizationMembership).where(
                OrganizationMembership.user_id == user.id,
                OrganizationMembership.organization_id == farm.organization_id,
                OrganizationMembership.is_active.is_(True),
                OrganizationMembership.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if org_mem is not None:
        return
    farm_mem = (
        await session.execute(
            select(FarmMembership).where(
                FarmMembership.user_id == user.id,
                FarmMembership.farm_id == farm.id,
                FarmMembership.is_active.is_(True),
                FarmMembership.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if farm_mem is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Farm not found.")


async def _load_warehouse(
    warehouse_id: uuid.UUID, user, session: DBSession
) -> tuple[Warehouse, Farm | None]:
    """Tenancy-safe warehouse loader.

    Returns 404 if the caller has no access. Farm-pinned warehouses
    require farm-membership OR org-membership; org-shared warehouses
    require org-membership.
    """
    wh = (
        await session.execute(
            select(Warehouse).where(Warehouse.id == warehouse_id, Warehouse.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if wh is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Warehouse not found.")
    farm: Farm | None = None
    if wh.farm_id is not None:
        farm = (
            await session.execute(select(Farm).where(Farm.id == wh.farm_id))
        ).scalar_one_or_none()
        if farm is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Warehouse not found.")
        await _assert_farm_membership_or_org_access(session, user, farm)
    else:
        await _assert_org_membership(session, user, wh.organization_id)
    return wh, farm


# --------------------------------------------------------------------- #
# Warehouses
# --------------------------------------------------------------------- #
@router.post(
    "/organizations/{organization_id}/warehouses",
    response_model=WarehousePublic,
    status_code=status.HTTP_201_CREATED,
    tags=["inventory-warehouses"],
)
async def create_warehouse(
    organization_id: uuid.UUID,
    payload: WarehouseCreate,
    user: CurrentUser,
    session: DBSession,
    request_ctx: RequestCtx,
    service: Annotated[InventoryService, Depends(get_inventory_service)],
) -> WarehousePublic:
    await _assert_org_membership(session, user, organization_id)
    await _enforce_prod_permission(
        user=user,
        session=session,
        code="inventory_warehouse.create",
        organization_id=organization_id,
        farm_id=payload.farm_id,
    )
    # If pinning to a farm, that farm must belong to the same org.
    if payload.farm_id is not None:
        farm = (
            await session.execute(select(Farm).where(Farm.id == payload.farm_id))
        ).scalar_one_or_none()
        if farm is None or farm.organization_id != organization_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Farm not found in this organization.")
    wh = await service.create_warehouse(
        actor=user,
        organization_id=organization_id,
        data=payload.model_dump(),
        request_ctx=request_ctx,
    )
    return WarehousePublic.model_validate(wh)


@router.get(
    "/organizations/{organization_id}/warehouses",
    response_model=list[WarehousePublic],
    tags=["inventory-warehouses"],
)
async def list_warehouses(
    organization_id: uuid.UUID,
    user: CurrentUser,
    session: DBSession,
    warehouse_repo: Annotated[WarehouseRepository, Depends(get_warehouse_repo)],
    operational_only: bool = False,
) -> list[WarehousePublic]:
    await _assert_org_membership(session, user, organization_id)
    await _enforce_prod_permission(
        user=user,
        session=session,
        code="inventory_warehouse.read",
        organization_id=organization_id,
        farm_id=None,
    )
    rows = await warehouse_repo.list_for_org(organization_id, operational_only=operational_only)
    # Non-superuser farm-member visibility: hide warehouses pinned to
    # farms the caller can't access. Org-shared and unpinned ones show.
    if not user.is_superuser:
        visible: list[Warehouse] = []
        for wh in rows:
            if wh.farm_id is None:
                visible.append(wh)
            else:
                try:
                    farm = (
                        await session.execute(select(Farm).where(Farm.id == wh.farm_id))
                    ).scalar_one()
                    await _assert_farm_membership_or_org_access(session, user, farm)
                    visible.append(wh)
                except HTTPException:
                    continue
        rows = visible
    return [WarehousePublic.model_validate(r) for r in rows]


@router.get(
    "/warehouses/{warehouse_id}",
    response_model=WarehousePublic,
    tags=["inventory-warehouses"],
)
async def get_warehouse(
    warehouse_id: uuid.UUID,
    user: CurrentUser,
    session: DBSession,
) -> WarehousePublic:
    wh, _ = await _load_warehouse(warehouse_id, user, session)
    await _enforce_prod_permission(
        user=user,
        session=session,
        code="inventory_warehouse.read",
        organization_id=wh.organization_id,
        farm_id=wh.farm_id,
    )
    return WarehousePublic.model_validate(wh)


@router.patch(
    "/warehouses/{warehouse_id}",
    response_model=WarehousePublic,
    tags=["inventory-warehouses"],
)
async def update_warehouse(
    warehouse_id: uuid.UUID,
    payload: WarehouseUpdate,
    user: CurrentUser,
    session: DBSession,
    request_ctx: RequestCtx,
    service: Annotated[InventoryService, Depends(get_inventory_service)],
) -> WarehousePublic:
    wh, _ = await _load_warehouse(warehouse_id, user, session)
    await _enforce_prod_permission(
        user=user,
        session=session,
        code="inventory_warehouse.update",
        organization_id=wh.organization_id,
        farm_id=wh.farm_id,
    )
    wh = await service.update_warehouse(
        actor=user,
        warehouse=wh,
        data=payload.model_dump(exclude_unset=True),
        request_ctx=request_ctx,
    )
    return WarehousePublic.model_validate(wh)


# --------------------------------------------------------------------- #
# Storage locations
# --------------------------------------------------------------------- #
@router.post(
    "/warehouses/{warehouse_id}/storage-locations",
    response_model=StorageLocationPublic,
    status_code=status.HTTP_201_CREATED,
    tags=["inventory-warehouses"],
)
async def create_storage_location(
    warehouse_id: uuid.UUID,
    payload: StorageLocationCreate,
    user: CurrentUser,
    session: DBSession,
    request_ctx: RequestCtx,
    service: Annotated[InventoryService, Depends(get_inventory_service)],
) -> StorageLocationPublic:
    wh, _ = await _load_warehouse(warehouse_id, user, session)
    await _enforce_prod_permission(
        user=user,
        session=session,
        code="inventory_warehouse.update",
        organization_id=wh.organization_id,
        farm_id=wh.farm_id,
    )
    loc = await service.create_storage_location(
        actor=user,
        warehouse=wh,
        data=payload.model_dump(),
        request_ctx=request_ctx,
    )
    return StorageLocationPublic.model_validate(loc)


@router.get(
    "/warehouses/{warehouse_id}/storage-locations",
    response_model=list[StorageLocationPublic],
    tags=["inventory-warehouses"],
)
async def list_storage_locations(
    warehouse_id: uuid.UUID,
    user: CurrentUser,
    session: DBSession,
    location_repo: Annotated[StorageLocationRepository, Depends(get_location_repo)],
) -> list[StorageLocationPublic]:
    wh, _ = await _load_warehouse(warehouse_id, user, session)
    await _enforce_prod_permission(
        user=user,
        session=session,
        code="inventory_warehouse.read",
        organization_id=wh.organization_id,
        farm_id=wh.farm_id,
    )
    return [
        StorageLocationPublic.model_validate(r)
        for r in await location_repo.list_for_warehouse(wh.id)
    ]


# --------------------------------------------------------------------- #
# Inventory items (catalog)
# --------------------------------------------------------------------- #
@router.post(
    "/organizations/{organization_id}/inventory-items",
    response_model=InventoryItemPublic,
    status_code=status.HTTP_201_CREATED,
    tags=["inventory-items"],
)
async def create_item(
    organization_id: uuid.UUID,
    payload: InventoryItemCreate,
    user: CurrentUser,
    session: DBSession,
    request_ctx: RequestCtx,
    service: Annotated[InventoryService, Depends(get_inventory_service)],
) -> InventoryItemPublic:
    await _assert_org_membership(session, user, organization_id)
    await _enforce_prod_permission(
        user=user,
        session=session,
        code="inventory_item.create",
        organization_id=organization_id,
        farm_id=None,
    )
    item = await service.create_item(
        actor=user,
        organization_id=organization_id,
        data=payload.model_dump(),
        request_ctx=request_ctx,
    )
    return InventoryItemPublic.model_validate(item)


@router.get(
    "/organizations/{organization_id}/inventory-items",
    response_model=list[InventoryItemPublic],
    dependencies=[
        Depends(require_permission("inventory_item.read", include_farm_grants_in_org=True))
    ],
    tags=["inventory-items"],
)
async def list_items(
    organization_id: uuid.UUID,
    user: CurrentUser,
    session: DBSession,
    item_repo: Annotated[InventoryItemRepository, Depends(get_item_repo)],
    operational_only: bool = False,
) -> list[InventoryItemPublic]:
    await _assert_org_membership(session, user, organization_id)
    return [
        InventoryItemPublic.model_validate(r)
        for r in await item_repo.list_for_org(organization_id, operational_only=operational_only)
    ]


@router.patch(
    "/inventory-items/{item_id}",
    response_model=InventoryItemPublic,
    tags=["inventory-items"],
)
async def update_item(
    item_id: uuid.UUID,
    payload: InventoryItemUpdate,
    user: CurrentUser,
    session: DBSession,
    request_ctx: RequestCtx,
    item_repo: Annotated[InventoryItemRepository, Depends(get_item_repo)],
    service: Annotated[InventoryService, Depends(get_inventory_service)],
) -> InventoryItemPublic:
    item = await item_repo.get_by_id(item_id)
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Inventory item not found.")
    await _assert_org_membership(session, user, item.organization_id)
    await _enforce_prod_permission(
        user=user,
        session=session,
        code="inventory_item.update",
        organization_id=item.organization_id,
        farm_id=None,
    )
    item = await service.update_item(
        actor=user,
        item=item,
        data=payload.model_dump(exclude_unset=True),
        request_ctx=request_ctx,
    )
    return InventoryItemPublic.model_validate(item)


# --------------------------------------------------------------------- #
# Lots + balances
# --------------------------------------------------------------------- #
@router.get(
    "/warehouses/{warehouse_id}/lots",
    response_model=list[InventoryLotWithBalance],
    tags=["inventory-lots"],
)
async def list_lots(
    warehouse_id: uuid.UUID,
    user: CurrentUser,
    session: DBSession,
    lot_repo: Annotated[InventoryLotRepository, Depends(get_lot_repo)],
    tx_repo: Annotated[InventoryTransactionRepository, Depends(get_tx_repo)],
    item_repo: Annotated[InventoryItemRepository, Depends(get_item_repo)],
) -> list[InventoryLotWithBalance]:
    wh, _ = await _load_warehouse(warehouse_id, user, session)
    await _enforce_prod_permission(
        user=user,
        session=session,
        code="inventory_lot.read",
        organization_id=wh.organization_id,
        farm_id=wh.farm_id,
    )
    lots = await lot_repo.list_for_warehouse(wh.id)
    out: list[InventoryLotWithBalance] = []
    for lot in lots:
        item = await item_repo.get_by_id(lot.item_id)
        balance = await tx_repo.get_balance_in_canonical(lot.id)
        out.append(
            InventoryLotWithBalance.model_validate(
                {
                    **{k: getattr(lot, k) for k in InventoryLotPublic.model_fields},
                    "balance": balance,
                    "balance_unit": item.canonical_unit,
                }
            )
        )
    return out


@router.get(
    "/lots/{lot_id}",
    response_model=InventoryLotWithBalance,
    tags=["inventory-lots"],
)
async def get_lot(
    lot_id: uuid.UUID,
    user: CurrentUser,
    session: DBSession,
    lot_repo: Annotated[InventoryLotRepository, Depends(get_lot_repo)],
    tx_repo: Annotated[InventoryTransactionRepository, Depends(get_tx_repo)],
    item_repo: Annotated[InventoryItemRepository, Depends(get_item_repo)],
) -> InventoryLotWithBalance:
    lot = await lot_repo.get_by_id(lot_id)
    if lot is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lot not found.")
    wh, _ = await _load_warehouse(lot.warehouse_id, user, session)
    await _enforce_prod_permission(
        user=user,
        session=session,
        code="inventory_lot.read",
        organization_id=wh.organization_id,
        farm_id=wh.farm_id,
    )
    item = await item_repo.get_by_id(lot.item_id)
    balance = await tx_repo.get_balance_in_canonical(lot.id)
    return InventoryLotWithBalance.model_validate(
        {
            **{k: getattr(lot, k) for k in InventoryLotPublic.model_fields},
            "balance": balance,
            "balance_unit": item.canonical_unit,
        }
    )


# --------------------------------------------------------------------- #
# Ledger operations
# --------------------------------------------------------------------- #
def _idempotency_header() -> str | None:
    return None  # placeholder — dependencies below use Header directly


@router.post(
    "/warehouses/{warehouse_id}/inventory:receive",
    response_model=InventoryTransactionPublic,
    tags=["inventory-transactions"],
)
async def receive_stock(
    warehouse_id: uuid.UUID,
    payload: ReceiptRequest,
    user: CurrentUser,
    session: DBSession,
    request_ctx: RequestCtx,
    response: Response,
    service: Annotated[InventoryService, Depends(get_inventory_service)],
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> InventoryTransactionPublic:
    wh, _ = await _load_warehouse(warehouse_id, user, session)
    await _enforce_prod_permission(
        user=user,
        session=session,
        code="inventory_transaction.create",
        organization_id=wh.organization_id,
        farm_id=wh.farm_id,
    )
    tx, _lot, is_replay = await service.receipt(
        actor=user,
        warehouse=wh,
        payload=payload.model_dump(),
        request_ctx=request_ctx,
        idempotency_key=idempotency_key,
    )
    response.status_code = status.HTTP_200_OK if is_replay else status.HTTP_201_CREATED
    if is_replay:
        response.headers["X-Idempotent-Replay"] = "true"
    return InventoryTransactionPublic.model_validate(tx)


@router.post(
    "/warehouses/{warehouse_id}/inventory:issue",
    response_model=InventoryTransactionPublic,
    tags=["inventory-transactions"],
)
async def issue_stock(
    warehouse_id: uuid.UUID,
    payload: IssueRequest,
    user: CurrentUser,
    session: DBSession,
    request_ctx: RequestCtx,
    response: Response,
    service: Annotated[InventoryService, Depends(get_inventory_service)],
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> InventoryTransactionPublic:
    wh, _ = await _load_warehouse(warehouse_id, user, session)
    await _enforce_prod_permission(
        user=user,
        session=session,
        code="inventory_transaction.create",
        organization_id=wh.organization_id,
        farm_id=wh.farm_id,
    )
    tx, is_replay = await service.issue(
        actor=user,
        warehouse=wh,
        payload=payload.model_dump(),
        request_ctx=request_ctx,
        idempotency_key=idempotency_key,
    )
    response.status_code = status.HTTP_200_OK if is_replay else status.HTTP_201_CREATED
    if is_replay:
        response.headers["X-Idempotent-Replay"] = "true"
    return InventoryTransactionPublic.model_validate(tx)


@router.post(
    "/warehouses/{warehouse_id}/inventory:transfer",
    response_model=InventoryTransactionPublic,
    tags=["inventory-transactions"],
)
async def transfer_stock(
    warehouse_id: uuid.UUID,
    payload: TransferRequest,
    user: CurrentUser,
    session: DBSession,
    request_ctx: RequestCtx,
    response: Response,
    service: Annotated[InventoryService, Depends(get_inventory_service)],
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> InventoryTransactionPublic:
    # Sprint 5.4.11 — Locked Authorization. The endpoint performs
    # only lightweight request parsing / identity resolution. Every
    # authorization decision (tenancy 404, membership 404, permission
    # 403, cross-org 409, warehouse status, farm/organization
    # validity) happens INSIDE the service, AFTER canonical row
    # locks on source + destination warehouses, their referenced
    # farms, and the owning organization are held FOR UPDATE. The
    # service reloads authoritative rows via those locks and derives
    # scopes exclusively from the locked state — a permission,
    # membership, role, warehouse-assignment, farm-assignment, or
    # organization-status change that races with the transfer is
    # authoritatively resolved against the locked state.
    del session  # authorization no longer runs at endpoint layer
    out_tx, _in, is_replay = await service.transfer(
        actor=user,
        warehouse_id=warehouse_id,
        payload=payload.model_dump(),
        request_ctx=request_ctx,
        idempotency_key=idempotency_key,
    )
    response.status_code = status.HTTP_200_OK if is_replay else status.HTTP_201_CREATED
    if is_replay:
        response.headers["X-Idempotent-Replay"] = "true"
    return InventoryTransactionPublic.model_validate(out_tx)


@router.post(
    "/warehouses/{warehouse_id}/inventory:adjust",
    response_model=InventoryTransactionPublic,
    tags=["inventory-transactions"],
)
async def adjust_stock(
    warehouse_id: uuid.UUID,
    payload: AdjustmentRequest,
    user: CurrentUser,
    session: DBSession,
    request_ctx: RequestCtx,
    response: Response,
    service: Annotated[InventoryService, Depends(get_inventory_service)],
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> InventoryTransactionPublic:
    wh, _ = await _load_warehouse(warehouse_id, user, session)
    await _enforce_prod_permission(
        user=user,
        session=session,
        code="inventory_transaction.create",
        organization_id=wh.organization_id,
        farm_id=wh.farm_id,
    )
    tx, is_replay = await service.adjustment(
        actor=user,
        warehouse=wh,
        payload=payload.model_dump(),
        request_ctx=request_ctx,
        idempotency_key=idempotency_key,
    )
    response.status_code = status.HTTP_200_OK if is_replay else status.HTTP_201_CREATED
    if is_replay:
        response.headers["X-Idempotent-Replay"] = "true"
    return InventoryTransactionPublic.model_validate(tx)


@router.post(
    "/warehouses/{warehouse_id}/inventory:reverse",
    response_model=InventoryTransactionPublic,
    tags=["inventory-transactions"],
)
async def reverse_stock(
    warehouse_id: uuid.UUID,
    payload: ReversalRequest,
    user: CurrentUser,
    session: DBSession,
    request_ctx: RequestCtx,
    response: Response,
    service: Annotated[InventoryService, Depends(get_inventory_service)],
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> InventoryTransactionPublic:
    wh, _ = await _load_warehouse(warehouse_id, user, session)
    # Sprint 5.4.3 — dual-warehouse authorization for transfer
    # reversals. `resolve_reversal_scopes` returns the source scope
    # plus, for paired transfers, the counterpart's scope. Every
    # scope must pass `_enforce_prod_permission` BEFORE the write
    # transaction opens; a failure on either side rejects the request
    # with no ledger effect.
    scopes = await service.resolve_reversal_scopes(
        warehouse=wh,
        reverses_transaction_id=payload.reverses_transaction_id,
    )
    for scope_org_id, scope_farm_id in scopes:
        await _enforce_prod_permission(
            user=user,
            session=session,
            code="inventory_transaction.create",
            organization_id=scope_org_id,
            farm_id=scope_farm_id,
        )
    tx, is_replay = await service.reversal(
        actor=user,
        warehouse=wh,
        payload=payload.model_dump(),
        request_ctx=request_ctx,
        idempotency_key=idempotency_key,
    )
    response.status_code = status.HTTP_200_OK if is_replay else status.HTTP_201_CREATED
    if is_replay:
        response.headers["X-Idempotent-Replay"] = "true"
    return InventoryTransactionPublic.model_validate(tx)


@router.get(
    "/lots/{lot_id}/transactions",
    response_model=InventoryTransactionPage,
    tags=["inventory-transactions"],
)
async def list_transactions(
    lot_id: uuid.UUID,
    user: CurrentUser,
    session: DBSession,
    limit: int = Query(default=50, ge=1, le=500),
    cursor: str | None = Query(default=None),
    lot_repo: Annotated[InventoryLotRepository, Depends(get_lot_repo)] = None,  # type: ignore
    tx_repo: Annotated[InventoryTransactionRepository, Depends(get_tx_repo)] = None,  # type: ignore
) -> InventoryTransactionPage:
    lot = await lot_repo.get_by_id(lot_id)
    if lot is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lot not found.")
    wh, _ = await _load_warehouse(lot.warehouse_id, user, session)
    await _enforce_prod_permission(
        user=user,
        session=session,
        code="inventory_transaction.read",
        organization_id=wh.organization_id,
        farm_id=wh.farm_id,
    )
    rows, next_cursor = await tx_repo.list_for_lot(lot.id, limit=limit, cursor=cursor)
    return InventoryTransactionPage(
        items=[InventoryTransactionPublic.model_validate(r) for r in rows],
        next_cursor=next_cursor,
        limit=limit,
    )
