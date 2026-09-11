'use client';

/**
 * Deliberate operational forms for STOCKING / FEEDING / MORTALITY,
 * a dedicated identity-safe TRANSFER form, and a controlled catalog-driven
 * form for the remaining Sprint 3 event types (SAMPLING / WATER_QUALITY / HARVEST).
 *
 * We intentionally do NOT drive stocking / feeding / mortality
 * through a generic JSON-schema form renderer — those are the
 * primary operational workflows and deserve first-class UX.
 */

import { FormEvent, useEffect, useRef, useState } from 'react';
import { ApiError, apiFetch, apiFetchResult } from '@/lib/api';
import { parseApiErrors } from '@/lib/api-errors';
import type { EventCatalogEntry, ProductionEvent, ProductionUnit } from '@/lib/types';
import type {
  DashboardInventoryItem,
  DashboardLot,
  DashboardWarehouse,
} from '@/lib/inventory-dashboard';

interface EventFormProps {
  batchId: string;
  onCreated(evt: ProductionEvent): void;
  onCancel(): void;
  onUnauthenticated?(): void;
  eventType: 'STOCKING' | 'FEEDING' | 'MORTALITY';
}

interface CatalogFormProps {
  batchId: string;
  onCreated(evt: ProductionEvent): void;
  onCancel(): void;
  onUnauthenticated?(): void;
  entry: EventCatalogEntry;
}

interface TransferFormProps extends CatalogFormProps {
  sourceUnit: ProductionUnit;
}

interface CreateResponse {
  event: ProductionEvent;
  replay: boolean;
}

interface FeedingFormProps extends Omit<EventFormProps, 'eventType'> {
  organizationId: string;
  farmId: string;
}

interface EligibleFeedLot {
  id: string;
  label: string;
}

interface EligibleTransferUnit {
  id: string;
  unit_id: string;
  label: string;
}

async function postEvent(
  batchId: string,
  body: { event_type: string; data: Record<string, unknown> },
  idempotencyKey: string,
): Promise<CreateResponse> {
  const { data, response } = await apiFetchResult<ProductionEvent>(
    `/v1/batches/${batchId}/events`,
    {
      method: 'POST',
      headers: {
        'Idempotency-Key': idempotencyKey,
      },
      body: JSON.stringify(body),
    },
  );
  return {
    event: data,
    replay: response.headers.get('X-Idempotent-Replay') === 'true',
  };
}

function nowLocalIso(): string {
  const d = new Date();
  d.setSeconds(0, 0);
  return d.toISOString().slice(0, 16);
}

function extractServerMessage(err: unknown): string {
  return parseApiErrors(err).generalErrors[0] ?? 'Request failed.';
}

function eventErrorMessage(err: unknown, onUnauthenticated?: () => void): string {
  if (err instanceof ApiError && err.status === 401) onUnauthenticated?.();
  return extractServerMessage(err);
}

/* ================================================================= */
/* STOCKING — deliberate                                              */
/* ================================================================= */

