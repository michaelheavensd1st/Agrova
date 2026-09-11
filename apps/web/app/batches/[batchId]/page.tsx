'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import { useParams, useRouter } from 'next/navigation';
import { ApiError, apiFetch } from '@/lib/api';
import type {
  BatchProjections,
  EventCatalogEntry,
  Farm,
  ProductionBatch,
  ProductionEvent,
  ProductionEventPage,
  ProductionSite,
  ProductionUnit,
  ProductionUnitType,
} from '@/lib/types';
import {
  Breadcrumbs,
  EmptyState,
  ErrorBanner,
  ForbiddenBanner,
  Loading,
  StateBadge,
} from '@/components/ape-ui';
import {
  CatalogEventForm,
  FeedingForm,
  MortalityForm,
  StockingForm,
  TransferEventForm,
  useEventCatalog,
} from '@/components/event-forms';

const CATALOG_DRIVEN = new Set(['SAMPLING', 'WATER_QUALITY', 'TRANSFER', 'HARVEST']);
const DELIBERATE = new Set(['STOCKING', 'FEEDING', 'MORTALITY']);

type PickerMode =
  | { kind: 'idle' }
  | { kind: 'deliberate'; type: 'STOCKING' | 'FEEDING' | 'MORTALITY' }
  | { kind: 'catalog'; entry: EventCatalogEntry };

