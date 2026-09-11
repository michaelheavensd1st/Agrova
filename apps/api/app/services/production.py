"""Production Engine services.

Contains the business rules for sites, unit types, units, batches
(including the state machine) and events (including catalog validation
and event → transition wiring).

The state machine (see :class:`ProductionBatchService`) is the trickiest
piece. It uses a compare-and-swap primitive
(:meth:`ProductionBatchRepository.compare_and_set_state`) so that two
concurrent transitions on the same batch produce exactly one success
and one 409 — never a corrupt final state.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import cast

from fastapi import HTTPException, status
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from app.models.farm import Farm
from app.models.production import (
    ProductionBatch,
    ProductionBatchState,
    ProductionEvent,
    ProductionSite,
    ProductionTransferRole,
    ProductionUnit,
    ProductionUnitType,
)
from app.models.user import User
from app.production.event_catalog import CATALOG, EventCatalogEntry
from app.production.lifecycle_policy import (
    assert_can_create_batch,
    assert_can_create_unit_in_site,
    assert_can_manually_transition,
    assert_event_allowed_by_lifecycle,
)
from app.repositories.audit_repo import AuditRepository
from app.repositories.production import (
    ProductionBatchRepository,
    ProductionBatchTransitionRepository,
    ProductionEventRepository,
    ProductionSiteRepository,
    ProductionUnitRepository,
    ProductionUnitTypeRepository,
)


# --------------------------------------------------------------------- #
# ProductionSite service
# --------------------------------------------------------------------- #
class ProductionSiteService:
    def __init__(
        self,
        *,
        site_repo: ProductionSiteRepository,
        unit_repo: ProductionUnitRepository,
        audit_repo: AuditRepository,
    ) -> None:
        self.site_repo = site_repo
        self.unit_repo = unit_repo
        self.audit_repo = audit_repo

    async def create(
        self, *, actor: User, farm: Farm, data: dict, request_ctx: dict, is_default: bool = False
    ) -> ProductionSite:
        site = await self.site_repo.create(
            farm_id=farm.id,
            is_default=is_default,
            **data,
        )
        await self.audit_repo.record(
            actor_id=actor.id if actor else None,
            action="production_site.create",
            entity_type="production_site",
            entity_id=str(site.id),
            organization_id=farm.organization_id,
            farm_id=farm.id,
            **request_ctx,
        )
        return site

    async def soft_delete(
        self, *, actor: User, site: ProductionSite, farm: Farm, request_ctx: dict
    ) -> None:
        # Guardrail: refuse when any active units exist (per Sprint 2 spec).
        active_units = await self.site_repo.count_active_units(site.id)
        if active_units > 0:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Cannot delete site while {active_units} active production unit(s) exist "
                f"underneath it. Delete or move the units first.",
            )
        if site.deleted_at is not None:
            return  # idempotent
        await self.site_repo.soft_delete(site)
        await self.audit_repo.record(
            actor_id=actor.id,
            action="production_site.delete",
            entity_type="production_site",
            entity_id=str(site.id),
            organization_id=farm.organization_id,
            farm_id=farm.id,
            **request_ctx,
        )

    async def restore(
        self, *, actor: User, site: ProductionSite, farm: Farm, request_ctx: dict
    ) -> ProductionSite:
        if site.deleted_at is None:
            return site
        await self.site_repo.restore(site)
        await self.audit_repo.record(
            actor_id=actor.id,
            action="production_site.restore",
            entity_type="production_site",
            entity_id=str(site.id),
            organization_id=farm.organization_id,
            farm_id=farm.id,
            **request_ctx,
        )
        return site


# --------------------------------------------------------------------- #
# ProductionUnitType service
# --------------------------------------------------------------------- #
class ProductionUnitTypeService:
    def __init__(
        self,
        *,
        unit_type_repo: ProductionUnitTypeRepository,
        audit_repo: AuditRepository,
    ) -> None:
        self.unit_type_repo = unit_type_repo
        self.audit_repo = audit_repo

    async def create_custom(
        self, *, actor: User, organization_id: uuid.UUID, data: dict, request_ctx: dict
    ) -> ProductionUnitType:
        # System-owned codes cannot be shadowed by custom types.
        if await self.unit_type_repo.system_code_exists(data["code"]):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Code {data['code']!r} is reserved by a system-owned production unit type.",
            )
        # Backfill display_name from name if omitted so the UI never
        # has to fall back to the abstract "Production Unit" label.
        data = dict(data)
        if not data.get("display_name"):
            data["display_name"] = data["name"]
        row = await self.unit_type_repo.create(
            organization_id=organization_id, is_system=False, **data
        )
        await self.audit_repo.record(
            actor_id=actor.id,
            action="production_unit_type.create",
            entity_type="production_unit_type",
            entity_id=str(row.id),
            organization_id=organization_id,
            **request_ctx,
        )
        return row

    async def delete_custom(
        self, *, actor: User, row: ProductionUnitType, request_ctx: dict
    ) -> None:
        if row.is_system:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "System production unit types cannot be deleted.",
            )
        if row.deleted_at is not None:
            return
        await self.unit_type_repo.soft_delete(row)
        await self.audit_repo.record(
            actor_id=actor.id,
            action="production_unit_type.delete",
            entity_type="production_unit_type",
            entity_id=str(row.id),
            organization_id=row.organization_id,
            **request_ctx,
        )


# --------------------------------------------------------------------- #
# ProductionUnit service
# --------------------------------------------------------------------- #
class ProductionUnitService:
    def __init__(
        self,
        *,
        unit_repo: ProductionUnitRepository,
        unit_type_repo: ProductionUnitTypeRepository,
        site_repo: ProductionSiteRepository,
        audit_repo: AuditRepository,
    ) -> None:
        self.unit_repo = unit_repo
        self.unit_type_repo = unit_type_repo
        self.site_repo = site_repo
        self.audit_repo = audit_repo

    async def create(
        self,
        *,
        actor: User,
        site: ProductionSite,
        farm: Farm,
        data: dict,
        request_ctx: dict,
    ) -> ProductionUnit:
        if site.deleted_at is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Cannot add a production unit to a deleted site. Restore the site first.",
            )
        # Codex Review Gate 02 (final) — parent-site lifecycle gate.
        # Central helper is the single source of truth for
        # ACTIVE / MAINTENANCE / CLOSED semantics.
        assert_can_create_unit_in_site(site)
        # Verify the type is visible to this org (system or their own).
        unit_type = await self.unit_type_repo.get_visible(
            data["unit_type_id"], organization_id=farm.organization_id
        )
        if unit_type is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Unknown or inaccessible production unit type.",
            )
        try:
            # Keep an expected uniqueness race contained to a SAVEPOINT so
            # the outer request transaction remains usable for a controlled
            # conflict response.
            async with self.unit_repo.session.begin_nested():
                unit = await self.unit_repo.create(site_id=site.id, **data)
        except IntegrityError as exc:
            existing = await self.unit_repo.get_by_site_and_code(
                site_id=site.id,
                code=data["code"],
            )
            if existing is None:
                raise

            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {
                    "code": "production_unit_code_conflict",
                    "message": ("A production unit with this code already exists " "in this site."),
                },
            ) from exc

        await self.audit_repo.record(
            actor_id=actor.id,
            action="production_unit.create",
            entity_type="production_unit",
            entity_id=str(unit.id),
            organization_id=farm.organization_id,
            farm_id=farm.id,
            **request_ctx,
        )
        return unit

    async def soft_delete(
        self,
        *,
        actor: User,
        unit: ProductionUnit,
        farm: Farm,
        request_ctx: dict,
    ) -> None:
        active_batches = await self.unit_repo.count_active_batches(unit.id)
        if active_batches > 0:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Cannot delete a unit with {active_batches} active batch(es). "
                "Close or transfer the batches first.",
            )
        if unit.deleted_at is not None:
            return
        await self.unit_repo.soft_delete(unit)
        await self.audit_repo.record(
            actor_id=actor.id,
            action="production_unit.delete",
            entity_type="production_unit",
            entity_id=str(unit.id),
            organization_id=farm.organization_id,
            farm_id=farm.id,
            **request_ctx,
        )


# --------------------------------------------------------------------- #
# ProductionBatch service — the state machine lives here
# --------------------------------------------------------------------- #

# Allowed transitions. Absence == 409 Conflict. Tuples are (from, to).
_ALLOWED_TRANSITIONS: dict[ProductionBatchState, set[ProductionBatchState]] = {
    ProductionBatchState.PLANNED: {
        ProductionBatchState.STOCKED,
        ProductionBatchState.CANCELLED,
    },
    ProductionBatchState.STOCKED: {
        ProductionBatchState.ACTIVE,
        ProductionBatchState.SUSPENDED,
    },
    ProductionBatchState.ACTIVE: {
        ProductionBatchState.HARVESTED,
        ProductionBatchState.SUSPENDED,
        ProductionBatchState.FAILED,
    },
    ProductionBatchState.SUSPENDED: {
        ProductionBatchState.ACTIVE,
        ProductionBatchState.FAILED,
        ProductionBatchState.CANCELLED,
    },
    ProductionBatchState.HARVESTED: {
        ProductionBatchState.CLOSED,
    },
    # Terminal states — no outbound transitions.
    ProductionBatchState.CLOSED: set(),
    ProductionBatchState.CANCELLED: set(),
    ProductionBatchState.FAILED: set(),
}

# Certain transitions can only be reached via a specific event type.
_EVENT_DRIVEN_TRANSITIONS: dict[tuple[ProductionBatchState, ProductionBatchState], str] = {
    (ProductionBatchState.PLANNED, ProductionBatchState.STOCKED): "STOCKING",
    (ProductionBatchState.ACTIVE, ProductionBatchState.HARVESTED): "HARVEST",
}

_TERMINAL_STATES = {
    ProductionBatchState.CLOSED,
    ProductionBatchState.CANCELLED,
    ProductionBatchState.FAILED,
}


def _compute_payload_hash(event_type: str, validated_data: dict, request_payload: dict) -> str:
    """Deterministic SHA-256 hex over the complete semantic event request.

    Stable regardless of dict key order so two clients constructing the
    same logical request get the same hash. Generated identifiers and audit
    timestamps are deliberately excluded. Used to detect
    Idempotency-Key replays that would otherwise silently overwrite a
    different payload (Codex Review Gate 01, finding CRG01-2).
    """
    canonical = json.dumps(
        {
            "event_type": event_type,
            "performed_at": request_payload.get("performed_at"),
            "data": validated_data,
            "attachments": request_payload.get("attachments"),
            "notes": request_payload.get("notes"),
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _compute_legacy_payload_hash(event_type: str, validated_data: dict[str, object]) -> str:
    """Recognize idempotency hashes persisted before semantic-request hashing."""
    canonical = json.dumps(
        {"event_type": event_type, "data": validated_data},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _idempotency_hash_matches(
    stored_hash: str | None,
    current_hash: str,
    event_type: str,
    validated_data: dict[str, object],
) -> bool:
    return stored_hash in {
        current_hash,
        _compute_legacy_payload_hash(event_type, validated_data),
    }


class ProductionBatchService:
    def __init__(
        self,
        *,
        batch_repo: ProductionBatchRepository,
        transition_repo: ProductionBatchTransitionRepository,
        unit_repo: ProductionUnitRepository,
        audit_repo: AuditRepository,
    ) -> None:
        self.batch_repo = batch_repo
        self.transition_repo = transition_repo
        self.unit_repo = unit_repo
        self.audit_repo = audit_repo

    async def create(
        self,
        *,
        actor: User,
        unit: ProductionUnit,
        site: ProductionSite,
        farm: Farm,
        data: dict,
        request_ctx: dict,
    ) -> ProductionBatch:
        if unit.deleted_at is not None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                "Production unit not found.",
            )
        # Codex Review Gate 02 (final) — parent site + unit must be
        # ACTIVE for new batches. Single source of truth in
        # ``app.production.lifecycle_policy``.
        assert_can_create_batch(site, unit)
        batch = await self.batch_repo.create(
            unit_id=unit.id, state=ProductionBatchState.PLANNED, **data
        )
        # Record the initial transition (from=None) so history is complete.
        await self.transition_repo.record(
            batch_id=batch.id,
            from_state=None,
            to_state=ProductionBatchState.PLANNED,
            actor_id=actor.id,
        )
        await self.audit_repo.record(
            actor_id=actor.id,
            action="production_batch.create",
            entity_type="production_batch",
            entity_id=str(batch.id),
            organization_id=farm.organization_id,
            farm_id=farm.id,
            **request_ctx,
        )
        return batch

    def _validate_transition(
        self,
        *,
        current: ProductionBatchState,
        target: ProductionBatchState,
        triggering_event: ProductionEvent | None,
    ) -> None:
        if current == target:
            # Idempotent same-state transition — noop, not a failure.
            return
        allowed = _ALLOWED_TRANSITIONS.get(current, set())
        if target not in allowed:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Invalid batch transition {current.value} → {target.value}.",
            )
        required_event = _EVENT_DRIVEN_TRANSITIONS.get((current, target))
        if required_event and (
            triggering_event is None or triggering_event.event_type != required_event
        ):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Transition {current.value} → {target.value} requires a "
                f"{required_event} event to trigger it.",
            )

    async def transition(
        self,
        *,
        actor: User,
        batch: ProductionBatch,
        farm: Farm,
        target_state: ProductionBatchState,
        reason: str | None = None,
        request_ctx: dict,
        triggering_event: ProductionEvent | None = None,
        metadata: dict | None = None,
        site: ProductionSite | None = None,
        unit: ProductionUnit | None = None,
    ) -> ProductionBatch:
        # Codex Review Gate 02 (final) — MANUAL transitions must
        # respect the parent site / unit lifecycle. Event-driven
        # transitions (``triggering_event is not None``) are already
        # governed by ``assert_event_allowed_by_lifecycle`` at the
        # event-service layer, so we skip the extra guard here to
        # avoid double-rejecting an allowed evacuation transfer.
        if triggering_event is None and site is not None and unit is not None:
            assert_can_manually_transition(site, unit)

        # Serialise concurrent transitions through the same row lock
        # used by event insertion (Codex Review Gate 02). Skipped when
        # this call is already re-entering under an event write —
        # ``triggering_event is not None`` means the caller already
        # acquired the lock a few frames up, avoiding a self-deadlock.
        if triggering_event is None:
            locked = await self.batch_repo.get_by_id_for_update(batch.id)
            if locked is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Batch not found.")
            batch = locked

        current = batch.state
        self._validate_transition(
            current=current, target=target_state, triggering_event=triggering_event
        )
        if current == target_state:
            return batch

        # Timestamps for lifecycle milestones.
        now = datetime.now(UTC)
        ts_fields: dict[str, datetime] = {}
        if target_state == ProductionBatchState.STOCKED:
            ts_fields["stocked_at"] = now
        elif target_state == ProductionBatchState.HARVESTED:
            ts_fields["harvested_at"] = now
        elif target_state == ProductionBatchState.CLOSED:
            ts_fields["closed_at"] = now

        succeeded = await self.batch_repo.compare_and_set_state(
            batch.id,
            from_state=current,
            to_state=target_state,
            timestamp_fields=ts_fields,
        )
        if not succeeded:
            # Another caller transitioned the batch first.
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Batch state changed concurrently. Reload and try again.",
            )

        # Reflect the new state on the in-memory ORM instance for the caller.
        batch.state = target_state
        for k, v in ts_fields.items():
            setattr(batch, k, v)

        await self.transition_repo.record(
            batch_id=batch.id,
            from_state=current,
            to_state=target_state,
            actor_id=actor.id,
            event_id=triggering_event.id if triggering_event else None,
            reason=reason,
            metadata=metadata,
        )
        await self.audit_repo.record(
            actor_id=actor.id,
            action="production_batch.transition",
            entity_type="production_batch",
            entity_id=str(batch.id),
            organization_id=farm.organization_id,
            farm_id=farm.id,
            metadata={"from": current.value, "to": target_state.value, "reason": reason},
            **request_ctx,
        )
        return batch


# --------------------------------------------------------------------- #
# ProductionEvent service — validates payload + drives batch transitions
# --------------------------------------------------------------------- #
class ProductionEventService:
    def __init__(
        self,
        *,
        event_repo: ProductionEventRepository,
        batch_repo: ProductionBatchRepository,
        batch_service: ProductionBatchService,
        unit_repo: ProductionUnitRepository,
        site_repo: ProductionSiteRepository,
        audit_repo: AuditRepository,
    ) -> None:
        self.event_repo = event_repo
        self.batch_repo = batch_repo
        self.batch_service = batch_service
        self.unit_repo = unit_repo
        self.site_repo = site_repo
        self.audit_repo = audit_repo

    async def create(
        self,
        *,
        actor: User,
        batch: ProductionBatch,
        unit: ProductionUnit,
        site: ProductionSite,
        farm: Farm,
        payload: dict,
        request_ctx: dict,
        idempotency_key: str | None = None,
    ) -> tuple[ProductionEvent, bool]:
        """Create (or idempotently replay) a production event.

        Returns ``(event, is_replay)``. ``is_replay=True`` means an
        existing row with the same ``(batch_id, idempotency_key)`` was
        returned instead of creating a new one — the endpoint uses
        this to signal 200 vs 201 to the client (see
        docs/audits/codex-review-gate-01.md, finding CRG01-2).

        Concurrency (Codex Review Gate 02):
        the batch row is loaded with ``SELECT ... FOR UPDATE`` inside
        the request transaction so mortality/transfer/harvest
        population arithmetic, STOCKING-once enforcement and
        final-harvest gating cannot race with a concurrent event
        write on the same batch. All population reads used for
        validation happen AFTER the lock is held.
        """
        # ---- Catalog validation (schema first — cheap, no DB) ---- #
        code = str(payload.get("event_type", "")).upper()
        entry: EventCatalogEntry | None = CATALOG.get(code)
        if entry is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Unknown event_type {code!r}. Registered types: {', '.join(CATALOG.codes())}.",
            )
        try:
            validated_data = entry.validate(payload.get("data") or {})
        except ValidationError as exc:
            # Emit field-level errors so the UI can surface them precisely.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "event_type": entry.code,
                    "errors": [
                        {
                            "field": ".".join(str(p) for p in err["loc"]),
                            "message": err["msg"],
                            "type": err["type"],
                        }
                        for err in exc.errors()
                    ],
                },
            ) from exc

        # ---- Idempotency check (pre-lock, pre-insert) ------------ #
        # Cheap short-circuit for the common replay case so we don't
        # take out the batch lock unnecessarily.
        payload_hash = _compute_payload_hash(entry.code, validated_data, payload)
        if idempotency_key is not None:
            existing = await self.event_repo.get_by_batch_and_key(batch.id, idempotency_key)
            if existing is not None:
                if not _idempotency_hash_matches(
                    existing.payload_hash, payload_hash, entry.code, validated_data
                ):
                    raise HTTPException(
                        status.HTTP_409_CONFLICT,
                        {
                            "code": "idempotency_key_payload_conflict",
                            "message": (
                                "This Idempotency-Key was previously used with a "
                                "different payload on this batch."
                            ),
                            "idempotency_key": idempotency_key,
                        },
                    )
                # Same key + same payload → return prior event; do NOT
                # re-audit, re-transition, or re-write anything.
                return existing, True

        # ---- Serialise on the batch row (Postgres: FOR UPDATE) --- #
        # Every subsequent read used for population validation must
        # go through ``self.event_repo`` on the same session so the
        # queries observe the same snapshot the lock is holding.
        destination_batch_id: uuid.UUID | None = None
        if entry.code == "TRANSFER":
            try:
                destination_batch_id = uuid.UUID(str(validated_data["destination_batch_id"]))
            except (KeyError, ValueError, TypeError) as exc:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    {"code": "transfer_invalid_destination_batch_id"},
                ) from exc
            if destination_batch_id == batch.id:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    {
                        "code": "transfer_same_batch",
                        "message": "Source and destination batches must differ.",
                    },
                )
            locked = await self.batch_repo.lock_pair(batch.id, destination_batch_id)
            locked_batch = locked.get(batch.id)
        else:
            locked_batch = await self.batch_repo.get_by_id_for_update(batch.id)
        if locked_batch is None:
            # Deleted between the endpoint's tenancy load and now.
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Batch not found.")
        # Reflect the freshly-locked state onto the ORM instance the
        # caller passed in so downstream code (transitions, audit
        # metadata) sees the truth.
        batch = locked_batch

        # Re-check after acquiring the source lock. This closes the window
        # where two identical transfer commands both miss the optimistic
        # pre-lock lookup; the loser now observes and replays the complete pair.
        if idempotency_key is not None:
            existing = await self.event_repo.get_by_batch_and_key(batch.id, idempotency_key)
            if existing is not None:
                if not _idempotency_hash_matches(
                    existing.payload_hash, payload_hash, entry.code, validated_data
                ):
                    raise HTTPException(
                        status.HTTP_409_CONFLICT,
                        {"code": "idempotency_key_payload_conflict"},
                    )
                return existing, True

        if batch.state in _TERMINAL_STATES:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Cannot log new events on a {batch.state.value} batch.",
            )

        # ---- Site / Unit lifecycle policy (Codex Review Gate 02) - #
        await self._enforce_site_unit_lifecycle_policy(
            code=entry.code,
            site=site,
            unit=unit,
            batch=batch,
            data=validated_data,
        )

        # ---- Sprint 3 business rules (now under batch lock) ------ #
        # These run AFTER lock acquisition so mortality / transfer /
        # harvest population arithmetic is race-free.
        is_final = bool(validated_data.get("is_final", False))
        await self._enforce_business_rules(
            code=entry.code,
            data=validated_data,
            batch=batch,
            unit=unit,
            farm=farm,
            is_final=is_final,
        )

        if entry.code == "TRANSFER":
            assert destination_batch_id is not None
            event = await self._create_transfer_pair(
                actor=actor,
                source_batch=batch,
                source_unit=unit,
                source_site=site,
                farm=farm,
                data=validated_data,
                payload=payload,
                payload_hash=payload_hash,
                idempotency_key=idempotency_key,
            )
            await self.audit_repo.record(
                actor_id=actor.id,
                action="production_transfer.create",
                entity_type="production_transfer",
                entity_id=str(event.transfer_id),
                organization_id=farm.organization_id,
                farm_id=farm.id,
                metadata={"source_event_id": str(event.id)},
                **request_ctx,
            )
            return event, False

        try:
            # Wrap the INSERT in a SAVEPOINT so that a concurrent-race
            # ``IntegrityError`` rolls back ONLY this statement, not
            # the whole request transaction. Without this, the audit +
            # transition writes queued earlier in the request would
            # also be lost on collision.
            async with self.event_repo.session.begin_nested():
                event = await self.event_repo.create(
                    organization_id=farm.organization_id,
                    farm_id=farm.id,
                    site_id=site.id,
                    unit_id=unit.id,
                    batch_id=batch.id,
                    event_type=entry.code,
                    event_type_version=entry.version,
                    performed_by_id=actor.id,
                    performed_at=payload.get("performed_at") or datetime.now(UTC),
                    data=validated_data,
                    attachments=payload.get("attachments"),
                    is_final=is_final,
                    notes=payload.get("notes"),
                    idempotency_key=idempotency_key,
                    payload_hash=payload_hash if idempotency_key is not None else None,
                )
        except IntegrityError as exc:
            # Concurrent request won the race for this idempotency key.
            # The savepoint already rolled back this INSERT; the outer
            # transaction is still valid, so we can safely look up the
            # winning row and either replay it or emit 409.
            if idempotency_key is None:
                raise
            existing = await self.event_repo.get_by_batch_and_key(batch.id, idempotency_key)
            if existing is None:  # defensive — should not happen
                raise
            if not _idempotency_hash_matches(
                existing.payload_hash, payload_hash, entry.code, validated_data
            ):
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    {
                        "code": "idempotency_key_payload_conflict",
                        "message": (
                            "This Idempotency-Key was previously used with a "
                            "different payload on this batch."
                        ),
                        "idempotency_key": idempotency_key,
                    },
                ) from exc
            return existing, True

        await self.audit_repo.record(
            actor_id=actor.id,
            action="production_event.create",
            entity_type="production_event",
            entity_id=str(event.id),
            organization_id=farm.organization_id,
            farm_id=farm.id,
            metadata={"event_type": entry.code, "idempotency_key": idempotency_key},
            **request_ctx,
        )

        # ---- Sprint 4 FEEDING → CONSUMPTION integration ---------- #
        # When the FEEDING payload carries an ``inventory_lot_id`` we
        # deduct the recorded quantity from that lot in the SAME
        # session. The savepoint above already delivered the event
        # row; if the consumption raises 409 (insufficient stock,
        # incompatible unit, cross-tenant lot, closed warehouse) the
        # HTTP layer's request-level rollback undoes BOTH writes so
        # we never leave a dangling event with no matching deduction.
        # The idempotency key mirrors the event's key so retries
        # replay the same deduction instead of double-deducting.
        if entry.code == "FEEDING" and validated_data.get("inventory_lot_id") is not None:
            from decimal import Decimal as _Decimal

            from app.models.inventory import StockUnit as _StockUnit
            from app.repositories.audit_repo import AuditRepository as _AuditRepo
            from app.repositories.inventory import (
                InventoryItemRepository as _ItemRepo,
            )
            from app.repositories.inventory import (
                InventoryLotRepository as _LotRepo,
            )
            from app.repositories.inventory import (
                InventoryTransactionRepository as _TxRepo,
            )
            from app.repositories.inventory import (
                StorageLocationRepository as _LocRepo,
            )
            from app.repositories.inventory import (
                WarehouseRepository as _WhRepo,
            )
            from app.services.inventory import InventoryService as _InvService

            inv_service = _InvService(
                session=self.event_repo.session,
                warehouse_repo=_WhRepo(self.event_repo.session),
                item_repo=_ItemRepo(self.event_repo.session),
                lot_repo=_LotRepo(self.event_repo.session),
                tx_repo=_TxRepo(self.event_repo.session),
                location_repo=_LocRepo(self.event_repo.session),
                audit_repo=_AuditRepo(self.event_repo.session),
            )
            # ``FeedUnit`` and ``StockUnit`` share the same string
            # values for the units we support in Sprint 4 (kg / g).
            raw_unit = str(validated_data["unit"])
            try:
                lot_unit = _StockUnit(raw_unit)
            except ValueError as exc:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    {
                        "code": "feed_unit_not_supported_by_inventory",
                        "message": f"Feed unit {raw_unit!r} is not supported by inventory.",
                    },
                ) from exc
            await inv_service.consume_for_event(
                actor=actor,
                farm=farm,
                lot_id=uuid.UUID(str(validated_data["inventory_lot_id"])),
                quantity=_Decimal(str(validated_data["quantity"])),
                unit=lot_unit,
                event_id=event.id,
                # Scope the inventory idempotency key by the event's
                # key so a retry of the same request produces the
                # same paired outcome. If no event key was supplied,
                # namespace by the event id — still safe against
                # accidental replay on the inventory side because
                # the event id is unique per successful insert.
                idempotency_key=(
                    f"prod-event:{idempotency_key}"
                    if idempotency_key is not None
                    else f"prod-event-id:{event.id}"
                ),
                request_ctx=request_ctx,
            )

        # ---- Optional lifecycle transition ----------------------- #
        # Atomicity: transition and event write share the same
        # request-scoped SQLAlchemy session — either both commit or
        # both roll back. If the transition raises 409 (concurrent
        # batch state change) we let it propagate; the request-level
        # rollback in the DB dep removes the event insert too, so we
        # never leave a "dangling" event with no corresponding
        # transition on an event-driven type.
        if entry.triggers_transition_to is not None:
            target = ProductionBatchState(entry.triggers_transition_to)
            # HARVEST only closes the batch when marked final.
            if entry.code == "HARVEST" and not is_final:
                pass
            elif batch.state != target and target in _ALLOWED_TRANSITIONS.get(batch.state, set()):
                await self.batch_service.transition(
                    actor=actor,
                    batch=batch,
                    farm=farm,
                    target_state=target,
                    reason=f"triggered by {entry.code} event",
                    request_ctx=request_ctx,
                    triggering_event=event,
                )
        return event, False

    async def _create_transfer_pair(
        self,
        *,
        actor: User,
        source_batch: ProductionBatch,
        source_unit: ProductionUnit,
        source_site: ProductionSite,
        farm: Farm,
        data: dict[str, object],
        payload: dict[str, object],
        payload_hash: str,
        idempotency_key: str | None,
    ) -> ProductionEvent:
        destination_batch_id = uuid.UUID(str(data["destination_batch_id"]))
        destination_unit_id = uuid.UUID(str(data["destination_unit_id"]))
        resolved = await self.batch_repo.resolve_transfer_destination(
            batch_id=destination_batch_id, unit_id=destination_unit_id, farm_id=farm.id
        )
        if resolved is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                {
                    "code": "transfer_destination_batch_ineligible",
                    "message": "Destination batch does not belong to the selected eligible unit.",
                },
            )
        destination_batch, destination_unit, destination_site = resolved
        if destination_batch.state not in {
            ProductionBatchState.STOCKED,
            ProductionBatchState.ACTIVE,
        }:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {
                    "code": "transfer_destination_batch_state",
                    "message": "Destination batch cannot receive transfers in its current state.",
                },
            )
        transfer = await self.event_repo.create_transfer(
            organization_id=farm.organization_id,
            farm_id=farm.id,
            source_batch_id=source_batch.id,
            destination_batch_id=destination_batch.id,
            source_unit_id=source_unit.id,
            destination_unit_id=destination_unit.id,
            quantity=int(cast(int | str, data["quantity"])),
            transfer_loss=int(cast(int | str, data.get("transfer_loss", 0))),
            idempotency_key=idempotency_key,
            payload_hash=payload_hash if idempotency_key else None,
        )
        raw_performed_at = payload.get("performed_at") or data["transferred_at"]
        effective_performed_at = (
            raw_performed_at
            if isinstance(raw_performed_at, datetime)
            else datetime.fromisoformat(str(raw_performed_at))
        )
        effective_performed_at = (
            effective_performed_at.replace(tzinfo=UTC)
            if effective_performed_at.tzinfo is None
            else effective_performed_at.astimezone(UTC)
        )
        common = {
            "organization_id": farm.organization_id,
            "farm_id": farm.id,
            "event_type": "TRANSFER",
            "event_type_version": CATALOG.require("TRANSFER").version,
            "performed_by_id": actor.id,
            "performed_at": effective_performed_at,
            "data": data,
            "attachments": payload.get("attachments"),
            "is_final": False,
            "notes": payload.get("notes"),
            "transfer_id": transfer.id,
        }
        source_event = await self.event_repo.create(
            **common,
            site_id=source_site.id,
            unit_id=source_unit.id,
            batch_id=source_batch.id,
            transfer_role=ProductionTransferRole.OUT,
            idempotency_key=idempotency_key,
            payload_hash=payload_hash if idempotency_key else None,
        )
        await self.event_repo.create(
            **common,
            site_id=destination_site.id,
            unit_id=destination_unit.id,
            batch_id=destination_batch.id,
            transfer_role=ProductionTransferRole.IN,
            idempotency_key=None,
            payload_hash=None,
        )
        return source_event

    async def list_for_batch(
        self, batch: ProductionBatch, *, limit: int, cursor: str | None, event_type: str | None
    ) -> tuple[list[ProductionEvent], str | None]:
        return await self.event_repo.list_for_batch(
            batch.id,
            limit=limit,
            cursor=cursor,
            event_type=event_type,
        )

    # ------------------------------------------------------------ #
    # Sprint 3 — aquaculture business rules
    # ------------------------------------------------------------ #
    async def _enforce_business_rules(
        self,
        *,
        code: str,
        data: dict,
        batch: ProductionBatch,
        unit: ProductionUnit,
        farm: Farm,
        is_final: bool = False,
    ) -> None:
        """Vertical-neutral pre-insert guards.

        Called AFTER the batch row is held under
        ``SELECT ... FOR UPDATE`` so every population-based decision
        is race-free with concurrent event writes on the same batch.

        Contract (Codex Review Gate 02):

        * STOCKING is allowed only while batch state is ``PLANNED`` and
          only once per batch. A second STOCKING attempt returns 409
          ``stocking_already_recorded``. See PRD "Sprint 3 STOCKING
          policy".
        * MORTALITY / TRANSFER / HARVEST quantities cannot exceed the
          estimated remaining population computed inside the lock.
        * HARVEST with ``is_final=true`` is rejected if the batch has
          already recorded a final HARVEST (409 ``harvest_already_final``)
          — HARVESTED batches also block through the terminal-state
          check upstream, this guard covers the ACTIVE-batch race
          where two final harvests would otherwise land simultaneously.
        * TRANSFER events must reference the batch's current unit as
          source, and a destination unit inside the SAME farm (and
          therefore the same organization). Cross-farm transfers
          rejected pending a Sprint 4 lineage feature.
        """
        if code == "STOCKING":
            await self._enforce_stocking_once(batch=batch)
        elif code == "MORTALITY":
            await self._enforce_mortality_bounds(batch=batch, data=data)
        elif code == "TRANSFER":
            await self._enforce_transfer_scope(batch=batch, unit=unit, farm=farm, data=data)
        elif code == "HARVEST":
            await self._enforce_harvest_rules(batch=batch, data=data, is_final=is_final)

    async def _enforce_stocking_once(self, *, batch: ProductionBatch) -> None:
        """Sprint 3 STOCKING policy: exactly one STOCKING per batch, PLANNED only.

        Rationale (see PRD "Sprint 3 STOCKING policy"): a batch is one
        biologically coherent cohort. Allowing multiple STOCKINGs would
        distort ``initial_stocked_quantity``, cumulative mortality,
        survival rate, biomass and harvest projections. Corrections
        require a dedicated future correction/adjustment workflow so
        the audit trail stays intact.
        """
        if batch.state != ProductionBatchState.PLANNED:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {
                    "code": "stocking_only_in_planned_state",
                    "message": (
                        "STOCKING is only allowed while the batch is in the PLANNED "
                        f"state. Current state: {batch.state.value}."
                    ),
                    "batch_state": batch.state.value,
                },
            )
        # Serialised through the FOR UPDATE lock on the batch row taken
        # in :meth:`create`, so this count is race-safe.
        already = await self.event_repo.count_by_type(batch.id, "STOCKING")
        if already > 0:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {
                    "code": "stocking_already_recorded",
                    "message": (
                        "This batch already has a STOCKING event. A batch represents "
                        "a single biological cohort; additional stock must go into a "
                        "new batch or arrive via a TRANSFER event."
                    ),
                },
            )

    async def _enforce_mortality_bounds(self, *, batch: ProductionBatch, data: dict) -> None:
        from app.services.projections import compute_batch_projections  # local import: cycle-safe

        count = int(data.get("count", 0) or 0)
        if count <= 0:
            return
        events = await self.event_repo.list_all_for_batch_asc(batch.id)
        projections = compute_batch_projections(batch, events)
        if projections.initial_stocked_quantity == 0:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {
                    "code": "mortality_before_stocking",
                    "message": (
                        "Cannot log mortality on a batch that has not been " "stocked yet."
                    ),
                },
            )
        remaining = projections.estimated_remaining_population
        if count > remaining:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {
                    "code": "mortality_exceeds_population",
                    "message": (
                        f"Mortality count {count} exceeds estimated remaining "
                        f"population {remaining}. Use an authorised correction "
                        "workflow to reconcile — the platform will not silently "
                        "accept negative stock."
                    ),
                    "count": count,
                    "estimated_remaining_population": remaining,
                },
            )

    async def _enforce_harvest_rules(
        self,
        *,
        batch: ProductionBatch,
        data: dict,
        is_final: bool,
    ) -> None:
        """Harvest validation completeness (Codex Review Gate 02):

        * ``quantity <= remaining_population``
        * ``total_weight > 0`` (schema enforced; guarded again here)
        * A second final HARVEST is rejected 409
          ``harvest_already_final`` — atomic with the transition.
        """
        from app.services.projections import compute_batch_projections  # cycle-safe

        qty = int(data.get("quantity", 0) or 0)
        total_weight = float(data.get("total_weight", 0) or 0)
        if total_weight <= 0:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                {
                    "code": "harvest_total_weight_required",
                    "message": "HARVEST total_weight must be greater than zero.",
                },
            )

        events = await self.event_repo.list_all_for_batch_asc(batch.id)
        projections = compute_batch_projections(batch, events)
        remaining = projections.estimated_remaining_population
        if qty > remaining:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {
                    "code": "harvest_exceeds_population",
                    "message": (
                        f"Harvest quantity {qty} exceeds estimated remaining "
                        f"population {remaining}."
                    ),
                    "quantity": qty,
                    "estimated_remaining_population": remaining,
                },
            )

        if is_final and await self.event_repo.has_final_harvest(batch.id):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {
                    "code": "harvest_already_final",
                    "message": (
                        "This batch already has a final HARVEST event. "
                        "Additional harvests are not permitted."
                    ),
                },
            )

    async def _enforce_site_unit_lifecycle_policy(
        self,
        *,
        code: str,
        site: ProductionSite,
        unit: ProductionUnit,
        batch: ProductionBatch,
        data: dict,
    ) -> None:
        """Delegate to the central lifecycle policy helper.

        Kept as a thin method so tests + audit trails have a stable
        service-level entry point. All ACTIVE / MAINTENANCE / CLOSED
        rules live in ``app.production.lifecycle_policy``.
        """
        del batch  # batch state is validated separately
        assert_event_allowed_by_lifecycle(
            site=site,
            unit=unit,
            event_code=code,
            source_unit_id=(
                str(data.get("source_unit_id", "")).strip() if code == "TRANSFER" else None
            ),
        )

    async def _enforce_transfer_scope(
        self,
        *,
        batch: ProductionBatch,
        unit: ProductionUnit,
        farm: Farm,
        data: dict,
    ) -> None:
        raw_src = str(data.get("source_unit_id", "")).strip()
        raw_dst = str(data.get("destination_unit_id", "")).strip()
        raw_dst_batch = str(data.get("destination_batch_id", "")).strip()
        try:
            src_id = uuid.UUID(raw_src)
            dst_id = uuid.UUID(raw_dst)
            dst_batch_id = uuid.UUID(raw_dst_batch)
        except (ValueError, TypeError) as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                {
                    "code": "transfer_invalid_unit_id",
                    "message": "source_unit_id and destination_unit_id must be UUIDs.",
                },
            ) from exc

        if src_id != unit.id:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {
                    "code": "transfer_source_changed",
                    "message": "The batch's source unit changed. Refresh and try again.",
                },
            )

        destination = await self.unit_repo.get_eligible_transfer_destination(
            unit_id=dst_id,
            farm_id=farm.id,
            exclude_unit_id=unit.id,
        )
        if destination is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                {
                    "code": "transfer_destination_ineligible",
                    "message": "The selected destination is not eligible for this transfer.",
                },
            )

        resolved_batch = await self.batch_repo.resolve_transfer_destination(
            batch_id=dst_batch_id, unit_id=dst_id, farm_id=farm.id
        )
        if resolved_batch is None or dst_batch_id == batch.id:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                {
                    "code": "transfer_destination_batch_ineligible",
                    "message": "The selected destination batch is not eligible for this transfer.",
                },
            )
        destination_batch, _, _ = resolved_batch
        if destination_batch.state not in {
            ProductionBatchState.STOCKED,
            ProductionBatchState.ACTIVE,
        }:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {
                    "code": "transfer_destination_batch_state",
                    "message": "Destination batch cannot receive transfers in its current state.",
                },
            )

        # Population guard: quantity + transfer_loss ≤ remaining population.
        from app.services.projections import compute_batch_projections  # local: cycle-safe

        qty = int(data.get("quantity", 0) or 0)
        loss = int(data.get("transfer_loss", 0) or 0)
        events = await self.event_repo.list_all_for_batch_asc(batch.id)
        projections = compute_batch_projections(batch, events)
        if qty + loss > projections.estimated_remaining_population:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                {
                    "code": "transfer_exceeds_population",
                    "message": (
                        f"Transfer quantity+loss ({qty + loss}) exceeds "
                        f"estimated remaining population "
                        f"({projections.estimated_remaining_population})."
                    ),
                },
            )

    async def list_transfer_destinations(
        self, *, batch: ProductionBatch, unit: ProductionUnit, farm: Farm
    ) -> list[dict[str, object]]:
        rows = await self.batch_repo.list_eligible_transfer_batches(
            farm_id=farm.id,
            exclude_batch_id=batch.id,
            exclude_unit_id=unit.id,
        )
        return [
            {
                "id": destination_batch.id,
                "unit_id": destination.id,
                "label": (
                    f"{destination.code} — {destination.name} · {destination_batch.code} · {site.code}"
                    if destination.name
                    else f"{destination.code} · {destination_batch.code} · {site.code}"
                ),
            }
            for destination_batch, destination, site in rows
        ]