export function StockingForm({
  batchId,
  onCreated,
  onCancel,
  onUnauthenticated,
}: Omit<EventFormProps, 'eventType'>) {
  const [species, setSpecies] = useState('WHITE_SHRIMP');
  const [quantity, setQuantity] = useState('25000');
  const [avgWeight, setAvgWeight] = useState('0.02');
  const [weightUnit, setWeightUnit] = useState<'g' | 'kg'>('g');
  const [source, setSource] = useState('');
  const [stockedAt, setStockedAt] = useState(nowLocalIso());
  const [notes, setNotes] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!confirm) {
      setError('Please confirm this stocking event before saving.');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const { event } = await postEvent(
        batchId,
        {
          event_type: 'STOCKING',
          data: {
            species_code: species.trim(),
            quantity: Number(quantity),
            average_weight: Number(avgWeight),
            weight_unit: weightUnit,
            source: source.trim() || null,
            stocked_at: new Date(stockedAt).toISOString(),
            ...(notes.trim() ? { notes: notes.trim() } : {}),
          },
        },
        `stock-${batchId}-${Date.now()}-${crypto.randomUUID()}`,
      );
      onCreated(event);
    } catch (err) {
      setError(eventErrorMessage(err, onUnauthenticated));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="space-y-4" data-testid="stocking-form">
      <h3 className="font-display text-lg">Record stocking</h3>
      <p className="text-xs text-muted-foreground">
        Stocking transitions the batch to <strong>Stocked</strong>. This is irreversible.
      </p>
      <label className="block text-sm">
        Species code
        <input
          data-testid="stocking-species"
          className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
          value={species}
          onChange={(e) => setSpecies(e.target.value)}
          required
        />
      </label>
      <div className="grid grid-cols-2 gap-3">
        <label className="block text-sm">
          Quantity (individuals)
          <input
            data-testid="stocking-quantity"
            type="number"
            min={1}
            className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
            value={quantity}
            onChange={(e) => setQuantity(e.target.value)}
            required
          />
        </label>
        <label className="block text-sm">
          Average weight
          <div className="mt-1 flex gap-1">
            <input
              data-testid="stocking-avg-weight"
              type="number"
              min={0}
              step="0.001"
              className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
              value={avgWeight}
              onChange={(e) => setAvgWeight(e.target.value)}
              required
            />
            <select
              data-testid="stocking-weight-unit"
              className="rounded-md border border-border bg-background px-2 text-sm"
              value={weightUnit}
              onChange={(e) => setWeightUnit(e.target.value as 'g' | 'kg')}
            >
              <option value="g">g</option>
              <option value="kg">kg</option>
            </select>
          </div>
        </label>
      </div>
      <label className="block text-sm">
        Source (hatchery, supplier)
        <input
          data-testid="stocking-source"
          className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
          value={source}
          onChange={(e) => setSource(e.target.value)}
        />
      </label>
      <label className="block text-sm">
        Stocked at
        <input
          data-testid="stocking-stocked-at"
          type="datetime-local"
          className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
          value={stockedAt}
          onChange={(e) => setStockedAt(e.target.value)}
          required
        />
      </label>
      <label className="block text-sm">
        Notes
        <textarea
          data-testid="stocking-notes"
          rows={2}
          className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
        />
      </label>
      <label className="flex items-start gap-2 text-xs text-muted-foreground">
        <input
          type="checkbox"
          data-testid="stocking-confirm"
          className="mt-0.5"
          checked={confirm}
          onChange={(e) => setConfirm(e.target.checked)}
        />
        I confirm the batch has been stocked with these values. This is an append-only operational
        event and cannot be edited later.
      </label>
      {error && (
        <p
          className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive"
          data-testid="stocking-error"
        >
          {error}
        </p>
      )}
      <div className="flex justify-end gap-2">
        <button
          type="button"
          onClick={onCancel}
          className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-secondary"
        >
          Cancel
        </button>
        <button
          type="submit"
          disabled={busy}
          data-testid="stocking-submit"
          className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-60"
        >
          {busy ? 'Saving…' : 'Record stocking'}
        </button>
      </div>
    </form>
  );
}

/* ================================================================= */
/* FEEDING — deliberate                                               */
/* ================================================================= */

