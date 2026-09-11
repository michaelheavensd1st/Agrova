'use client';

/**
 * Sprint 5.1 — Inventory Dashboard.
 *
 * Read-only operational overview of inventory for the currently
 * selected organization. Uses ONLY existing Sprint 4 inventory
 * endpoints:
 *
 *   GET /v1/organizations                             — pick org context
 *   GET /v1/organizations/{org}/warehouses            — warehouses
 *   GET /v1/organizations/{org}/inventory-items       — item catalog
 *   GET /v1/warehouses/{wh}/lots                      — lots + live balance
 *
 * Tenant isolation is enforced by the backend on every endpoint.
 * Farm-scoped warehouses are filtered server-side according to the
 * caller's role assignments — this page trusts that filter and does
 * NOT re-implement tenancy client-side.
 *
 * Review-round fixes (Sprint 5.1):
 *  1. Selected organization is propagated to every workspace link
 *     via `?organization_id=…` so navigation preserves tenant context.
 *  2. A monotonically-increasing request-generation ref guards the
 *     dashboard against stale organization responses — only the
 *     latest active fetch may write projection / warehouses / items /
 *     lots / error / forbidden state.
 *  3. 401 / 403 in the warehouse lot fan-out are propagated to
 *     auth handling (redirect / ForbiddenBanner). Only ordinary
 *     failures produce the "partial totals" warning.
 *  4. 403 during the organization bootstrap renders ForbiddenBanner.
 *  5. The old "Recent lot activity" list (ranked by `lot.updated_at`)
 *     is gone — receipts / issues / transfers / adjustments do NOT
 *     update the parent lot's timestamp, so that ordering was
 *     misleading. It is replaced with an explicit deferred panel.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import Link from 'next/link';
import { apiFetch, ApiError } from '@/lib/api';
import { ErrorBanner, ForbiddenBanner, Loading } from '@/components/ape-ui';
import { EmptyStateCard, friendlyError } from '@/components/ui-polish';
import {
  buildDashboardProjection,
  isQuarantinedReceiptFixtureWarehouse,
  resolveOrganizationId,
  type DashboardInventoryItem,
  type DashboardLot,
  type DashboardOrganization,
  type DashboardProjection,
  type DashboardWarehouse,
} from '@/lib/inventory-dashboard';
import { InventoryDashboardSummaryCards } from '@/components/inventory-dashboard/summary-cards';
import { InventoryDashboardAttentionPanel } from '@/components/inventory-dashboard/attention-panel';
import { InventoryDashboardActivityPlaceholder } from '@/components/inventory-dashboard/activity-placeholder';
import {
  InventoryDashboardQuickActions,
  buildWorkspaceHref,
} from '@/components/inventory-dashboard/quick-actions';

interface DashboardState {
  loading: boolean;
  forbidden: boolean;
  error: string | null;
  projection: DashboardProjection | null;
  warehouses: DashboardWarehouse[];
  items: DashboardInventoryItem[];
  lots: DashboardLot[];
  nowIso: string;
}

const INITIAL_STATE: DashboardState = {
  loading: true,
  forbidden: false,
  error: null,
  projection: null,
  warehouses: [],
  items: [],
  lots: [],
  nowIso: new Date().toISOString(),
};

/**
 * Fan-out warehouse-lots outcome inspector.
 *
 * Distinguishes:
 *   - "unauthenticated"  → surface 401 to the caller for /login redirect
 *   - "forbidden"        → surface 403 for ForbiddenBanner
 *   - "partial"          → at least one non-auth failure (500 / network)
 *   - "ok"               → every fan-out request succeeded
 */
type LotFanOutOutcome =
  | { kind: 'ok'; lots: DashboardLot[] }
  | { kind: 'partial'; lots: DashboardLot[] }
  | { kind: 'unauthenticated' }
  | { kind: 'forbidden' };