function fmtNumber(n: number | null | undefined, digits = 0): string {
  if (n == null) return '—';
  return n.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function payloadSummary(evt: ProductionEvent): string {
  const d = evt.data;
  switch (evt.event_type) {
    case 'STOCKING':
      return `${fmtNumber(Number(d.quantity))} × ${d.species_code ?? '—'} @ ${d.average_weight ?? '?'}${d.weight_unit ?? ''}`;
    case 'FEEDING':
      return `${d.quantity}${d.unit ?? ''} — ${d.feed_description ?? d.feed_item_ref ?? '—'}`;
    case 'MORTALITY':
      return `−${fmtNumber(Number(d.count))} · ${d.suspected_cause ?? '—'}`;
    case 'SAMPLING':
      return `${d.sample_size} sample · avg ${d.average_weight}${d.weight_unit ?? ''}${d.estimated_population ? ` · est pop ${d.estimated_population}` : ''}`;
    case 'WATER_QUALITY': {
      const parts = [];
      if (d.temperature != null) parts.push(`${d.temperature}°C`);
      if (d.dissolved_oxygen != null) parts.push(`DO ${d.dissolved_oxygen}mg/l`);
      if (d.ph != null) parts.push(`pH ${d.ph}`);
      return parts.join(' · ') || 'water quality';
    }
    case 'TRANSFER':
      return evt.transfer_role === 'in'
        ? `← ${d.quantity} ind. received`
        : `→ ${d.quantity} ind. sent`;
    case 'HARVEST':
      return `${fmtNumber(Number(d.quantity))} ind · ${d.total_weight}${d.weight_unit ?? 'kg'}${d.is_final ? ' · FINAL' : ''}`;
    default:
      return evt.event_type;
  }
}

export default function BatchDetailPage() {
  const router = useRouter();
  const params = useParams<{ batchId: string }>();
  const batchId = params.batchId;
  const [batch, setBatch] = useState<ProductionBatch | null>(null);
  const [unit, setUnit] = useState<ProductionUnit | null>(null);
  const [site, setSite] = useState<ProductionSite | null>(null);
  const [farm, setFarm] = useState<Farm | null>(null);
  const [unitType, setUnitType] = useState<ProductionUnitType | null>(null);
  const [events, setEvents] = useState<ProductionEvent[] | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [projections, setProjections] = useState<BatchProjections | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [forbidden, setForbidden] = useState(false);
  const [picker, setPicker] = useState<PickerMode>({ kind: 'idle' });
  const catalog = useEventCatalog();

  const reloadEvents = useCallback(async () => {
    const page = await apiFetch<ProductionEventPage>(`/v1/batches/${batchId}/events?limit=50`);
    setEvents(page.items);
    setCursor(page.next_cursor);
  }, [batchId]);

  const reloadProjections = useCallback(async () => {
    const proj = await apiFetch<BatchProjections>(`/v1/batches/${batchId}/projections`);
    setProjections(proj);
  }, [batchId]);

  const reloadBatch = useCallback(async () => {
    const b = await apiFetch<ProductionBatch>(`/v1/batches/${batchId}`);
    setBatch(b);
    const u = await apiFetch<ProductionUnit>(`/v1/units/${b.unit_id}`);
    setUnit(u);
    const s = await apiFetch<ProductionSite>(`/v1/sites/${u.site_id}`);
    setSite(s);
    const f = await apiFetch<Farm>(`/v1/farms/${s.farm_id}`);
    setFarm(f);
    const allTypes = await apiFetch<ProductionUnitType[]>('/v1/production-unit-types');
    setUnitType(allTypes.find((t) => t.id === u.unit_type_id) ?? null);
  }, [batchId]);

  useEffect(() => {
    (async () => {
      try {
        await Promise.all([reloadBatch(), reloadEvents(), reloadProjections()]);
      } catch (err) {
        if (err instanceof ApiError && err.status === 403) setForbidden(true);
        else
          setError(
            err instanceof ApiError
              ? ((err.payload.detail as string) ?? 'Failed to load.')
              : String(err),
          );
      }
    })();
  }, [reloadBatch, reloadEvents, reloadProjections]);

  const catalogByCode = useMemo(() => {
    const m: Record<string, EventCatalogEntry> = {};
    (catalog ?? []).forEach((c) => {
      m[c.code] = c;
    });
    return m;
  }, [catalog]);

  async function loadMore() {
    if (!cursor) return;
    const page = await apiFetch<ProductionEventPage>(
      `/v1/batches/${batchId}/events?limit=50&cursor=${encodeURIComponent(cursor)}`,
    );
    setEvents((prev) => [...(prev ?? []), ...page.items]);
    setCursor(page.next_cursor);
  }

  async function onEventCreated() {
    setPicker({ kind: 'idle' });
    await Promise.all([reloadBatch(), reloadEvents(), reloadProjections()]);
  }

  if (forbidden)
    return (
      <main className="mx-auto max-w-6xl px-6 py-10">
        <ForbiddenBanner />
      </main>
    );

  const displayUnitLabel = unitType?.display_name ?? 'Unit';

  return (
    <main className="mx-auto max-w-6xl px-6 py-10" data-testid="batch-detail-page">
      <Breadcrumbs
        items={[
          { label: 'Dashboard', href: '/dashboard' },
          { label: unit ? displayUnitLabel : '…', href: unit ? `/units/${unit.id}` : undefined },
          { label: batch ? batch.code : 'Batch' },
        ]}
      />

      <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
        <div>
          <p className="text-xs uppercase tracking-widest text-muted-foreground">Batch</p>
          <h1 className="flex items-center gap-3 font-display text-3xl">
            {batch?.code ?? '…'}
            {batch && <StateBadge state={batch.state} />}
          </h1>
          {batch && (
            <p className="mt-0.5 text-sm text-muted-foreground">
              {unit && `${displayUnitLabel} · ${unit.name}`} {batch.species && `· ${batch.species}`}
            </p>
          )}
        </div>
      </div>

      {error && (
        <div className="mt-4">
          <ErrorBanner message={error} />
        </div>
      )}

      {/* Projections */}
      <section
        className="mt-8 grid gap-3 sm:grid-cols-2 lg:grid-cols-4"
        data-testid="projections-panel"
      >
        <Metric
          label="Stocked"
          value={projections ? fmtNumber(projections.initial_stocked_quantity) : '—'}
          testid="metric-stocked"
        />
        <Metric
          label="Estimated remaining"
          value={projections ? fmtNumber(projections.estimated_remaining_population) : '—'}
          testid="metric-remaining"
        />
        <Metric
          label="Mortality"
          value={projections ? fmtNumber(projections.cumulative_mortality) : '—'}
          testid="metric-mortality"
        />
        <Metric
          label="Survival rate"
          value={
            projections?.survival_rate != null
              ? `${(projections.survival_rate * 100).toFixed(1)}%`
              : '—'
          }
          testid="metric-survival"
        />
        <Metric
          label="Latest avg weight"
          value={
            projections?.latest_average_weight != null
              ? `${projections.latest_average_weight}${projections.weight_unit ?? ''}`
              : '—'
          }
          testid="metric-avg-weight"
        />
        <Metric
          label="Estimated biomass"
          value={
            projections?.estimated_biomass_kg != null
              ? `${fmtNumber(projections.estimated_biomass_kg, 2)} kg`
              : '—'
          }
          testid="metric-biomass"
        />
        <Metric
          label="Total feed"
          value={projections ? `${fmtNumber(projections.total_feed_kg, 2)} kg` : '—'}
          testid="metric-feed"
        />
        <Metric
          label="Batch age"
          value={projections?.batch_age_days != null ? `${projections.batch_age_days} days` : '—'}
          testid="metric-age"
        />
      </section>

      {/* Event actions */}
      <section className="mt-8">
        <h2 className="text-lg font-semibold">Record event</h2>
        <div className="mt-3 flex flex-wrap gap-2">
          <button
            type="button"
            data-testid="record-STOCKING"
            className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-secondary"
            onClick={() => setPicker({ kind: 'deliberate', type: 'STOCKING' })}
          >
            Stocking
          </button>
          <button
            type="button"
            data-testid="record-FEEDING"
            className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-secondary"
            onClick={() => setPicker({ kind: 'deliberate', type: 'FEEDING' })}
          >
            Feeding
          </button>
          <button
            type="button"
            data-testid="record-MORTALITY"
            className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-secondary"
            onClick={() => setPicker({ kind: 'deliberate', type: 'MORTALITY' })}
          >
            Mortality
          </button>
          {[...CATALOG_DRIVEN].map((code) => {
            const entry = catalogByCode[code];
            if (!entry) return null;
            return (
              <button
                key={code}
                type="button"
                data-testid={`record-${code}`}
                className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-secondary"
                onClick={() => setPicker({ kind: 'catalog', entry })}
              >
                {entry.display_name}
              </button>
            );
          })}
        </div>
        {picker.kind !== 'idle' && (
          <div
            className="mt-4 rounded-2xl border border-border bg-card/60 p-5"
            data-testid="event-form-panel"
          >
            {picker.kind === 'deliberate' && picker.type === 'STOCKING' && (
              <StockingForm
                batchId={batchId}
                onCreated={onEventCreated}
                onCancel={() => setPicker({ kind: 'idle' })}
                onUnauthenticated={() => router.push('/login')}
              />
            )}
            {picker.kind === 'deliberate' && picker.type === 'FEEDING' && farm && site && (
              <FeedingForm
                batchId={batchId}
                organizationId={farm.organization_id}
                farmId={site.farm_id}
                onCreated={onEventCreated}
                onCancel={() => setPicker({ kind: 'idle' })}
                onUnauthenticated={() => router.push('/login')}
              />
            )}
            {picker.kind === 'deliberate' && picker.type === 'MORTALITY' && (
              <MortalityForm
                batchId={batchId}
                onCreated={onEventCreated}
                onCancel={() => setPicker({ kind: 'idle' })}
                onUnauthenticated={() => router.push('/login')}
              />
            )}
            {picker.kind === 'catalog' && picker.entry.code === 'TRANSFER' && unit && farm && (
              <TransferEventForm
                batchId={batchId}
                entry={picker.entry}
                sourceUnit={unit}
                onCreated={onEventCreated}
                onCancel={() => setPicker({ kind: 'idle' })}
                onUnauthenticated={() => router.push('/login')}
              />
            )}
            {picker.kind === 'catalog' && picker.entry.code !== 'TRANSFER' && (
              <CatalogEventForm
                batchId={batchId}
                entry={picker.entry}
                onCreated={onEventCreated}
                onCancel={() => setPicker({ kind: 'idle' })}
                onUnauthenticated={() => router.push('/login')}
              />
            )}
          </div>
        )}
      </section>

      {/* Timeline */}
      <section className="mt-10">
        <h2 className="text-lg font-semibold">Event timeline</h2>
        {events === null ? (
          <div className="mt-4">
            <Loading label="Loading events…" />
          </div>
        ) : events.length === 0 ? (
          <div className="mt-4">
            <EmptyState
              title="No events yet"
              description="Record the first stocking event to begin the batch lifecycle."
            />
          </div>
        ) : (
          <ol className="mt-4 space-y-2" data-testid="event-timeline">
            {events.map((e) => {
              const catalogEntry = catalogByCode[e.event_type];
              const label = catalogEntry?.display_name ?? e.event_type;
              return (
                <li
                  key={e.id}
                  data-testid={`event-row-${e.id}`}
                  className="rounded-md border border-border bg-card/50 p-3"
                >
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="flex items-center gap-2">
                      <span
                        className={`inline-flex rounded px-2 py-0.5 text-xs font-medium ${
                          DELIBERATE.has(e.event_type)
                            ? 'bg-primary/10 text-primary'
                            : 'bg-secondary text-foreground/80'
                        }`}
                      >
                        {label}
                      </span>
                      <span className="text-sm">{payloadSummary(e)}</span>
                    </div>
                    <div className="text-xs text-muted-foreground">
                      {new Date(e.performed_at).toLocaleString()}
                    </div>
                  </div>
                  {catalogEntry?.triggers_transition_to && (
                    <p className="mt-1 text-[11px] text-muted-foreground">
                      Triggers transition → {catalogEntry.triggers_transition_to}
                    </p>
                  )}
                </li>
              );
            })}
          </ol>
        )}
        {cursor && (
          <button
            type="button"
            onClick={loadMore}
            data-testid="load-more-events"
            className="mt-3 rounded-md border border-border px-3 py-1.5 text-xs hover:bg-secondary"
          >
            Load more
          </button>
        )}
      </section>
    </main>
  );
}

function Metric({ label, value, testid }: { label: string; value: string; testid: string }) {
  return (
    <div className="rounded-2xl border border-border bg-card/60 p-4" data-testid={testid}>
      <p className="text-[11px] uppercase tracking-widest text-muted-foreground">{label}</p>
      <p className="mt-1 text-lg font-semibold">{value}</p>
    </div>
  );
}