export function FeedingForm({
  batchId,
  organizationId,
  farmId,
  onCreated,
  onCancel,
  onUnauthenticated,
}: FeedingFormProps) {
  const [description, setDescription] = useState('Grower crumble 35%');
  const [quantity, setQuantity] = useState('2.5');
  const [unit, setUnit] = useState<'g' | 'kg'>('kg');
  const [method, setMethod] = useState('broadcast');
  const [round, setRound] = useState('1');
  const [notes, setNotes] = useState('');
  const [lotId, setLotId] = useState('');
  const [eligibleLots, setEligibleLots] = useState<EligibleFeedLot[]>([]);
  const [lotsLoading, setLotsLoading] = useState(true);
  const [lotsError, setLotsError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function loadEligibleLots() {
      setLotsLoading(true);
      setLotsError(null);
      try {
        const [warehouses, items] = await Promise.all([
          apiFetch<DashboardWarehouse[]>(
            `/v1/organizations/${organizationId}/warehouses?operational_only=true`,
          ),
          apiFetch<DashboardInventoryItem[]>(
            `/v1/organizations/${organizationId}/inventory-items?operational_only=true`,
          ),
        ]);
        const itemById = new Map(
          items
            .filter(
              (item) =>
                item.is_active &&
                item.category === 'feed' &&
                (item.canonical_unit === 'kg' || item.canonical_unit === 'g'),
            )
            .map((item) => [item.id, item]),
        );
        const visibleWarehouses = warehouses.filter(
          (warehouse) =>
            warehouse.status === 'active' &&
            (warehouse.farm_id === null || warehouse.farm_id === farmId),
        );
        const lotsByWarehouse = await Promise.all(
          visibleWarehouses.map(async (warehouse) => ({
            warehouse,
            lots: await apiFetch<DashboardLot[]>(`/v1/warehouses/${warehouse.id}/lots`),
          })),
        );
        const options = lotsByWarehouse.flatMap(({ warehouse, lots }) =>
          lots.flatMap((lot) => {
            const item = itemById.get(lot.item_id);
            const balance = Number(lot.balance);
            if (!item || !Number.isFinite(balance) || balance <= 0) return [];
            return [
              {
                id: lot.id,
                label: `${item.name} · lot ${lot.lot_code} · ${balance} ${lot.balance_unit} · ${warehouse.name}`,
              },
            ];
          }),
        );
        if (!cancelled) setEligibleLots(options);
      } catch (err) {
        if (!cancelled) {
          if (err instanceof ApiError && err.status === 401) onUnauthenticated?.();
          setLotsError(extractServerMessage(err));
          setEligibleLots([]);
        }
      } finally {
        if (!cancelled) setLotsLoading(false);
      }
    }
    void loadEligibleLots();
    return () => {
      cancelled = true;
    };
  }, [farmId, onUnauthenticated, organizationId]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const selectedLotId = eligibleLots.some((lot) => lot.id === lotId) ? lotId : '';
      const trimmedDesc = description.trim();
      // A lot id can only originate from the scoped server-backed selection.
      const feedRef: Record<string, unknown> = selectedLotId
        ? { inventory_lot_id: selectedLotId }
        : { feed_description: trimmedDesc };
      const { event } = await postEvent(
        batchId,
        {
          event_type: 'FEEDING',
          data: {
            ...feedRef,
            quantity: Number(quantity),
            unit,
            feeding_method: method,
            ...(round ? { feeding_round: Number(round) } : {}),
            ...(notes.trim() ? { notes: notes.trim() } : {}),
          },
        },
        `feed-${batchId}-${Date.now()}-${crypto.randomUUID()}`,
      );
      onCreated(event);
    } catch (err) {
      setError(eventErrorMessage(err, onUnauthenticated));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="space-y-4" data-testid="feeding-form">
      <h3 className="font-display text-lg">Record feeding</h3>

      <label className="block text-sm">
        Feed lot (optional — deducts inventory)
        <select
          data-testid="feeding-lot-id"
          className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
          value={lotId}
          onChange={(e) => setLotId(e.target.value)}
          disabled={lotsLoading || Boolean(lotsError)}
        >
          <option value="">No inventory lot — record ad-hoc feeding</option>
          {eligibleLots.map((lot) => (
            <option key={lot.id} value={lot.id}>
              {lot.label}
            </option>
          ))}
        </select>
      </label>

      {lotsLoading && (
        <p className="text-xs text-muted-foreground" data-testid="feeding-lots-loading">
          Loading eligible feed lots…
        </p>
      )}
      {!lotsLoading && !lotsError && eligibleLots.length === 0 && (
        <p
          className="rounded-md bg-secondary px-3 py-2 text-xs text-muted-foreground"
          data-testid="feeding-lots-empty"
        >
          No eligible feed lots are available for this farm. You can record an ad-hoc feeding with a
          description, or receive feed stock in Inventory first.
        </p>
      )}
      {lotsError && (
        <p className="text-xs text-destructive" data-testid="feeding-lots-error">
          Feed lots could not be loaded: {lotsError}. Ad-hoc feeding remains available.
        </p>
      )}

      <label className="block text-sm">
        Feed description {lotId.trim() ? '(ignored when a lot is provided)' : ''}
        <input
          data-testid="feeding-description"
          className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          disabled={Boolean(lotId.trim())}
          required={!lotId.trim()}
        />
      </label>
      <div className="grid grid-cols-2 gap-3">
        <label className="block text-sm">
          Quantity
          <div className="mt-1 flex gap-1">
            <input
              data-testid="feeding-quantity"
              type="number"
              min={0.001}
              step="0.001"
              className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
              value={quantity}
              onChange={(e) => setQuantity(e.target.value)}
              required
            />
            <select
              data-testid="feeding-unit"
              className="rounded-md border border-border bg-background px-2 text-sm"
              value={unit}
              onChange={(e) => setUnit(e.target.value as 'g' | 'kg')}
            >
              <option value="kg">kg</option>
              <option value="g">g</option>
            </select>
          </div>
        </label>
        <label className="block text-sm">
          Feeding round
          <input
            data-testid="feeding-round"
            type="number"
            min={1}
            className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
            value={round}
            onChange={(e) => setRound(e.target.value)}
          />
        </label>
      </div>
      <label className="block text-sm">
        Method
        <select
          data-testid="feeding-method"
          className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
          value={method}
          onChange={(e) => setMethod(e.target.value)}
        >
          <option value="broadcast">Broadcast</option>
          <option value="tray">Tray</option>
          <option value="automatic">Automatic</option>
          <option value="hand">By hand</option>
        </select>
      </label>
      <label className="block text-sm">
        Notes
        <textarea
          data-testid="feeding-notes"
          rows={2}
          className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
        />
      </label>
      {error && (
        <p
          className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive"
          data-testid="feeding-error"
        >
          {error}
        </p>
      )}
      <div className="flex justify-end gap-2">
        <button
          type="button"
          onClick={onCancel}
          className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-secondary"
        >
          Cancel
        </button>
        <button
          type="submit"
          disabled={busy}
          data-testid="feeding-submit"
          className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-60"
        >
          {busy ? 'Saving…' : 'Record feeding'}
        </button>
      </div>
    </form>
  );
}