function inspectLotFanOut(results: PromiseSettledResult<DashboardLot[]>[]): LotFanOutOutcome {
  // Auth failures take absolute precedence — never quietly downgrade.
  for (const r of results) {
    if (r.status === 'rejected' && r.reason instanceof ApiError) {
      if (r.reason.status === 401) return { kind: 'unauthenticated' };
      if (r.reason.status === 403) return { kind: 'forbidden' };
    }
  }
  const lots: DashboardLot[] = [];
  let hadFailure = false;
  for (const r of results) {
    if (r.status === 'fulfilled') {
      lots.push(...r.value);
    } else {
      hadFailure = true;
    }
  }
  return { kind: hadFailure ? 'partial' : 'ok', lots };
}

/** Read a query parameter without pulling in useSearchParams (which
 * would force this page into a Suspense boundary and duplicate the
 * hydration bootstrap tests). */
function readInitialOrganizationId(): string | null {
  if (typeof window === 'undefined') return null;
  try {
    return new URLSearchParams(window.location.search).get('organization_id');
  } catch {
    return null;
  }
}

export default function InventoryDashboardPage() {
  const router = useRouter();
  const [orgs, setOrgs] = useState<DashboardOrganization[] | null>(null);
  const [orgId, setOrgId] = useState<string>('');
  const [state, setState] = useState<DashboardState>(INITIAL_STATE);

  // Sprint 5.1 review fix #2 — stale-response guard. Every time we
  // begin a new dashboard load we bump the generation; only writes
  // that carry the current generation may mutate `state`.
  const activeGenerationRef = useRef(0);

  // Sprint 5.1 review fix #1 — respect ?organization_id= in the URL
  // when the user lands directly on the dashboard, but ONLY after
  // validating it against the authenticated user's org list.
  const requestedOrgIdRef = useRef<string | null>(null);
  if (requestedOrgIdRef.current === null && typeof window !== 'undefined') {
    requestedOrgIdRef.current = readInitialOrganizationId();
  }

  // Bootstrap: load organizations.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const list = await apiFetch<DashboardOrganization[]>('/v1/organizations');
        if (cancelled) return;
        setOrgs(list);
        if (list.length === 0) {
          router.push('/onboarding');
          return;
        }
        // Validate ?organization_id=… against the caller's real orgs.
        const requested = requestedOrgIdRef.current;
        const validated = resolveOrganizationId(requested, list) ?? list[0].id;
        setOrgId(validated);
      } catch (err) {
        if (cancelled) return;
        // Sprint 5.1 review fix #4 — 401 and 403 both need dedicated
        // paths at bootstrap. Everything else falls through to the
        // regular error banner.
        if (err instanceof ApiError) {
          if (err.status === 401) {
            router.push('/login');
            return;
          }
          if (err.status === 403) {
            setState({ ...INITIAL_STATE, loading: false, forbidden: true });
            return;
          }
        }
        setState({ ...INITIAL_STATE, loading: false, error: friendlyError(err) });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [router]);

  const loadDashboard = useCallback(
    async (organizationId: string) => {
      const generation = ++activeGenerationRef.current;
      const isStale = () => activeGenerationRef.current !== generation;

      // Clear organization-specific data so we never render stale rows
      // under a new organization heading while the fresh fetch is in
      // flight.
      setState({
        loading: true,
        forbidden: false,
        error: null,
        projection: null,
        warehouses: [],
        items: [],
        lots: [],
        nowIso: new Date().toISOString(),
      });

      try {
        const [warehouses, items] = await Promise.all([
          apiFetch<DashboardWarehouse[]>(`/v1/organizations/${organizationId}/warehouses`),
          apiFetch<DashboardInventoryItem[]>(`/v1/organizations/${organizationId}/inventory-items`),
        ]);
        if (isStale()) return;

        // Reporting intentionally retains closed warehouses and inactive item
        // metadata. Only the documented Release 6.0.6 receipt fixture is
        // excluded from this operational projection and lot fan-out.
        const reportingWarehouses = warehouses.filter(
          (warehouse) => !isQuarantinedReceiptFixtureWarehouse(warehouse),
        );

        const lotResults = await Promise.allSettled(
          reportingWarehouses.map((wh) => apiFetch<DashboardLot[]>(`/v1/warehouses/${wh.id}/lots`)),
        );
        if (isStale()) return;

        const outcome = inspectLotFanOut(lotResults);
        if (outcome.kind === 'unauthenticated') {
          router.push('/login');
          return;
        }
        if (outcome.kind === 'forbidden') {
          setState({ ...INITIAL_STATE, loading: false, forbidden: true });
          return;
        }

        const nowIso = new Date().toISOString();
        const projection = buildDashboardProjection({
          warehouses: reportingWarehouses,
          items,
          lots: outcome.lots,
          nowIso,
        });

        setState({
          loading: false,
          forbidden: false,
          error:
            outcome.kind === 'partial'
              ? 'One or more warehouses could not be loaded. Some totals may be understated.'
              : null,
          projection,
          warehouses: reportingWarehouses,
          items,
          lots: outcome.lots,
          nowIso,
        });
      } catch (err) {
        if (isStale()) return;
        if (err instanceof ApiError) {
          if (err.status === 401) {
            router.push('/login');
            return;
          }
          if (err.status === 403) {
            setState({ ...INITIAL_STATE, loading: false, forbidden: true });
            return;
          }
        }
        setState({
          ...INITIAL_STATE,
          loading: false,
          error: friendlyError(err),
        });
      }
    },
    [router],
  );

  useEffect(() => {
    if (!orgId) return;
    void loadDashboard(orgId);
  }, [orgId, loadDashboard]);

  const activeOrg = useMemo(() => orgs?.find((o) => o.id === orgId) ?? null, [orgs, orgId]);

  const workspaceHref = buildWorkspaceHref(orgId || null, null);
  const workspaceWarehousesHref = buildWorkspaceHref(orgId || null, 'warehouses');

  return (
    <main className="mx-auto max-w-6xl px-6 py-10" data-testid="inventory-dashboard-page">
      <header className="mb-6 flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-xs uppercase tracking-widest text-muted-foreground">
            Sprint 5.1 · Inventory
          </p>
          <h1 className="font-display text-3xl">Inventory dashboard</h1>
          {activeOrg && (
            <p
              className="mt-1 text-sm text-muted-foreground"
              data-testid="inventory-dashboard-org-name"
            >
              {activeOrg.name}
            </p>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-3">
          {orgs && orgs.length > 1 && (
            <div className="flex items-center gap-2 text-sm">
              <label className="text-muted-foreground" htmlFor="inventory-dashboard-org-selector">
                Organization
              </label>
              <select
                id="inventory-dashboard-org-selector"
                data-testid="inventory-dashboard-org-selector"
                className="rounded-md border border-border bg-background px-2 py-1"
                value={orgId}
                onChange={(e) => setOrgId(e.target.value)}
              >
                {orgs.map((o) => (
                  <option key={o.id} value={o.id}>
                    {o.name}
                  </option>
                ))}
              </select>
            </div>
          )}
          <Link
            href={workspaceHref}
            data-testid="inventory-dashboard-workspace-link"
            className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-secondary"
          >
            Open workspace
          </Link>
        </div>
      </header>

      {state.forbidden ? (
        <ForbiddenBanner />
      ) : state.loading ? (
        <Loading label="Loading inventory dashboard…" />
      ) : (
        <div className="space-y-6">
          {state.error && <ErrorBanner message={state.error} />}
          {state.projection && (
            <>
              <InventoryDashboardSummaryCards summary={state.projection.summary} />
              {state.warehouses.length === 0 && state.items.length === 0 ? (
                <EmptyStateCard
                  testId="inventory-dashboard-empty"
                  title="No inventory yet"
                  description="Add a warehouse and a few items to start tracking stock."
                  action={
                    <Link
                      href={workspaceWarehousesHref}
                      data-testid="inventory-dashboard-empty-cta"
                      className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground"
                    >
                      Create your first warehouse
                    </Link>
                  }
                />
              ) : (
                <div className="grid gap-6 lg:grid-cols-2">
                  <InventoryDashboardAttentionPanel rows={state.projection.attention} />
                  <InventoryDashboardActivityPlaceholder organizationId={orgId || null} />
                </div>
              )}
              <InventoryDashboardQuickActions organizationId={orgId || null} />
            </>
          )}
        </div>
      )}
    </main>
  );
}
