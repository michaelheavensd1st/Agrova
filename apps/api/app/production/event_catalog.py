"""Production Event catalog + per-event-type Pydantic schemas.

Every :class:`ProductionEvent` payload is validated by a schema
registered in :data:`CATALOG`. Unknown fields are rejected
(``extra="forbid"``); the validated dict is what gets persisted into
the ``data`` JSONB column. The registration is central so:

* the frontend can pull the JSON Schema per event type and generate
  forms + client-side validation from the same source of truth;
* analytics / warehousing consumers can rely on stable field names;
* new event types are added ONLY through platform releases (never at
  runtime by tenants).

This module is the aquaculture-first Sprint 3 event surface. Other
verticals (livestock, crop) will register their own catalog entries
in follow-on sprints without duplicating APE tables.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


# --------------------------------------------------------------------- #
# Shared value objects
# --------------------------------------------------------------------- #
class WeightUnit(StrEnum):
    G = "g"
    KG = "kg"


class FeedUnit(StrEnum):
    G = "g"
    KG = "kg"


class FeedingMethod(StrEnum):
    BROADCAST = "broadcast"
    TRAY = "tray"
    AUTOMATIC = "automatic"
    HAND = "hand"


class HarvestType(StrEnum):
    PARTIAL = "partial"
    TOTAL = "total"


class MortalityDisposal(StrEnum):
    BURIAL = "burial"
    INCINERATION = "incineration"
    COMPOST = "compost"
    RENDERING = "rendering"
    OTHER = "other"


class WaterQualityUnits(BaseModel):
    """Explicit unit annotations for every water-quality measurement.

    Making units first-class prevents the "was that °C or °F?" class
    of latent data errors when the same JSONB is consumed by
    analytics later.
    """

    model_config = ConfigDict(extra="forbid")

    temperature: str | None = Field(default="C", description="Temperature unit (C | F).")
    dissolved_oxygen: str | None = Field(default="mg_l", description="DO unit (mg_l | ppm | %).")
    ammonia: str | None = Field(default="mg_l", description="Ammonia unit (mg_l | ppm).")
    nitrite: str | None = Field(default="mg_l", description="Nitrite unit (mg_l | ppm).")
    turbidity: str | None = Field(default="NTU", description="Turbidity unit (NTU | JTU).")


class MortalityEvidence(BaseModel):
    """Optional evidence metadata attached to a MORTALITY event."""

    model_config = ConfigDict(extra="forbid")

    photos: list[str] | None = Field(
        default=None, description="Attachment IDs (stored elsewhere) or user-provided URIs."
    )
    lab_report_ref: str | None = Field(default=None, max_length=255)
    veterinarian_id: str | None = Field(default=None, max_length=255)
    notes: str | None = Field(default=None, max_length=1000)


# --------------------------------------------------------------------- #
# Per-event-type payload schemas (Sprint 3 — aquaculture slice 01)
# --------------------------------------------------------------------- #
class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StockingEventSchema(_StrictModel):
    """Initial stocking event. Transitions PLANNED → STOCKED."""

    species_code: str = Field(
        min_length=1,
        max_length=64,
        description="Vertical-specific species identifier (e.g. WHITE_SHRIMP, TILAPIA_GIFT).",
    )
    quantity: int = Field(ge=1, description="Individuals stocked.")
    average_weight: float = Field(ge=0)
    weight_unit: WeightUnit = Field(default=WeightUnit.G)
    source: str | None = Field(
        default=None, max_length=255, description="Hatchery / supplier reference."
    )
    stocked_at: datetime = Field(description="When the stocking physically happened.")
    notes: str | None = Field(default=None, max_length=1000)


class FeedingEventSchema(_StrictModel):
    """Feed delivered to the batch. At least one of ``feed_item_ref``,
    ``feed_description``, or ``inventory_lot_id`` must be supplied.

    Sprint 4 integration: when ``inventory_lot_id`` is provided the
    event service atomically posts a ``CONSUMPTION`` transaction on
    that lot (see :meth:`InventoryService.consume_for_event`). The
    feed lot must belong to the same organization AND (if pinned) the
    same farm as the batch's unit. Insufficient stock raises 409.
    """

    feed_item_ref: str | None = Field(
        default=None,
        max_length=128,
        description=(
            "Reference to an inventory feed item, if one exists. "
            "Sprint 4 adds first-class inventory — prefer "
            "``inventory_lot_id`` when possible."
        ),
    )
    feed_description: str | None = Field(default=None, max_length=255)
    inventory_lot_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "Optional feed lot to deduct against. When supplied, event "
            "creation and stock deduction succeed or fail together — "
            "insufficient stock, incompatible units, or cross-tenant "
            "lot references all reject the whole event."
        ),
    )
    quantity: float = Field(gt=0)
    unit: FeedUnit = Field(default=FeedUnit.KG)
    feeding_method: FeedingMethod = Field(default=FeedingMethod.BROADCAST)
    feeding_round: int | None = Field(
        default=None, ge=1, description="1-indexed feeding round for the day."
    )
    notes: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _at_least_one_feed_id(self) -> FeedingEventSchema:
        if not self.feed_item_ref and not self.feed_description and not self.inventory_lot_id:
            raise ValueError("Provide feed_item_ref, feed_description, or inventory_lot_id.")
        return self


class MortalityEventSchema(_StrictModel):
    """Mortality observation. The service layer enforces
    ``count <= estimated_remaining_population`` at write time."""

    count: int = Field(ge=1, description="Number of mortalities observed.")
    suspected_cause: str | None = Field(default=None, max_length=255)
    disposal_method: MortalityDisposal | None = Field(default=None)
    observed_at: datetime
    evidence: MortalityEvidence | None = None


class SamplingEventSchema(_StrictModel):
    """Growth / population sampling."""

    sample_size: int = Field(ge=1, description="Individuals weighed in the sample.")
    average_weight: float = Field(ge=0)
    minimum_weight: float | None = Field(default=None, ge=0)
    maximum_weight: float | None = Field(default=None, ge=0)
    weight_unit: WeightUnit = Field(default=WeightUnit.G)
    estimated_population: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Farmer's re-estimate of remaining population. Used as an "
            "authoritative override in the projection service when "
            "present."
        ),
    )
    notes: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _weight_bounds(self) -> SamplingEventSchema:
        if self.minimum_weight is not None and self.minimum_weight > self.average_weight:
            raise ValueError("minimum_weight cannot exceed average_weight.")
        if self.maximum_weight is not None and self.maximum_weight < self.average_weight:
            raise ValueError("maximum_weight cannot be below average_weight.")
        if (
            self.minimum_weight is not None
            and self.maximum_weight is not None
            and self.minimum_weight > self.maximum_weight
        ):
            raise ValueError("minimum_weight cannot exceed maximum_weight.")
        return self


class WaterQualityEventSchema(_StrictModel):
    """Water-quality reading. Every parameter is nullable because
    real farms cannot always measure every value; but the unit
    annotation must remain explicit and impossible values are
    rejected."""

    temperature: float | None = Field(default=None, ge=-5, le=60, description="Water temperature.")
    ph: float | None = Field(default=None, ge=0, le=14)
    dissolved_oxygen: float | None = Field(default=None, ge=0, le=30)
    ammonia: float | None = Field(default=None, ge=0, le=100)
    nitrite: float | None = Field(default=None, ge=0, le=100)
    turbidity: float | None = Field(default=None, ge=0, le=1000)
    measurement_units: WaterQualityUnits = Field(default_factory=WaterQualityUnits)
    measured_at: datetime


class TransferEventSchema(_StrictModel):
    """Transfer of individuals from the current batch's unit to another.

    Business rules enforced in the service layer:
    * ``source_unit_id`` must equal the batch's current unit.
    * ``destination_unit_id`` must belong to the SAME farm and same
      organization as the source.
    * ``quantity + transfer_loss`` cannot exceed the batch's estimated
      remaining population.
    """

    source_unit_id: str = Field(min_length=1)
    destination_unit_id: str = Field(min_length=1)
    destination_batch_id: str = Field(min_length=1)
    quantity: int = Field(ge=1, description="Individuals transferred (net).")
    average_weight: float | None = Field(default=None, ge=0)
    weight_unit: WeightUnit = Field(default=WeightUnit.G)
    transfer_loss: int = Field(default=0, ge=0, description="Mortalities incurred during transfer.")
    transferred_at: datetime
    notes: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _distinct_units(self) -> TransferEventSchema:
        if self.source_unit_id == self.destination_unit_id:
            raise ValueError("source_unit_id and destination_unit_id must differ.")
        return self


class HarvestEventSchema(_StrictModel):
    """Harvest event. Batch transitions to HARVESTED only when
    ``is_final=true``. Partial harvests remain in-state."""

    quantity: int = Field(ge=1)
    total_weight: float = Field(gt=0, description="Aggregate harvested biomass; must be positive.")
    average_weight: float | None = Field(default=None, ge=0)
    weight_unit: WeightUnit = Field(default=WeightUnit.KG)
    harvest_type: HarvestType = Field(default=HarvestType.PARTIAL)
    is_final: bool = Field(default=False)
    harvested_at: datetime
    notes: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _final_iff_total(self) -> HarvestEventSchema:
        # is_final and harvest_type=TOTAL are semantically linked but
        # not identical: TOTAL implies is_final MUST be true.
        if self.harvest_type == HarvestType.TOTAL and not self.is_final:
            raise ValueError("harvest_type='total' requires is_final=true.")
        return self


# --------------------------------------------------------------------- #
# Catalog registry
# --------------------------------------------------------------------- #
@dataclass(frozen=True)
class EventCatalogEntry:
    code: str
    schema: type[_StrictModel]
    version: int
    display_name: str
    category: str
    triggers_transition_to: str | None = None
    metadata: dict = field(default_factory=dict)
    openapi_example: dict | None = None

    def validate(self, data: dict) -> dict:
        """Validate + coerce ``data``. Raises :class:`pydantic.ValidationError`."""
        return self.schema.model_validate(data).model_dump(mode="json")

    def json_schema(self) -> dict:
        return self.schema.model_json_schema()


class ProductionEventCatalog:
    """Central registry of every supported production event type."""

    def __init__(self) -> None:
        self._entries: dict[str, EventCatalogEntry] = {}

    def register(
        self,
        code: str,
        schema: type[_StrictModel],
        *,
        version: int = 1,
        display_name: str | None = None,
        category: str = "operational",
        triggers_transition_to: str | None = None,
        metadata: dict | None = None,
        openapi_example: dict | None = None,
    ) -> EventCatalogEntry:
        entry = EventCatalogEntry(
            code=code.upper(),
            schema=schema,
            version=version,
            display_name=display_name or code.title(),
            category=category,
            triggers_transition_to=triggers_transition_to,
            metadata=metadata or {},
            openapi_example=openapi_example,
        )
        self._entries[entry.code] = entry
        return entry

    def get(self, code: str) -> EventCatalogEntry | None:
        return self._entries.get(code.upper())

    def require(self, code: str) -> EventCatalogEntry:
        entry = self.get(code)
        if entry is None:
            raise KeyError(f"Unknown production event type: {code!r}")
        return entry

    def codes(self) -> list[str]:
        return sorted(self._entries.keys())

    def as_openapi_catalog(self) -> list[dict]:
        return [
            {
                "code": e.code,
                "display_name": e.display_name,
                "category": e.category,
                "version": e.version,
                "triggers_transition_to": e.triggers_transition_to,
                "schema": e.json_schema(),
                "metadata": e.metadata,
                "openapi_example": e.openapi_example,
            }
            for e in sorted(self._entries.values(), key=lambda x: x.code)
        ]


# --------------------------------------------------------------------- #
# The single, process-wide catalog. Import this — do NOT construct new
# instances at call sites.
# --------------------------------------------------------------------- #
CATALOG = ProductionEventCatalog()

CATALOG.register(
    "STOCKING",
    StockingEventSchema,
    version=2,
    display_name="Stocking",
    category="lifecycle",
    triggers_transition_to="stocked",
    metadata={"description": "Initial stocking of a batch into a unit."},
    openapi_example={
        "species_code": "WHITE_SHRIMP",
        "quantity": 25000,
        "average_weight": 0.02,
        "weight_unit": "g",
        "source": "Central Hatchery / Batch H-2026-14",
        "stocked_at": "2026-02-08T08:00:00+00:00",
        "notes": "PL10, arrived in good condition.",
    },
)
CATALOG.register(
    "FEEDING",
    FeedingEventSchema,
    version=2,
    display_name="Feeding",
    openapi_example={
        "feed_description": "Starter crumble 40% protein",
        "quantity": 6.5,
        "unit": "kg",
        "feeding_method": "broadcast",
        "feeding_round": 3,
    },
)
CATALOG.register(
    "MORTALITY",
    MortalityEventSchema,
    version=2,
    display_name="Mortality",
    openapi_example={
        "count": 120,
        "suspected_cause": "low DO overnight",
        "disposal_method": "burial",
        "observed_at": "2026-02-15T06:00:00+00:00",
    },
)
CATALOG.register(
    "SAMPLING",
    SamplingEventSchema,
    version=2,
    display_name="Sampling",
    openapi_example={
        "sample_size": 30,
        "average_weight": 4.8,
        "minimum_weight": 3.9,
        "maximum_weight": 5.7,
        "weight_unit": "g",
        "estimated_population": 22800,
    },
)
CATALOG.register(
    "WATER_QUALITY",
    WaterQualityEventSchema,
    version=2,
    display_name="Water Quality",
    openapi_example={
        "temperature": 29.4,
        "ph": 7.9,
        "dissolved_oxygen": 5.6,
        "ammonia": 0.15,
        "nitrite": 0.02,
        "turbidity": 42,
        "measurement_units": {
            "temperature": "C",
            "dissolved_oxygen": "mg_l",
            "ammonia": "mg_l",
            "nitrite": "mg_l",
            "turbidity": "NTU",
        },
        "measured_at": "2026-02-16T06:15:00+00:00",
    },
)
CATALOG.register(
    "TRANSFER",
    TransferEventSchema,
    version=3,
    display_name="Transfer",
    openapi_example={
        "source_unit_id": "1c0c4bcf-16f7-4e9f-8b40-3a0f7a1c1a11",
        "destination_unit_id": "9f16f74c-9c2c-4e57-8bde-b3a9f0e6b2a5",
        "destination_batch_id": "6f98d0ec-442e-4f61-b8ba-a819c3d4e22b",
        "quantity": 5000,
        "average_weight": 2.8,
        "weight_unit": "g",
        "transfer_loss": 12,
        "transferred_at": "2026-02-20T09:00:00+00:00",
    },
)
CATALOG.register(
    "HARVEST",
    HarvestEventSchema,
    version=2,
    display_name="Harvest",
    category="lifecycle",
    triggers_transition_to="harvested",
    metadata={
        "description": "Harvest event. Batch transitions to HARVESTED only when is_final=true.",
        "transition_conditional_on": "is_final == true",
    },
    openapi_example={
        "quantity": 22400,
        "total_weight": 615.5,
        "average_weight": 27.4,
        "weight_unit": "kg",
        "harvest_type": "total",
        "is_final": True,
        "harvested_at": "2026-04-30T05:00:00+00:00",
    },
)


__all__ = [
    "CATALOG",
    "EventCatalogEntry",
    "FeedUnit",
    "FeedingEventSchema",
    "FeedingMethod",
    "HarvestEventSchema",
    "HarvestType",
    "MortalityDisposal",
    "MortalityEventSchema",
    "MortalityEvidence",
    "ProductionEventCatalog",
    "SamplingEventSchema",
    "StockingEventSchema",
    "TransferEventSchema",
    "ValidationError",
    "WaterQualityEventSchema",
    "WaterQualityUnits",
    "WeightUnit",
]