/* ================================================================= */
/* MORTALITY — deliberate                                             */
/* ================================================================= */

export function MortalityForm({
  batchId,
  onCreated,
  onCancel,
  onUnauthenticated,
}: Omit<EventFormProps, 'eventType'>) {
  const [count, setCount] = useState('10');
  const [cause, setCause] = useState('');
  const [disposal, setDisposal] = useState('burial');
  const [observedAt, setObservedAt] = useState(nowLocalIso());
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!confirm) {
      setError('Confirm before recording mortality — this cannot be edited later.');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const { event } = await postEvent(
        batchId,
        {
          event_type: 'MORTALITY',
          data: {
            count: Number(count),
            ...(cause.trim() ? { suspected_cause: cause.trim() } : {}),
            disposal_method: disposal,
            observed_at: new Date(observedAt).toISOString(),
          },
        },
        `mort-${batchId}-${Date.now()}-${crypto.randomUUID()}`,
      );
      onCreated(event);
    } catch (err) {
      setError(eventErrorMessage(err, onUnauthenticated));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="space-y-4" data-testid="mortality-form">
      <h3 className="font-display text-lg">Record mortality</h3>
      <p className="text-xs text-muted-foreground">
        Mortality is deducted from the estimated remaining population. If the count exceeds
        population, the platform will reject the entry — use the sampling workflow to reconcile the
        count first.
      </p>
      <label className="block text-sm">
        Number of individuals
        <input
          data-testid="mortality-count"
          type="number"
          min={1}
          className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
          value={count}
          onChange={(e) => setCount(e.target.value)}
          required
        />
      </label>
      <label className="block text-sm">
        Suspected cause
        <input
          data-testid="mortality-cause"
          className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
          value={cause}
          onChange={(e) => setCause(e.target.value)}
        />
      </label>
      <label className="block text-sm">
        Disposal method
        <select
          data-testid="mortality-disposal"
          className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
          value={disposal}
          onChange={(e) => setDisposal(e.target.value)}
        >
          <option value="burial">Burial</option>
          <option value="incineration">Incineration</option>
          <option value="compost">Compost</option>
          <option value="rendering">Rendering</option>
          <option value="other">Other</option>
        </select>
      </label>
      <label className="block text-sm">
        Observed at
        <input
          data-testid="mortality-observed-at"
          type="datetime-local"
          className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
          value={observedAt}
          onChange={(e) => setObservedAt(e.target.value)}
          required
        />
      </label>
      <label className="flex items-start gap-2 text-xs text-muted-foreground">
        <input
          type="checkbox"
          data-testid="mortality-confirm"
          className="mt-0.5"
          checked={confirm}
          onChange={(e) => setConfirm(e.target.checked)}
        />
        I confirm this mortality count is correct and append-only.
      </label>
      {error && (
        <p
          className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive"
          data-testid="mortality-error"
        >
          {error}
        </p>
      )}
      <div className="flex justify-end gap-2">
        <button
          type="button"
          onClick={onCancel}
          className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-secondary"
        >
          Cancel
        </button>
        <button
          type="submit"
          disabled={busy}
          data-testid="mortality-submit"
          className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-60"
        >
          {busy ? 'Saving…' : 'Record mortality'}
        </button>
      </div>
    </form>
  );
}

