"""Production Engine API schemas.

Kept in one file so the whole Production bounded-context ships as a
single cohesive contract — matching the ``services/production.py``,
``repositories/production.py``, and ``endpoints/production.py``
layout.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models.production import (
    ProductionBatchState,
    ProductionSiteStatus,
    ProductionUnitStatus,
)


# --------------------------------------------------------------------- #
# ProductionSite
# --------------------------------------------------------------------- #
class ProductionSiteCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    code: str = Field(min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=1000)
    address: str | None = Field(default=None, max_length=500)
    latitude: float | None = None
    longitude: float | None = None
    timezone: str | None = Field(default=None, max_length=80)
    manager_id: UUID | None = None
    capacity: int | None = Field(default=None, ge=0)
    status: ProductionSiteStatus = ProductionSiteStatus.ACTIVE
    metadata_json: dict | None = None


class ProductionSiteUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    address: str | None = Field(default=None, max_length=500)
    latitude: float | None = None
    longitude: float | None = None
    timezone: str | None = Field(default=None, max_length=80)
    manager_id: UUID | None = None
    capacity: int | None = Field(default=None, ge=0)
    status: ProductionSiteStatus | None = None
    metadata_json: dict | None = None


class ProductionSitePublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    farm_id: UUID
    name: str
    code: str
    description: str | None
    address: str | None
    latitude: float | None
    longitude: float | None
    timezone: str | None
    manager_id: UUID | None
    capacity: int | None
    status: ProductionSiteStatus
    metadata_json: dict | None
    is_default: bool
    is_active: bool = True
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------- #
# ProductionUnitType
# --------------------------------------------------------------------- #
class BatchProjectionsPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    batch_id: UUID
    initial_stocked_quantity: int
    cumulative_mortality: int
    cumulative_harvest: int
    cumulative_transfer_out: int
    cumulative_transfer_in: int
    estimated_remaining_population: int
    latest_average_weight: float | None
    weight_unit: str | None
    estimated_biomass_kg: float | None
    total_feed_kg: float
    survival_rate: float | None
    batch_age_days: int | None
    latest_water_quality: dict | None
    latest_sampling_at: str | None
    computed_at: str


class ProductionUnitTypeCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    display_name: str | None = Field(default=None, max_length=255)
    plural_name: str | None = Field(default=None, max_length=255)
    vertical: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=1000)
    category: str | None = Field(default=None, max_length=64)
    metadata_json: dict | None = None


class ProductionUnitTypePublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    organization_id: UUID | None
    code: str
    name: str
    display_name: str
    plural_name: str | None
    vertical: str | None
    description: str | None
    category: str | None
    is_system: bool
    metadata_json: dict | None
    created_at: datetime


# --------------------------------------------------------------------- #
# ProductionUnit
# --------------------------------------------------------------------- #
class ProductionUnitCreate(BaseModel):
    unit_type_id: UUID
    name: str = Field(min_length=1, max_length=255)
    code: str = Field(min_length=1, max_length=64)
    capacity: int | None = Field(default=None, ge=0)
    status: ProductionUnitStatus = ProductionUnitStatus.ACTIVE
    metadata_json: dict | None = None


class ProductionUnitUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    capacity: int | None = Field(default=None, ge=0)
    status: ProductionUnitStatus | None = None
    metadata_json: dict | None = None


class ProductionUnitPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    site_id: UUID
    unit_type_id: UUID
    name: str
    code: str
    capacity: int | None
    status: ProductionUnitStatus
    metadata_json: dict | None
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------- #
# ProductionBatch
# --------------------------------------------------------------------- #
class ProductionBatchCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    species: str | None = Field(default=None, max_length=255)
    planned_at: datetime | None = None
    expected_quantity: int | None = Field(default=None, ge=0)
    notes: str | None = Field(default=None, max_length=2000)
    metadata_json: dict | None = None


class ProductionBatchUpdate(BaseModel):
    """Non-state metadata edits. State changes go through /transitions."""

    species: str | None = Field(default=None, max_length=255)
    planned_at: datetime | None = None
    expected_quantity: int | None = Field(default=None, ge=0)
    notes: str | None = Field(default=None, max_length=2000)
    metadata_json: dict | None = None


class ProductionBatchPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    unit_id: UUID
    code: str
    state: ProductionBatchState
    species: str | None
    planned_at: datetime | None
    stocked_at: datetime | None
    harvested_at: datetime | None
    closed_at: datetime | None
    expected_quantity: int | None
    actual_quantity: int | None
    notes: str | None
    metadata_json: dict | None
    created_at: datetime
    updated_at: datetime


class ProductionBatchTransitionRequest(BaseModel):
    target_state: ProductionBatchState
    reason: str | None = Field(default=None, max_length=1000)
    metadata_json: dict | None = None


class ProductionBatchTransitionPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    batch_id: UUID
    from_state: ProductionBatchState | None
    to_state: ProductionBatchState
    actor_id: UUID | None
    event_id: UUID | None
    reason: str | None
    metadata_json: dict | None
    occurred_at: datetime
    created_at: datetime


# --------------------------------------------------------------------- #
# ProductionEvent
# --------------------------------------------------------------------- #
class ProductionEventCreate(BaseModel):
    event_type: str = Field(min_length=1, max_length=64)
    performed_at: datetime | None = None
    data: dict[str, Any]
    attachments: list[dict] | None = None
    notes: str | None = Field(default=None, max_length=2000)


class TransferDestinationPublic(BaseModel):
    """Explicit eligible destination batch with an authoritative unit label."""

    id: UUID
    unit_id: UUID
    label: str


class ProductionEventPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    organization_id: UUID
    farm_id: UUID
    site_id: UUID
    unit_id: UUID
    batch_id: UUID
    event_type: str
    event_type_version: int
    transfer_id: UUID | None = None
    transfer_role: str | None = None
    performed_by_id: UUID | None
    performed_at: datetime
    data: dict
    attachments: list[dict] | None
    is_final: bool
    notes: str | None
    idempotency_key: str | None = None
    created_at: datetime


class ProductionEventPage(BaseModel):
    """Cursor-paginated event list.

    Cursor is opaque to the client — pass ``next_cursor`` back on the
    next call. Ordering is ``(performed_at DESC, id DESC)``.
    """

    items: list[ProductionEventPublic]
    next_cursor: str | None = None
    limit: int


class ProductionEventCatalogEntry(BaseModel):
    # ``schema`` is a legitimate JSON-schema field name in the catalog
    # response; ignore Pydantic's protected-namespaces warning for it.
    model_config = ConfigDict(populate_by_name=True, protected_namespaces=(), extra="ignore")

    code: str
    display_name: str
    category: str
    version: int
    triggers_transition_to: str | None
    payload_schema: dict = Field(..., alias="schema")
    metadata: dict


class ProductionEventCatalogResponse(BaseModel):
    entries: list[ProductionEventCatalogEntry]
