"""APE Batch Projections.

Derived, read-only aggregates computed from the append-only event
stream on a single :class:`ProductionBatch`. Nothing here is a
source-of-truth field — everything is derived from
``production_events`` (and, where absolutely necessary, the batch's
own lifecycle columns for age).

Sprint 3 exposes:

* ``initial_stocked_quantity`` — sum of STOCKING quantities.
* ``cumulative_mortality`` — sum of MORTALITY counts + TRANSFER losses.
* ``cumulative_harvest`` — sum of HARVEST quantities.
* ``cumulative_transfer_out`` — sum of TRANSFER quantities.
* ``estimated_remaining_population`` -- either the most recent
  authoritative SAMPLING.estimated_population, or
  ``stocked - mortality - transfer_out - harvest``.
* ``latest_average_weight`` + ``weight_unit`` -- most recent
  SAMPLING.average_weight (falls back to STOCKING.average_weight).
* ``estimated_biomass_kg`` -- remaining population multiplied by
  normalised to kg.
* ``total_feed_kg`` — cumulative FEEDING quantity normalised to kg.
* ``survival_rate`` — remaining / initial, in [0, 1].
* ``batch_age_days`` — days since ``stocked_at`` (or ``planned_at``).
* ``latest_water_quality`` — most recent WATER_QUALITY event dict.

These are recomputed on demand; there is no separately editable
snapshot table. If perf ever demands a materialised view, it lives
here (owned by APE), not in a vertical module.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.models.production import ProductionBatch, ProductionEvent


@dataclass(frozen=True)
class BatchProjections:
    batch_id: str
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
    latest_water_quality: dict[str, Any] | None
    latest_sampling_at: datetime | None
    computed_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "initial_stocked_quantity": self.initial_stocked_quantity,
            "cumulative_mortality": self.cumulative_mortality,
            "cumulative_harvest": self.cumulative_harvest,
            "cumulative_transfer_out": self.cumulative_transfer_out,
            "cumulative_transfer_in": self.cumulative_transfer_in,
            "estimated_remaining_population": self.estimated_remaining_population,
            "latest_average_weight": self.latest_average_weight,
            "weight_unit": self.weight_unit,
            "estimated_biomass_kg": self.estimated_biomass_kg,
            "total_feed_kg": self.total_feed_kg,
            "survival_rate": self.survival_rate,
            "batch_age_days": self.batch_age_days,
            "latest_water_quality": self.latest_water_quality,
            "latest_sampling_at": (
                self.latest_sampling_at.isoformat() if self.latest_sampling_at else None
            ),
            "computed_at": self.computed_at.isoformat(),
        }


# ------------------------------------------------------------------ #
# Weight-unit normalisation
# ------------------------------------------------------------------ #
_WEIGHT_TO_KG = {"kg": 1.0, "g": 0.001}


def _to_kg(value: float, unit: str | None) -> float:
    if unit is None:
        return value
    return value * _WEIGHT_TO_KG.get(unit.lower(), 1.0)


def _to_aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def compute_batch_projections(
    batch: ProductionBatch,
    events: list[ProductionEvent],
    *,
    now: datetime | None = None,
) -> BatchProjections:
    """Compute derived projections for a batch from its event stream.

    ``events`` MUST be ordered by ``(performed_at, created_at, id)``.
    ``created_at`` is immutable insertion chronology for equal performed
    times; ``id`` is only a deterministic final tie-breaker.
    """
    now = now or datetime.now(UTC)

    initial_stocked = 0
    cumulative_mortality = 0
    cumulative_harvest = 0
    cumulative_transfer_out = 0
    cumulative_transfer_in = 0
    remaining = 0
    total_feed_kg = 0.0
    latest_average_weight: float | None = None
    latest_weight_unit: str | None = None
    stocking_weight_g_seed: float | None = None
    latest_sampling_estimated_pop: int | None = None
    latest_sampling_at: datetime | None = None
    latest_water_quality: dict[str, Any] | None = None
    stocking_seen_at: datetime | None = None

    for evt in events:
        data = evt.data or {}
        code = evt.event_type
        if code == "STOCKING":
            stocked_quantity = int(data.get("quantity", 0))
            initial_stocked += stocked_quantity
            remaining += stocked_quantity
            if stocking_seen_at is None:
                stocking_seen_at = evt.performed_at
            # Cache first-stocking weight for later fallback
            if stocking_weight_g_seed is None:
                stocking_weight_g_seed = (
                    _to_kg(
                        float(data.get("average_weight", 0) or 0),
                        data.get("weight_unit"),
                    )
                    * 1000
                )  # keep in grams for display consistency
                latest_average_weight = float(data.get("average_weight", 0) or 0)
                latest_weight_unit = data.get("weight_unit") or "g"
        elif code == "MORTALITY":
            count = int(data.get("count", 0) or 0)
            cumulative_mortality += count
            remaining -= count
        elif code == "TRANSFER":
            quantity = int(data.get("quantity", 0) or 0)
            raw_role = getattr(evt, "transfer_role", None)
            role = getattr(raw_role, "value", raw_role)
            if role == "in":
                cumulative_transfer_in += quantity
                remaining += quantity
                if data.get("average_weight") is not None:
                    latest_average_weight = float(data["average_weight"])
                    latest_weight_unit = data.get("weight_unit") or "g"
            else:  # Legacy source-only TRANSFER rows remain OUT-compatible.
                loss = int(data.get("transfer_loss", 0) or 0)
                cumulative_transfer_out += quantity
                cumulative_mortality += loss
                remaining -= quantity + loss
        elif code == "HARVEST":
            quantity = int(data.get("quantity", 0) or 0)
            cumulative_harvest += quantity
            remaining -= quantity
        elif code == "FEEDING":
            qty = float(data.get("quantity", 0) or 0)
            unit = data.get("unit") or "kg"
            total_feed_kg += _to_kg(qty, unit)
        elif code == "SAMPLING":
            avg = data.get("average_weight")
            if avg is not None:
                latest_average_weight = float(avg)
                latest_weight_unit = data.get("weight_unit") or latest_weight_unit or "g"
            if data.get("estimated_population") is not None:
                latest_sampling_estimated_pop = int(data["estimated_population"])
                remaining = latest_sampling_estimated_pop
            latest_sampling_at = _to_aware(evt.performed_at)
        elif code == "WATER_QUALITY":
            latest_water_quality = {
                "temperature": data.get("temperature"),
                "ph": data.get("ph"),
                "dissolved_oxygen": data.get("dissolved_oxygen"),
                "ammonia": data.get("ammonia"),
                "nitrite": data.get("nitrite"),
                "turbidity": data.get("turbidity"),
                "measurement_units": data.get("measurement_units"),
                "measured_at": data.get("measured_at"),
            }

    # Estimated remaining population: authoritative sampling wins;
    # otherwise fall back to the mass-balance formula.
    remaining = max(remaining, 0)

    biomass_kg: float | None
    if latest_average_weight is not None and latest_weight_unit is not None:
        biomass_kg = _to_kg(latest_average_weight, latest_weight_unit) * remaining
    else:
        biomass_kg = None

    population_introduced = initial_stocked + cumulative_transfer_in
    survival = (
        max(0.0, min(1.0, remaining / population_introduced)) if population_introduced > 0 else None
    )

    # Batch age: prefer stocked_at → then first STOCKING event →
    # then planned_at (in that order). Normalise everything to
    # UTC-aware so SQLite (which forgets tz) and Postgres agree.
    anchor = (
        _to_aware(batch.stocked_at) or _to_aware(stocking_seen_at) or _to_aware(batch.planned_at)
    )
    age_days = max((now - anchor).days, 0) if anchor is not None else None

    return BatchProjections(
        batch_id=str(batch.id),
        initial_stocked_quantity=initial_stocked,
        cumulative_mortality=cumulative_mortality,
        cumulative_harvest=cumulative_harvest,
        cumulative_transfer_out=cumulative_transfer_out,
        cumulative_transfer_in=cumulative_transfer_in,
        estimated_remaining_population=remaining,
        latest_average_weight=latest_average_weight,
        weight_unit=latest_weight_unit,
        estimated_biomass_kg=biomass_kg,
        total_feed_kg=round(total_feed_kg, 4),
        survival_rate=survival,
        batch_age_days=age_days,
        latest_water_quality=latest_water_quality,
        latest_sampling_at=latest_sampling_at,
        computed_at=now,
    )


__all__ = ["BatchProjections", "compute_batch_projections"]