/* ================================================================= */
/* TRANSFER — deliberate identity-safe operational form              */
/* ================================================================= */

interface JsonSchema {
  properties?: Record<string, JsonSchema>;
  $defs?: Record<string, JsonSchema>;
  required?: string[];
  type?: string;
  enum?: string[];
  format?: string;
  description?: string;
  minimum?: number;
  maximum?: number;
  default?: unknown;
  $ref?: string;
  anyOf?: JsonSchema[];
}

function resolvedSchema(root: JsonSchema, prop: JsonSchema): JsonSchema {
  let resolved = prop;
  if (prop.$ref?.startsWith('#/$defs/')) {
    resolved = root.$defs?.[prop.$ref.slice('#/$defs/'.length)] ?? prop;
  }
  const concrete = resolved.anyOf?.find((candidate) => candidate.type !== 'null');
  return concrete ? { ...resolved, ...concrete } : resolved;
}

function catalogInitialValue(entry: EventCatalogEntry, key: string, fallback = ''): string {
  const schema = entry.schema as JsonSchema;
  const prop = resolvedSchema(schema, schema.properties?.[key] ?? {});
  const value = prop.default ?? entry.openapi_example?.[key] ?? fallback;
  return value == null || typeof value === 'object' ? fallback : String(value);
}

function transferLocalDateTimeValue(date: Date): string {
  const pad = (value: number) => String(value).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(
    date.getHours(),
  )}:${pad(date.getMinutes())}`;
}

function safeTransferMessage(message: string): string {
  return message
    .replaceAll('source_unit_id', 'Source unit')
    .replaceAll('destination_unit_id', 'Destination unit')
    .replaceAll('average_weight', 'Average weight')
    .replaceAll('weight_unit', 'Weight unit')
    .replaceAll('transfer_loss', 'Transfer loss')
    .replaceAll('transferred_at', 'Transferred at')
    .replace(
      /\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b/gi,
      'selected unit',
    );
}

export function TransferEventForm({
  batchId,
  entry,
  sourceUnit,
  onCreated,
  onCancel,
  onUnauthenticated,
}: TransferFormProps) {
  const schema = entry.schema as JsonSchema;
  const properties = schema.properties ?? {};
  const onUnauthenticatedRef = useRef(onUnauthenticated);
  onUnauthenticatedRef.current = onUnauthenticated;
  const [destinations, setDestinations] = useState<EligibleTransferUnit[]>([]);
  const [destinationsLoading, setDestinationsLoading] = useState(true);
  const [destinationsError, setDestinationsError] = useState<string | null>(null);
  const [destinationId, setDestinationId] = useState('');
  const [quantity, setQuantity] = useState(() => catalogInitialValue(entry, 'quantity'));
  const [averageWeight, setAverageWeight] = useState(() =>
    catalogInitialValue(entry, 'average_weight'),
  );
  const [weightUnit, setWeightUnit] = useState(() =>
    catalogInitialValue(entry, 'weight_unit', 'g'),
  );
  const [transferLoss, setTransferLoss] = useState(() =>
    catalogInitialValue(entry, 'transfer_loss', '0'),
  );
  const [transferredAt, setTransferredAt] = useState(() => transferLocalDateTimeValue(new Date()));
  const [notes, setNotes] = useState(() => catalogInitialValue(entry, 'notes'));
  const submittingRef = useRef(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});

  useEffect(() => {
    let cancelled = false;
    async function loadDestinations() {
      setDestinationsLoading(true);
      setDestinationsError(null);
      setDestinations([]);
      setDestinationId('');
      try {
        const options = await apiFetch<EligibleTransferUnit[]>(
          `/v1/batches/${batchId}/transfer-destinations`,
        );
        if (!cancelled) setDestinations(options);
      } catch (loadError) {
        if (!cancelled) {
          if (loadError instanceof ApiError && loadError.status === 401) {
            onUnauthenticatedRef.current?.();
          }
          if (loadError instanceof ApiError && loadError.status === 403) {
            setDestinationsError('You do not have permission to record transfers.');
          } else {
            const parsed = parseApiErrors(loadError);
            setDestinationsError(
              safeTransferMessage(
                parsed.generalErrors[0] ?? 'Eligible destination units could not be loaded.',
              ),
            );
          }
          setDestinations([]);
        }
      } finally {
        if (!cancelled) setDestinationsLoading(false);
      }
    }
    void loadDestinations();
    return () => {
      cancelled = true;
    };
  }, [batchId]);

  function descriptionFor(key: string): string | undefined {
    return resolvedSchema(schema, properties[key] ?? {}).description;
  }

  function fieldError(key: string) {
    const message = fieldErrors[key];
    if (!message) return null;
    return (
      <span
        id={`transfer-${key}-error`}
        role="alert"
        className="mt-1 block text-xs text-destructive"
      >
        {message}
      </span>
    );
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (submittingRef.current) return;
    const selectedDestination = destinations.find((unit) => unit.id === destinationId);
    if (!selectedDestination) {
      setFieldErrors({ destination_batch_id: 'Select an eligible destination batch.' });
      return;
    }
    submittingRef.current = true;
    setBusy(true);
    setError(null);
    setFieldErrors({});
    try {
      const data: Record<string, unknown> = {
        source_unit_id: sourceUnit.id,
        destination_unit_id: selectedDestination.unit_id,
        destination_batch_id: selectedDestination.id,
        quantity: Number(quantity),
        weight_unit: weightUnit,
        transfer_loss: Number(transferLoss),
        transferred_at: new Date(transferredAt).toISOString(),
      };
      if (averageWeight !== '') data.average_weight = Number(averageWeight);
      if (notes.trim()) data.notes = notes.trim();
      const { event } = await postEvent(
        batchId,
        { event_type: entry.code, data },
        `transfer-${batchId}-${Date.now()}-${crypto.randomUUID()}`,
      );
      onCreated(event);
    } catch (submitError) {
      if (submitError instanceof ApiError && submitError.status === 401) onUnauthenticated?.();
      const parsed = parseApiErrors(submitError, new Set(Object.keys(properties)));
      setFieldErrors(
        Object.fromEntries(
          Object.entries(parsed.fieldErrors).map(([key, message]) => [
            key,
            safeTransferMessage(message),
          ]),
        ),
      );
      setError(parsed.generalErrors[0] ? safeTransferMessage(parsed.generalErrors[0]) : null);
    } finally {
      submittingRef.current = false;
      setBusy(false);
    }
  }

  const sourceLabel = sourceUnit.name
    ? `${sourceUnit.code} — ${sourceUnit.name}`
    : sourceUnit.code || 'Current production unit';
  const weightUnits = resolvedSchema(schema, properties.weight_unit ?? {}).enum ?? ['g', 'kg'];
  const destinationUnavailable =
    destinationsLoading || Boolean(destinationsError) || destinations.length === 0;

  return (
    <form onSubmit={submit} className="space-y-3" data-testid="transfer-form">
      <h3 className="font-display text-lg">Record {entry.display_name}</h3>
      <div className="text-sm">
        <span className="block">Source unit</span>
        <p
          className="mt-1 rounded-md border border-border bg-secondary px-3 py-2"
          data-testid="transfer-source-unit"
        >
          {sourceLabel}
        </p>
        {fieldError('source_unit_id')}
      </div>

      <label className="block text-sm">
        Destination batch <span className="text-destructive">*</span>
        <select
          data-testid="transfer-destination-unit"
          className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
          value={destinationId}
          onChange={(e) => {
            setDestinationId(e.target.value);
            setFieldErrors((prev) => ({ ...prev, destination_batch_id: '' }));
          }}
          disabled={destinationUnavailable || busy}
          required
          aria-invalid={Boolean(fieldErrors.destination_batch_id)}
          aria-describedby={
            fieldErrors.destination_batch_id ? 'transfer-destination_batch_id-error' : undefined
          }
        >
          <option value="">Select an eligible destination batch…</option>
          {destinations.map((unit) => (
            <option key={unit.id} value={unit.id}>
              {unit.label}
            </option>
          ))}
        </select>
        {fieldError('destination_batch_id')}
      </label>
      {destinationsLoading && (
        <p className="text-xs text-muted-foreground" data-testid="transfer-destinations-loading">
          Loading eligible destination batches…
        </p>
      )}
      {!destinationsLoading && !destinationsError && destinations.length === 0 && (
        <p
          className="rounded-md bg-secondary px-3 py-2 text-xs text-muted-foreground"
          data-testid="transfer-destinations-empty"
        >
          No eligible destination batches are available in this farm.
        </p>
      )}
      {destinationsError && (
        <p className="text-xs text-destructive" data-testid="transfer-destinations-error">
          Destination batches could not be loaded: {destinationsError}
        </p>
      )}

      <div className="grid gap-3 sm:grid-cols-2">
        <label className="block text-sm">
          Quantity (individuals) <span className="text-destructive">*</span>
          <input
            data-testid="transfer-quantity"
            type="number"
            min={resolvedSchema(schema, properties.quantity ?? {}).minimum ?? 1}
            className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
            value={quantity}
            onChange={(e) => setQuantity(e.target.value)}
            required
          />
          {fieldError('quantity')}
          {descriptionFor('quantity') && (
            <span className="mt-0.5 block text-xs text-muted-foreground">
              {descriptionFor('quantity')}
            </span>
          )}
        </label>
        <label className="block text-sm">
          Average weight
          <input
            data-testid="transfer-average-weight"
            type="number"
            min={resolvedSchema(schema, properties.average_weight ?? {}).minimum ?? 0}
            step="0.001"
            className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
            value={averageWeight}
            onChange={(e) => setAverageWeight(e.target.value)}
          />
          {fieldError('average_weight')}
        </label>
        <label className="block text-sm">
          Weight unit
          <select
            data-testid="transfer-weight-unit"
            className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
            value={weightUnit}
            onChange={(e) => setWeightUnit(e.target.value)}
          >
            {weightUnits.map((unit) => (
              <option key={unit} value={unit}>
                {unit}
              </option>
            ))}
          </select>
          {fieldError('weight_unit')}
        </label>
        <label className="block text-sm">
          Transfer loss
          <input
            data-testid="transfer-loss"
            type="number"
            min={resolvedSchema(schema, properties.transfer_loss ?? {}).minimum ?? 0}
            className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
            value={transferLoss}
            onChange={(e) => setTransferLoss(e.target.value)}
          />
          {fieldError('transfer_loss')}
          {descriptionFor('transfer_loss') && (
            <span className="mt-0.5 block text-xs text-muted-foreground">
              {descriptionFor('transfer_loss')}
            </span>
          )}
        </label>
      </div>
      <label className="block text-sm">
        Transferred at <span className="text-destructive">*</span>
        <input
          data-testid="transfer-transferred-at"
          type="datetime-local"
          className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
          value={transferredAt}
          onChange={(e) => setTransferredAt(e.target.value)}
          required
        />
        {fieldError('transferred_at')}
      </label>
      <label className="block text-sm">
        Notes
        <textarea
          data-testid="transfer-notes"
          rows={2}
          className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
        />
        {fieldError('notes')}
      </label>
      {error && (
        <p
          className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive"
          data-testid="transfer-error"
        >
          {error}
        </p>
      )}
      <div className="flex justify-end gap-2">
        <button
          type="button"
          onClick={onCancel}
          className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-secondary"
        >
          Cancel
        </button>
        <button
          type="submit"
          disabled={busy || destinationUnavailable || !destinationId}
          data-testid="transfer-submit"
          className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-60"
        >
          {busy ? 'Saving…' : 'Record transfer'}
        </button>
      </div>
    </form>
  );
}

/* ================================================================= */
/* Catalog-driven fallback (SAMPLING / WATER_QUALITY / HARVEST)      */
/* ================================================================= */

function inputTypeFor(prop: JsonSchema): 'text' | 'number' | 'datetime-local' | 'checkbox' {
  if (prop.type === 'number' || prop.type === 'integer') return 'number';
  if (prop.type === 'boolean') return 'checkbox';
  if (prop.format === 'date-time') return 'datetime-local';
  return 'text';
}

export function CatalogEventForm({
  batchId,
  onCreated,
  onCancel,
  onUnauthenticated,
  entry,
}: CatalogFormProps) {
  const schema = entry.schema as JsonSchema;
  const properties = schema.properties ?? {};
  const required = new Set(schema.required ?? []);
  const [values, setValues] = useState<Record<string, string>>(() => {
    const example = (entry.openapi_example ?? {}) as Record<string, unknown>;
    const seed: Record<string, string> = {};
    for (const key of Object.keys(properties)) {
      const v = example[key];
      if (v == null) continue;
      if (typeof v === 'object') continue; // nested — leave blank
      seed[key] = String(v);
    }
    return seed;
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});

  function set(key: string, val: string) {
    setValues((prev) => ({ ...prev, [key]: val }));
    setFieldErrors((prev) => ({ ...prev, [key]: '' }));
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setFieldErrors({});
    try {
      const data: Record<string, unknown> = {};
      for (const [key, prop] of Object.entries(properties)) {
        const raw = values[key];
        if (raw === undefined || raw === '') {
          if (required.has(key)) throw new Error(`${key} is required.`);
          continue;
        }
        if (prop.type === 'number' || prop.type === 'integer') data[key] = Number(raw);
        else if (prop.type === 'boolean') data[key] = raw === 'true';
        else if (prop.format === 'date-time') data[key] = new Date(raw).toISOString();
        else data[key] = raw;
      }
      // Seed nested defaults from the example (e.g. WATER_QUALITY.measurement_units)
      const example = (entry.openapi_example ?? {}) as Record<string, unknown>;
      for (const [key, val] of Object.entries(example)) {
        if (val != null && typeof val === 'object' && !(key in data)) data[key] = val;
      }
      const { event } = await postEvent(
        batchId,
        { event_type: entry.code, data },
        `${entry.code.toLowerCase()}-${batchId}-${Date.now()}-${crypto.randomUUID()}`,
      );
      onCreated(event);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) onUnauthenticated?.();
      const visibleFields = new Set(
        Object.entries(properties)
          .filter(([, prop]) => prop.type !== 'object' && !prop.$ref)
          .map(([key]) => key),
      );
      const parsed = parseApiErrors(err, visibleFields);
      setFieldErrors(parsed.fieldErrors);
      setError(parsed.generalErrors[0] ?? null);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="space-y-3" data-testid={`catalog-form-${entry.code}`}>
      <h3 className="font-display text-lg">Record {entry.display_name}</h3>
      <p className="text-xs text-muted-foreground">
        Fields defined by the platform event catalog. Nested unit annotations auto-seed from the
        platform defaults.
      </p>
      {Object.entries(properties).map(([key, prop]) => {
        // Skip nested objects — we auto-populate from example defaults.
        if (prop.type === 'object' || prop.$ref) return null;
        const type = inputTypeFor(prop);
        const isEnum = Array.isArray(prop.enum);
        const fieldError = fieldErrors[key];
        const errorId = `catalog-field-${entry.code}-${key}-error`;
        return (
          <label key={key} className="block text-sm">
            {key}
            {required.has(key) && <span className="ml-1 text-destructive">*</span>}
            {isEnum ? (
              <select
                data-testid={`catalog-field-${key}`}
                className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
                value={values[key] ?? ''}
                onChange={(e) => set(key, e.target.value)}
                required={required.has(key)}
                aria-invalid={Boolean(fieldError)}
                aria-describedby={fieldError ? errorId : undefined}
              >
                <option value="" />
                {(prop.enum ?? []).map((opt) => (
                  <option key={opt} value={opt}>
                    {opt}
                  </option>
                ))}
              </select>
            ) : (
              <input
                data-testid={`catalog-field-${key}`}
                type={type}
                className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
                value={values[key] ?? ''}
                onChange={(e) => set(key, e.target.value)}
                required={required.has(key)}
                step={prop.type === 'number' ? '0.001' : undefined}
                min={prop.minimum}
                max={prop.maximum}
                aria-invalid={Boolean(fieldError)}
                aria-describedby={fieldError ? errorId : undefined}
              />
            )}
            {fieldError && (
              <span id={errorId} role="alert" className="mt-1 block text-xs text-destructive">
                {fieldError}
              </span>
            )}
            {prop.description && (
              <span className="mt-0.5 block text-xs text-muted-foreground">{prop.description}</span>
            )}
          </label>
        );
      })}
      {error && (
        <p
          className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive"
          data-testid={`catalog-error-${entry.code}`}
        >
          {error}
        </p>
      )}
      <div className="flex justify-end gap-2">
        <button
          type="button"
          onClick={onCancel}
          className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-secondary"
        >
          Cancel
        </button>
        <button
          type="submit"
          disabled={busy}
          data-testid={`catalog-submit-${entry.code}`}
          className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-60"
        >
          {busy ? 'Saving…' : `Record ${entry.display_name.toLowerCase()}`}
        </button>
      </div>
    </form>
  );
}

/* ================================================================= */
/* Hook: fetch event catalog                                          */
/* ================================================================= */

export function useEventCatalog() {
  const [catalog, setCatalog] = useState<EventCatalogEntry[] | null>(null);
  useEffect(() => {
    (async () => {
      try {
        const res = await apiFetch<{ entries: EventCatalogEntry[] }>(
          '/v1/production-events/catalog',
        );
        setCatalog(res.entries);
      } catch {
        setCatalog([]);
      }
    })();
  }, []);
  return catalog;
}
