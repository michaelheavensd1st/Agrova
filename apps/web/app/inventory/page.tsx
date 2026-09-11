'use client';

/**
 * Sprint 4 — Inventory workspace.
 *
 * One deliberate operator screen covering every workflow the spec
 * calls for: warehouses, items, lots + balances, receive / issue /
 * transfer / adjust / reverse, and per-lot transaction history.
 *
 * Sprint 4 UX polish (2026-02-08):
 *  · Toast notifications for success + failure.
 *  · Loading skeletons while lists are fetching.
 *  · Search + filter inputs on Warehouses / Items / Lots / History.
 *  · Confirmation dialogs before destructive posts (Adjust, Reverse).
 *  · Empty-state cards with clear CTAs on first-run screens.
 *  · Submit buttons disable during in-flight requests.
 *  · Friendly language for 409 / idempotency conflicts.
 */

import { FormEvent, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams, useRouter } from 'next/navigation';
import { apiFetch, ApiError } from '@/lib/api';
import {
  isItemInCurrentOrg,
  isLotInCurrentOrg,
  isWarehouseInCurrentOrg,
  resolveOrganizationId,
} from '@/lib/inventory-dashboard';
import { isWarehouseEligibleForStockOperation } from '@/lib/stock-operations';
import {
  ConfirmDialog,
  EmptyStateCard,
  Skeleton,
  SkeletonRows,
  friendlyError,
  toast,
} from '@/components/ui-polish';

// --- Types ---------------------------------------------------------- //
interface Organization {
  id: string;
  name: string;
  slug: string;
}
interface Warehouse {
  id: string;
  code: string;
  name: string;
  status: 'active' | 'closed' | 'maintenance';
  farm_id: string | null;
}
interface InventoryItem {
  id: string;
  code: string;
  name: string;
  category: 'feed' | 'medicine' | 'chemical' | 'supply';
  canonical_unit: string;
}
interface Lot {
  id: string;
  item_id: string;
  warehouse_id: string;
  lot_code: string;
  expiry_date: string | null;
  balance: string;
  balance_unit: string;
}
interface LedgerTx {
  id: string;
  transaction_type: string;
  quantity: string;
  unit: string;
  performed_at: string;
  reason: string | null;
  reference_type: string | null;
}

type Tab =
  | 'overview'
  | 'warehouses'
  | 'items'
  | 'lots'
  | 'receive'
  | 'issue'
  | 'transfer'
  | 'adjust'
  | 'history';

function idem(prefix: string) {
  return `${prefix}-${Date.now()}-${crypto.randomUUID()}`;
}

async function postWithKey<T>(path: string, body: unknown, key: string): Promise<T> {
  return apiFetch<T>(path, {
    method: 'POST',
    headers: { 'Idempotency-Key': key },
    body: JSON.stringify(body),
  });
}

const UNITS = ['kg', 'g', 'L', 'mL', 'count', 'bag', 'pack'] as const;
const CATEGORIES = ['feed', 'medicine', 'chemical', 'supply'] as const;

// -------------------------------------------------------------------- //
export default function InventoryPage() {
  return (
    <Suspense
      fallback={
        <main className="mx-auto max-w-6xl px-6 py-10" data-testid="inventory-page-loading">
          <Skeleton className="mb-6 h-10 w-64" />
          <SkeletonRows rows={5} />
        </main>
      }
    >
      <InventoryInner />
    </Suspense>
  );
}

function InventoryInner() {
  const router = useRouter();
  const params = useSearchParams();
  const [tab, setTab] = useState<Tab>((params.get('tab') as Tab) ?? 'overview');
  const [orgs, setOrgs] = useState<Organization[]>([]);
  const [orgId, setOrgId] = useState<string>('');
  const [warehouses, setWarehouses] = useState<Warehouse[]>([]);
  const [items, setItems] = useState<InventoryItem[]>([]);
  const [operationalWarehouses, setOperationalWarehouses] = useState<Warehouse[]>([]);
  const [operationalItems, setOperationalItems] = useState<InventoryItem[]>([]);
  const [selectedWh, setSelectedWh] = useState<string>('');
  const [lots, setLots] = useState<Lot[]>([]);
  const [history, setHistory] = useState<LedgerTx[]>([]);
  const [selectedLot, setSelectedLot] = useState<string>('');
  const [loadingOrg, setLoadingOrg] = useState(true);
  const [loadingLots, setLoadingLots] = useState(false);
  const [loadingHistory, setLoadingHistory] = useState(false);

  // Sprint 5.1 review round #4 — centralized 401 / 403 handling.
  //
  // Every workspace loader (reloadOrg / reloadLots / history) used
  // to convert authorization errors into ordinary toasts, which is
  // inconsistent with the organization bootstrap (which redirects
  // on 401) and hides missing-permission failures behind a generic
  // error banner.
  //
  // We now route auth errors through `handleLoadError`:
  //   - 401 → unconditional redirect to /login (session-scoped;
  //           we do NOT write any state before navigating).
  //   - 403 → clear the affected slice of state and surface a
  //           dedicated permission banner. State writes are gated
  //           on `isCurrent()` so a late org-A 403 cannot damage
  //           org-B once the user has switched.
  //   - anything else → falls through to the existing friendly
  //           toast (also gated on `isCurrent()`).
  //
  // `forbidden.scope` narrows the visible affected region:
  //   'org'     → the whole workspace body is replaced by a banner.
  //   'lot'     → the Lots tab body and any lot-dependent history
  //               view show a scoped banner (warehouses + items
  //               stay usable).
  //   'history' → only the History tab shows a scoped banner.
  const [forbidden, setForbidden] = useState<{
    scope: 'org' | 'lot' | 'history';
    message: string;
  } | null>(null);

  // Sprint 5.1 review round #3 — async organization-race guard.
  //
  // Every organization-scoped fetch (reloadOrg / reloadLots / lot
  // history) captures the active orgId + selected warehouse + lot at
  // request start, along with a monotonically increasing generation
  // number. Before writing state we verify the captured context is
  // still current. An obsolete response — for example a slow
  // organization-A warehouses fetch that resolves AFTER the user has
  // switched to organization B — is dropped on the floor and can
  // never overwrite the current view.
  //
  // The three refs are decoupled deliberately so a stale lot fetch
  // does not invalidate an in-flight org fetch (and vice-versa),
  // even though every generation bumps when orgId changes.
  const orgGenerationRef = useRef(0);
  const lotGenerationRef = useRef(0);
  const historyGenerationRef = useRef(0);

  /**
   * Sprint 5.1 review round #4 — central workspace load-error handler.
   *
   * Returns 'auth' if the error was authorization-related and the
   * caller should stop (no toast, no other state writes). Returns
   * 'unhandled' otherwise so the caller can apply its own
   * (generation-guarded) fallback behavior.
   *
   * Contract:
   *   - 401 → unconditional `router.push('/login')`. We do NOT touch
   *     any state before navigating, because the session itself is
   *     invalid and any state we set would either be immediately
   *     replaced by /login or leak stale context onto the login page.
   *   - 403 → only mutates state when `isCurrent()` still holds, so
   *     a late org-A 403 that arrives after the user has switched
   *     to org-B cannot damage org-B's data or error state.
   */
  const handleLoadError = useCallback(
    (
      error: unknown,
      scope: 'org' | 'lot' | 'history',
      isCurrent: () => boolean,
    ): 'auth' | 'unhandled' => {
      if (!(error instanceof ApiError)) return 'unhandled';
      if (error.status === 401) {
        router.push('/login');
        return 'auth';
      }
      if (error.status === 403) {
        if (!isCurrent()) return 'auth';
        if (scope === 'org') {
          // Whole-org access lost: invalidate every downstream
          // generation, drop every organization-scoped slice, and
          // show a page-level banner in place of the tabs body.
          orgGenerationRef.current += 1;
          lotGenerationRef.current += 1;
          historyGenerationRef.current += 1;
          setWarehouses([]);
          setItems([]);
          setOperationalWarehouses([]);
          setOperationalItems([]);
          setLots([]);
          setHistory([]);
          setSelectedWh('');
          setSelectedLot('');
          setForbidden({
            scope: 'org',
            message: "You don't have permission to view this organization's inventory.",
          });
        } else if (scope === 'lot') {
          // Warehouse-scoped access lost: keep the org's warehouse
          // catalog + item catalog usable, but drop lots + history.
          lotGenerationRef.current += 1;
          historyGenerationRef.current += 1;
          setLots([]);
          setSelectedLot('');
          setHistory([]);
          setForbidden({
            scope: 'lot',
            message: "You don't have permission to view lots in this warehouse.",
          });
        } else {
          // Lot-history scoped access lost: keep everything else.
          historyGenerationRef.current += 1;
          setHistory([]);
          setForbidden({
            scope: 'history',
            message: "You don't have permission to view this lot's transaction history.",
          });
        }
        return 'auth';
      }
      return 'unhandled';
    },
    [router],
  );

  // Bootstrap: load orgs.
  useEffect(() => {
    (async () => {
      try {
        const list = await apiFetch<Organization[]>('/v1/organizations');
        setOrgs(list);
        if (list.length === 0) {
          router.push('/onboarding');
          return;
        }
        // Sprint 5.1 review fix — honour `?organization_id=…` when it
        // matches one of the caller's real orgs. Fall back safely
        // otherwise; never trust an unvalidated query parameter.
        const requested = params.get('organization_id');
        const validated = resolveOrganizationId(requested, list) ?? list[0].id;
        setOrgId(validated);
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) router.push('/login');
        else toast(friendlyError(e), 'error');
      } finally {
        setLoadingOrg(false);
      }
    })();
  }, [router, params]);

  const reloadOrg = useCallback(async () => {
    if (!orgId) return;
    const capturedOrgId = orgId;
    const generation = ++orgGenerationRef.current;
    // Every subsequent state mutation must clear this predicate — if
    // the user switches org (or triggers another reload) mid-flight
    // the generation ref bumps and this reload becomes a no-op writer.
    const isCurrent = () => orgGenerationRef.current === generation && capturedOrgId === orgId;
    setLoadingOrg(true);
    try {
      const [wh, it, operationalWh, operationalIt] = await Promise.all([
        apiFetch<Warehouse[]>(`/v1/organizations/${capturedOrgId}/warehouses`),
        apiFetch<InventoryItem[]>(`/v1/organizations/${capturedOrgId}/inventory-items`),
        apiFetch<Warehouse[]>(
          `/v1/organizations/${capturedOrgId}/warehouses?operational_only=true`,
        ),
        apiFetch<InventoryItem[]>(
          `/v1/organizations/${capturedOrgId}/inventory-items?operational_only=true`,
        ),
      ]);
      if (!isCurrent()) return;
      setWarehouses(wh);
      setItems(it);
      setOperationalWarehouses(operationalWh);
      setOperationalItems(operationalIt);
      // A successful org reload clears any stale org-scope 403 banner.
      setForbidden((f) => (f?.scope === 'org' ? null : f));
      // Sprint 5.1 review round #2 — after an organization switch we
      // want the workspace to auto-select a fresh warehouse from the
      // new org. The prior `!selectedWh` guard only auto-selected on
      // the initial load; the org-reset effect below clears
      // selectedWh right before reloadOrg runs, so this branch now
      // covers both first-load AND org-switch.
      setSelectedWh((current) =>
        operationalWh.some((warehouse) => warehouse.id === current)
          ? current
          : (operationalWh[0]?.id ?? ''),
      );
    } catch (e) {
      if (handleLoadError(e, 'org', isCurrent) === 'auth') return;
      if (!isCurrent()) return;
      toast(friendlyError(e), 'error');
    } finally {
      if (isCurrent()) setLoadingOrg(false);
    }
  }, [orgId, handleLoadError]);

  const reloadLots = useCallback(async () => {
    if (!selectedWh) return;
    const capturedOrgId = orgId;
    const capturedWh = selectedWh;
    const generation = ++lotGenerationRef.current;
    // Two-part staleness check: (1) the lot-generation must still be
    // the latest lot request; (2) the org+warehouse we captured must
    // still be the ones on screen. An obsolete lot fetch from a
    // previous organization or a previously-selected warehouse can
    // never repopulate lots.
    const isCurrent = () =>
      lotGenerationRef.current === generation &&
      capturedOrgId === orgId &&
      capturedWh === selectedWh;
    setLoadingLots(true);
    try {
      const list = await apiFetch<Lot[]>(`/v1/warehouses/${capturedWh}/lots`);
      if (!isCurrent()) return;
      setLots(list);
      // A successful lot reload clears any stale lot-scope 403 banner.
      setForbidden((f) => (f?.scope === 'lot' ? null : f));
    } catch (e) {
      if (handleLoadError(e, 'lot', isCurrent) === 'auth') return;
      if (!isCurrent()) return;
      toast(friendlyError(e), 'error');
    } finally {
      if (isCurrent()) setLoadingLots(false);
    }
  }, [selectedWh, orgId, handleLoadError]);

  // Sprint 5.1 review round #2 — organization context retention.
  //
  // When `orgId` changes we must IMMEDIATELY clear every piece of
  // organization-dependent state so no data from the previous org
  // is visible or actionable under the new org's heading. The
  // reload effects below then re-populate the workspace with data
  // from the newly selected org.
  //
  // Form-local state (Receive, Issue, Transfer, Adjust) is reset
  // separately via `key={orgId}` on each form-bearing panel so
  // React remounts the form components — see the panel wrappers
  // further down in this file.
  useEffect(() => {
    // Invalidate any in-flight organization / lot / history request
    // whose response arrives after this org change. `reloadOrg` /
    // `reloadLots` will bump the generation again on entry — the
    // extra bump here closes the window between orgId changing and
    // the reload callbacks running.
    orgGenerationRef.current += 1;
    lotGenerationRef.current += 1;
    historyGenerationRef.current += 1;
    setWarehouses([]);
    setItems([]);
    setOperationalWarehouses([]);
    setOperationalItems([]);
    setSelectedWh('');
    setLots([]);
    setSelectedLot('');
    setHistory([]);
    // Sprint 5.1 review round #4 — a permission failure on org-A
    // must never bleed into org-B; wipe the forbidden banner too.
    setForbidden(null);
  }, [orgId]);

  useEffect(() => {
    void reloadOrg();
  }, [reloadOrg]);
  useEffect(() => {
    void reloadLots();
  }, [reloadLots]);

  useEffect(() => {
    if (!selectedLot) return;
    const capturedOrgId = orgId;
    const capturedWh = selectedWh;
    const capturedLot = selectedLot;
    const generation = ++historyGenerationRef.current;
    const isCurrent = () =>
      historyGenerationRef.current === generation &&
      capturedOrgId === orgId &&
      capturedWh === selectedWh &&
      capturedLot === selectedLot;
    setLoadingHistory(true);
    apiFetch<{ items: LedgerTx[] }>(`/v1/lots/${capturedLot}/transactions`)
      .then((r) => {
        if (!isCurrent()) return;
        setHistory(r.items);
        // A successful history load clears any stale history-scope
        // 403 banner (leaves broader lot/org banners in place).
        setForbidden((f) => (f?.scope === 'history' ? null : f));
      })
      .catch((e) => {
        if (handleLoadError(e, 'history', isCurrent) === 'auth') return;
        if (!isCurrent()) return;
        toast(friendlyError(e), 'error');
      })
      .finally(() => {
        if (isCurrent()) setLoadingHistory(false);
      });
  }, [selectedLot, selectedWh, orgId, handleLoadError]);

  const operationalItemIds = useMemo(
    () => new Set(operationalItems.map((item) => item.id)),
    [operationalItems],
  );

  const operationalLots = useMemo(
    () => lots.filter((lot) => operationalItemIds.has(lot.item_id)),
    [lots, operationalItemIds],
  );

  const totalBalanceByItem = useMemo(() => {
    const acc = new Map<string, { balance: number; unit: string; name: string }>();
    for (const lot of operationalLots) {
      const item = operationalItems.find((i) => i.id === lot.item_id);
      const name = item?.name ?? lot.item_id;
      const prev = acc.get(name) ?? { balance: 0, unit: lot.balance_unit, name };
      prev.balance += Number(lot.balance);
      acc.set(name, prev);
    }
    return Array.from(acc.values());
  }, [operationalItems, operationalLots]);

  const currentWh = operationalWarehouses.find((w) => w.id === selectedWh) ?? null;

  const tabs: { key: Tab; label: string }[] = [
    { key: 'overview', label: 'Overview' },
    { key: 'warehouses', label: 'Warehouses' },
    { key: 'items', label: 'Items' },
    { key: 'lots', label: 'Lots & balances' },
    { key: 'receive', label: 'Receive stock' },
    { key: 'issue', label: 'Issue stock' },
    { key: 'transfer', label: 'Transfer stock' },
    { key: 'adjust', label: 'Adjust / reconcile' },
    { key: 'history', label: 'Transaction history' },
  ];

  return (
    <main className="mx-auto max-w-6xl px-6 py-10" data-testid="inventory-page">
      <div className="mb-6 flex items-start justify-between">
        <div>
          <p className="text-xs uppercase tracking-widest text-muted-foreground">
            Sprint 4 · Operational Resources
          </p>
          <h1 className="font-display text-3xl">Inventory</h1>
        </div>
        <div className="flex items-center gap-2 text-sm">
          <a
            href={
              orgId
                ? `/inventory/dashboard?organization_id=${encodeURIComponent(orgId)}`
                : '/inventory/dashboard'
            }
            data-testid="inventory-workspace-dashboard-link"
            className="rounded-md border border-border px-3 py-1.5 hover:bg-secondary"
          >
            Dashboard
          </a>
          <label className="text-muted-foreground">Organization</label>
          <select
            data-testid="inv-org-selector"
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
      </div>

      <nav className="mb-6 flex flex-wrap gap-2 border-b border-border pb-2" data-testid="inv-tabs">
        {tabs.map((t) => (
          <button
            key={t.key}
            type="button"
            data-testid={`inv-tab-${t.key}`}
            onClick={() => setTab(t.key)}
            className={`rounded-md px-3 py-1.5 text-sm ${
              tab === t.key
                ? 'bg-primary text-primary-foreground'
                : 'border border-border hover:bg-secondary'
            }`}
          >
            {t.label}
          </button>
        ))}
      </nav>

      {forbidden?.scope === 'org' ? (
        <InventoryForbiddenBanner scope="org" message={forbidden.message} />
      ) : (
        <>
          {tab === 'overview' && (
            <OverviewPanel
              loading={loadingOrg}
              warehouses={operationalWarehouses}
              items={operationalItems}
              lots={operationalLots}
              balances={totalBalanceByItem}
              onCreateWarehouse={() => setTab('warehouses')}
              onCreateItem={() => setTab('items')}
            />
          )}

          {tab === 'warehouses' && (
            <WarehousesPanel
              loading={loadingOrg}
              orgId={orgId}
              warehouses={warehouses}
              onChange={reloadOrg}
              onSelect={(id) => {
                if (operationalWarehouses.some((warehouse) => warehouse.id === id)) {
                  setSelectedWh(id);
                  setTab('lots');
                  return;
                }
                router.push(
                  `/inventory/warehouses/${id}?organization_id=${encodeURIComponent(orgId)}`,
                );
              }}
            />
          )}

          {tab === 'items' && (
            <ItemsPanel loading={loadingOrg} orgId={orgId} items={items} onChange={reloadOrg} />
          )}

          {tab === 'lots' &&
            (forbidden?.scope === 'lot' ? (
              <InventoryForbiddenBanner scope="lot" message={forbidden.message} />
            ) : (
              <LotsPanel
                loading={loadingLots}
                warehouses={operationalWarehouses}
                selectedWh={selectedWh}
                onSelectWh={setSelectedWh}
                lots={lots}
                items={items}
                onOpenLot={(id) => {
                  setSelectedLot(id);
                  setTab('history');
                }}
                onReceive={() => setTab('receive')}
              />
            ))}

          {tab === 'receive' && (
            <ReceivePanel
              key={orgId || 'no-org'}
              warehouses={operationalWarehouses}
              items={operationalItems}
              selectedWh={selectedWh}
              onSelectWh={setSelectedWh}
              onDone={reloadLots}
            />
          )}

          {tab === 'issue' && (
            <TxPanel
              key={orgId || 'no-org'}
              mode="issue"
              warehouse={currentWh}
              lots={operationalLots}
              items={operationalItems}
              warehouses={operationalWarehouses}
              onDone={reloadLots}
            />
          )}

          {tab === 'transfer' && (
            <TransferPanel
              key={orgId || 'no-org'}
              warehouses={operationalWarehouses}
              warehouse={currentWh}
              lots={operationalLots}
              items={operationalItems}
              onDone={reloadLots}
            />
          )}

          {tab === 'adjust' && (
            <TxPanel
              key={orgId || 'no-org'}
              mode="adjust"
              warehouse={currentWh}
              lots={operationalLots}
              items={operationalItems}
              warehouses={operationalWarehouses}
              onDone={reloadLots}
            />
          )}

          {tab === 'history' &&
            (forbidden?.scope === 'lot' || forbidden?.scope === 'history' ? (
              <InventoryForbiddenBanner scope={forbidden.scope} message={forbidden.message} />
            ) : (
              <HistoryPanel
                loading={loadingHistory}
                lots={lots}
                selectedLot={selectedLot}
                onSelect={setSelectedLot}
                history={history}
              />
            ))}
        </>
      )}
    </main>
  );
}

// -------------------------------------------------------------------- //
// Scoped forbidden banner (Sprint 5.1 review round #4). Kept local so
// it can carry a distinct data-testid per scope for both operator
// clarity and regression tests.
// -------------------------------------------------------------------- //
function InventoryForbiddenBanner({
  scope,
  message,
}: {
  scope: 'org' | 'lot' | 'history';
  message: string;
}) {
  return (
    <div
      className="rounded-2xl border border-dashed border-border bg-card/40 p-10 text-center"
      data-testid={`inv-forbidden-${scope}`}
    >
      <p className="font-display text-lg">You don&apos;t have access.</p>
      <p className="mt-1 text-sm text-muted-foreground">{message}</p>
    </div>
  );
}

// -------------------------------------------------------------------- //
// Overview
// -------------------------------------------------------------------- //
function OverviewPanel({
  loading,
  warehouses,
  items,
  lots,
  balances,
  onCreateWarehouse,
  onCreateItem,
}: {
  loading: boolean;
  warehouses: Warehouse[];
  items: InventoryItem[];
  lots: Lot[];
  balances: { balance: number; unit: string; name: string }[];
  onCreateWarehouse: () => void;
  onCreateItem: () => void;
}) {
  if (loading) {
    return (
      <section data-testid="inv-overview" className="grid gap-4 sm:grid-cols-3">
        <Skeleton className="h-20 w-full" />
        <Skeleton className="h-20 w-full" />
        <Skeleton className="h-20 w-full" />
      </section>
    );
  }
  if (warehouses.length === 0 && items.length === 0) {
    return (
      <EmptyStateCard
        testId="inv-overview-empty"
        title="Start your inventory workspace"
        description="Set up a warehouse and a catalog of items to begin tracking stock."
        action={
          <div className="flex gap-2">
            <button
              type="button"
              data-testid="inv-overview-cta-warehouse"
              onClick={onCreateWarehouse}
              className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground"
            >
              Create your first warehouse
            </button>
            <button
              type="button"
              data-testid="inv-overview-cta-item"
              onClick={onCreateItem}
              className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-secondary"
            >
              Add an item
            </button>
          </div>
        }
      />
    );
  }
  return (
    <section data-testid="inv-overview" className="grid gap-4 sm:grid-cols-3">
      <div className="rounded-2xl border border-border p-4">
        <p className="text-xs text-muted-foreground">Warehouses</p>
        <p className="font-display text-2xl">{warehouses.length}</p>
      </div>
      <div className="rounded-2xl border border-border p-4">
        <p className="text-xs text-muted-foreground">Items</p>
        <p className="font-display text-2xl">{items.length}</p>
      </div>
      <div className="rounded-2xl border border-border p-4">
        <p className="text-xs text-muted-foreground">Lots</p>
        <p className="font-display text-2xl">{lots.length}</p>
      </div>
      {balances.length > 0 && (
        <div className="col-span-full rounded-2xl border border-border p-4">
          <p className="mb-2 text-xs uppercase tracking-widest text-muted-foreground">
            Balances (canonical units)
          </p>
          <ul className="grid gap-1 text-sm sm:grid-cols-2">
            {balances.map((row) => (
              <li key={row.name} className="flex justify-between border-b border-border py-1">
                <span>{row.name}</span>
                <span className="font-mono">
                  {row.balance} {row.unit}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

// -------------------------------------------------------------------- //
// Warehouses
// -------------------------------------------------------------------- //
function WarehousesPanel({
  loading,
  orgId,
  warehouses,
  onChange,
  onSelect,
}: {
  loading: boolean;
  orgId: string;
  warehouses: Warehouse[];
  onChange: () => void;
  onSelect: (id: string) => void;
}) {
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState('');
  const [code, setCode] = useState('');
  const [busy, setBusy] = useState(false);
  const [query, setQuery] = useState('');

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return warehouses;
    return warehouses.filter(
      (w) => w.name.toLowerCase().includes(q) || w.code.toLowerCase().includes(q),
    );
  }, [warehouses, query]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      await apiFetch(`/v1/organizations/${orgId}/warehouses`, {
        method: 'POST',
        body: JSON.stringify({ name: name.trim(), code: code.trim() }),
      });
      toast(`Warehouse "${name.trim()}" created.`, 'success');
      setName('');
      setCode('');
      setCreating(false);
      onChange();
    } catch (e) {
      toast(friendlyError(e), 'error');
    } finally {
      setBusy(false);
    }
  }

  return (
    <section data-testid="inv-warehouses">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h2 className="font-display text-lg">Warehouses</h2>
        <div className="flex items-center gap-2">
          <input
            data-testid="inv-warehouse-search"
            placeholder="Search warehouses…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            className="rounded-md border border-border bg-background px-2 py-1 text-sm"
          />
          <button
            type="button"
            className="rounded-md border border-border px-3 py-1 text-sm hover:bg-secondary"
            data-testid="inv-warehouse-new"
            onClick={() => setCreating((v) => !v)}
          >
            {creating ? 'Cancel' : '+ New warehouse'}
          </button>
        </div>
      </div>
      {creating && (
        <form
          onSubmit={submit}
          className="mb-4 grid gap-2 rounded-md border border-border p-3 sm:grid-cols-3"
        >
          <input
            data-testid="inv-warehouse-name"
            placeholder="Name"
            className="rounded-md border border-border bg-background px-2 py-1 text-sm"
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
          />
          <input
            data-testid="inv-warehouse-code"
            placeholder="Code (unique per org)"
            className="rounded-md border border-border bg-background px-2 py-1 text-sm"
            value={code}
            onChange={(e) => setCode(e.target.value)}
            required
          />
          <button
            type="submit"
            disabled={busy}
            data-testid="inv-warehouse-submit"
            className="rounded-md bg-primary px-3 py-1 text-sm font-medium text-primary-foreground disabled:opacity-60"
          >
            {busy ? 'Saving…' : 'Create'}
          </button>
        </form>
      )}
      {loading ? (
        <SkeletonRows rows={4} testId="inv-warehouses-loading" />
      ) : warehouses.length === 0 ? (
        <EmptyStateCard
          testId="inv-warehouses-empty"
          title="Create your first warehouse"
          description="Warehouses hold inventory lots. Pin them to a farm or share them across the whole organization."
          action={
            <button
              type="button"
              data-testid="inv-warehouses-empty-cta"
              onClick={() => setCreating(true)}
              className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground"
            >
              + New warehouse
            </button>
          }
        />
      ) : filtered.length === 0 ? (
        <EmptyStateCard
          testId="inv-warehouses-no-match"
          title="No warehouses match your search"
          description="Try a different name or code."
        />
      ) : (
        <ul className="divide-y divide-border rounded-md border border-border">
          {filtered.map((w) => (
            <li key={w.id} className="flex items-center justify-between p-3">
              <div>
                <p className="font-medium">{w.name}</p>
                <p className="text-xs text-muted-foreground">
                  {w.code} · {w.status}
                  {w.farm_id ? ' · farm-pinned' : ' · org-shared'}
                </p>
              </div>
              <button
                type="button"
                data-testid={`inv-warehouse-open-${w.code}`}
                className="rounded-md border border-border px-3 py-1 text-xs hover:bg-secondary"
                onClick={() => onSelect(w.id)}
              >
                Open lots →
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

// -------------------------------------------------------------------- //
// Items
// -------------------------------------------------------------------- //
function ItemsPanel({
  loading,
  orgId,
  items,
  onChange,
}: {
  loading: boolean;
  orgId: string;
  items: InventoryItem[];
  onChange: () => void;
}) {
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState({
    code: '',
    name: '',
    category: 'feed' as (typeof CATEGORIES)[number],
    canonical_unit: 'kg' as (typeof UNITS)[number],
  });
  const [busy, setBusy] = useState(false);
  const [query, setQuery] = useState('');
  const [category, setCategory] = useState<string>('');

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return items.filter(
      (i) =>
        (!q || i.name.toLowerCase().includes(q) || i.code.toLowerCase().includes(q)) &&
        (!category || i.category === category),
    );
  }, [items, query, category]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      await apiFetch(`/v1/organizations/${orgId}/inventory-items`, {
        method: 'POST',
        body: JSON.stringify(form),
      });
      toast(`Item "${form.name}" added to catalog.`, 'success');
      setForm({ code: '', name: '', category: 'feed', canonical_unit: 'kg' });
      setCreating(false);
      onChange();
    } catch (e) {
      toast(friendlyError(e), 'error');
    } finally {
      setBusy(false);
    }
  }

  return (
    <section data-testid="inv-items">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h2 className="font-display text-lg">Catalog items</h2>
        <div className="flex items-center gap-2">
          <input
            data-testid="inv-item-search"
            placeholder="Search items…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            className="rounded-md border border-border bg-background px-2 py-1 text-sm"
          />
          <select
            data-testid="inv-item-filter-category"
            value={category}
            onChange={(e) => setCategory(e.target.value)}
            className="rounded-md border border-border bg-background px-2 py-1 text-sm"
          >
            <option value="">All categories</option>
            {CATEGORIES.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
          <button
            type="button"
            data-testid="inv-item-new"
            className="rounded-md border border-border px-3 py-1 text-sm hover:bg-secondary"
            onClick={() => setCreating((v) => !v)}
          >
            {creating ? 'Cancel' : '+ New item'}
          </button>
        </div>
      </div>
      {creating && (
        <form
          onSubmit={submit}
          className="mb-4 grid gap-2 rounded-md border border-border p-3 sm:grid-cols-4"
        >
          <input
            data-testid="inv-item-code"
            placeholder="Code"
            className="rounded-md border border-border bg-background px-2 py-1 text-sm"
            value={form.code}
            onChange={(e) => setForm({ ...form, code: e.target.value })}
            required
          />
          <input
            data-testid="inv-item-name"
            placeholder="Name"
            className="rounded-md border border-border bg-background px-2 py-1 text-sm"
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            required
          />
          <select
            data-testid="inv-item-category"
            className="rounded-md border border-border bg-background px-2 py-1 text-sm"
            value={form.category}
            onChange={(e) =>
              setForm({ ...form, category: e.target.value as (typeof CATEGORIES)[number] })
            }
          >
            {CATEGORIES.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
          <select
            data-testid="inv-item-unit"
            className="rounded-md border border-border bg-background px-2 py-1 text-sm"
            value={form.canonical_unit}
            onChange={(e) =>
              setForm({ ...form, canonical_unit: e.target.value as (typeof UNITS)[number] })
            }
          >
            {UNITS.map((u) => (
              <option key={u} value={u}>
                {u}
              </option>
            ))}
          </select>
          <button
            type="submit"
            disabled={busy}
            data-testid="inv-item-submit"
            className="col-span-full rounded-md bg-primary px-3 py-1 text-sm font-medium text-primary-foreground disabled:opacity-60"
          >
            {busy ? 'Saving…' : 'Create item'}
          </button>
        </form>
      )}
      {loading ? (
        <SkeletonRows rows={4} testId="inv-items-loading" />
      ) : items.length === 0 ? (
        <EmptyStateCard
          testId="inv-items-empty"
          title="Add your first catalog item"
          description="Items describe what you receive into stock — feed, medicine, chemicals, or supplies."
          action={
            <button
              type="button"
              data-testid="inv-items-empty-cta"
              onClick={() => setCreating(true)}
              className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground"
            >
              + New item
            </button>
          }
        />
      ) : filtered.length === 0 ? (
        <EmptyStateCard
          testId="inv-items-no-match"
          title="No items match those filters"
          description="Try clearing the search box or the category filter."
        />
      ) : (
        <ul className="divide-y divide-border rounded-md border border-border">
          {filtered.map((it) => (
            <li key={it.id} className="p-3">
              <p className="font-medium">
                {it.name} <span className="text-xs text-muted-foreground">({it.code})</span>
              </p>
              <p className="text-xs text-muted-foreground">
                {it.category} · canonical unit {it.canonical_unit}
              </p>
              <p className="font-mono text-[10px] text-muted-foreground">{it.id}</p>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

// -------------------------------------------------------------------- //
// Lots + balances
// -------------------------------------------------------------------- //
function LotsPanel({
  loading,
  warehouses,
  selectedWh,
  onSelectWh,
  lots,
  items,
  onOpenLot,
  onReceive,
}: {
  loading: boolean;
  warehouses: Warehouse[];
  selectedWh: string;
  onSelectWh: (id: string) => void;
  lots: Lot[];
  items: InventoryItem[];
  onOpenLot: (id: string) => void;
  onReceive: () => void;
}) {
  const [query, setQuery] = useState('');

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return lots;
    return lots.filter((lot) => {
      const item = items.find((i) => i.id === lot.item_id);
      return (
        lot.lot_code.toLowerCase().includes(q) ||
        (item?.name.toLowerCase().includes(q) ?? false) ||
        (item?.code.toLowerCase().includes(q) ?? false)
      );
    });
  }, [lots, items, query]);

  return (
    <section data-testid="inv-lots">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h2 className="font-display text-lg">Lots &amp; balances</h2>
        <div className="flex items-center gap-2">
          <input
            data-testid="inv-lots-search"
            placeholder="Search lot / item…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            className="rounded-md border border-border bg-background px-2 py-1 text-sm"
          />
          <select
            data-testid="inv-lots-warehouse"
            className="rounded-md border border-border bg-background px-2 py-1 text-sm"
            value={selectedWh}
            onChange={(e) => onSelectWh(e.target.value)}
          >
            {warehouses.map((w) => (
              <option key={w.id} value={w.id}>
                {w.name}
              </option>
            ))}
          </select>
        </div>
      </div>
      {loading ? (
        <SkeletonRows rows={5} testId="inv-lots-loading" />
      ) : lots.length === 0 ? (
        <EmptyStateCard
          testId="inv-lots-empty"
          title="No lots in this warehouse yet"
          description="Receive stock to create the first lot."
          action={
            <button
              type="button"
              data-testid="inv-lots-empty-cta"
              onClick={onReceive}
              className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground"
            >
              + Receive stock
            </button>
          }
        />
      ) : filtered.length === 0 ? (
        <EmptyStateCard
          testId="inv-lots-no-match"
          title="No lots match your search"
          description="Clear the search box to see all lots."
        />
      ) : (
        <div className="overflow-x-auto rounded-md border border-border">
          <table className="w-full text-sm">
            <thead className="bg-secondary/40 text-xs uppercase tracking-widest text-muted-foreground">
              <tr>
                <th className="px-3 py-2 text-left">Lot</th>
                <th className="px-3 py-2 text-left">Item</th>
                <th className="px-3 py-2 text-left">Expiry</th>
                <th className="px-3 py-2 text-right">Balance</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {filtered.map((lot) => {
                const item = items.find((i) => i.id === lot.item_id);
                return (
                  <tr key={lot.id} className="border-t border-border">
                    <td className="px-3 py-2 font-mono">{lot.lot_code}</td>
                    <td className="px-3 py-2">{item?.name ?? lot.item_id}</td>
                    <td className="px-3 py-2">{lot.expiry_date ?? '—'}</td>
                    <td className="px-3 py-2 text-right font-mono">
                      {lot.balance} {lot.balance_unit}
                    </td>
                    <td className="px-3 py-2 text-right">
                      <button
                        type="button"
                        data-testid={`inv-lot-open-${lot.lot_code}`}
                        className="text-xs text-primary hover:underline"
                        onClick={() => onOpenLot(lot.id)}
                      >
                        History →
                      </button>
                      <div className="mt-0.5 font-mono text-[10px] text-muted-foreground">
                        {lot.id}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

// -------------------------------------------------------------------- //
// Receive
// -------------------------------------------------------------------- //
function ReceivePanel({
  warehouses,
  items,
  selectedWh,
  onSelectWh,
  onDone,
}: {
  warehouses: Warehouse[];
  items: InventoryItem[];
  selectedWh: string;
  onSelectWh: (id: string) => void;
  onDone: () => void;
}) {
  const [itemId, setItemId] = useState('');
  const [lotCode, setLotCode] = useState('');
  const [qty, setQty] = useState('');
  const [unit, setUnit] = useState<(typeof UNITS)[number]>('kg');
  const [busy, setBusy] = useState(false);
  const eligibleWarehouses = warehouses.filter((warehouse) =>
    isWarehouseEligibleForStockOperation(warehouse, 'receive'),
  );

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (busy) return;
    // Sprint 5.1 review round #2 — last-line-of-defence guard so a
    // stale warehouse or item selection from a previous org cannot
    // be POSTed. The backend still validates, but surfacing this in
    // the UI avoids a confusing 403 / 404 round-trip and lets us
    // clear the offending field.
    if (!isWarehouseInCurrentOrg(selectedWh, warehouses)) {
      toast('Selected warehouse no longer belongs to this organization.', 'error');
      return;
    }
    const selectedWarehouse = warehouses.find((warehouse) => warehouse.id === selectedWh);
    if (!selectedWarehouse || !isWarehouseEligibleForStockOperation(selectedWarehouse, 'receive')) {
      toast('Selected warehouse is not available for receiving stock.', 'error');
      return;
    }
    if (!isItemInCurrentOrg(itemId, items)) {
      toast('Selected item no longer belongs to this organization.', 'error');
      return;
    }
    setBusy(true);
    try {
      await postWithKey(
        `/v1/warehouses/${selectedWh}/inventory:receive`,
        {
          item_id: itemId,
          lot_code: lotCode.trim(),
          quantity: Number(qty),
          unit,
        },
        idem('receipt'),
      );
      toast(`Received ${qty} ${unit} into lot ${lotCode.trim()}.`, 'success');
      setLotCode('');
      setQty('');
      onDone();
    } catch (e) {
      toast(friendlyError(e), 'error');
    } finally {
      setBusy(false);
    }
  }

  if (warehouses.length === 0 || items.length === 0) {
    return (
      <EmptyStateCard
        testId="inv-receive-blocked"
        title="Set up warehouses and items first"
        description="Receiving stock requires at least one warehouse and one catalog item."
      />
    );
  }

  return (
    <form
      onSubmit={submit}
      className="grid gap-3 rounded-md border border-border p-4 sm:grid-cols-2"
      data-testid="inv-receive-form"
    >
      <label className="block text-sm">
        Warehouse
        <select
          data-testid="inv-receive-warehouse"
          className="mt-1 w-full rounded-md border border-border bg-background px-2 py-1"
          value={selectedWh}
          onChange={(e) => onSelectWh(e.target.value)}
        >
          {eligibleWarehouses.map((w) => (
            <option key={w.id} value={w.id}>
              {w.name}
            </option>
          ))}
        </select>
      </label>
      <label className="block text-sm">
        Item
        <select
          data-testid="inv-receive-item"
          className="mt-1 w-full rounded-md border border-border bg-background px-2 py-1"
          value={itemId}
          onChange={(e) => setItemId(e.target.value)}
          required
        >
          <option value="">— select —</option>
          {items.map((i) => (
            <option key={i.id} value={i.id}>
              {i.name} ({i.canonical_unit})
            </option>
          ))}
        </select>
      </label>
      <label className="block text-sm">
        Lot code
        <input
          data-testid="inv-receive-lot-code"
          className="mt-1 w-full rounded-md border border-border bg-background px-2 py-1"
          value={lotCode}
          onChange={(e) => setLotCode(e.target.value)}
          required
        />
      </label>
      <div className="grid grid-cols-2 gap-2">
        <label className="block text-sm">
          Quantity
          <input
            data-testid="inv-receive-quantity"
            type="number"
            step="0.001"
            min="0.001"
            className="mt-1 w-full rounded-md border border-border bg-background px-2 py-1"
            value={qty}
            onChange={(e) => setQty(e.target.value)}
            required
          />
        </label>
        <label className="block text-sm">
          Unit
          <select
            data-testid="inv-receive-unit"
            className="mt-1 w-full rounded-md border border-border bg-background px-2 py-1"
            value={unit}
            onChange={(e) => setUnit(e.target.value as (typeof UNITS)[number])}
          >
            {UNITS.map((u) => (
              <option key={u} value={u}>
                {u}
              </option>
            ))}
          </select>
        </label>
      </div>
      <button
        type="submit"
        disabled={busy || !selectedWh || !itemId}
        data-testid="inv-receive-submit"
        className="col-span-full rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-60"
      >
        {busy ? 'Posting…' : 'Post receipt'}
      </button>
    </form>
  );
}

// -------------------------------------------------------------------- //
// Issue / Adjust (destructive → confirmation dialog)
// -------------------------------------------------------------------- //
function TxPanel({
  mode,
  warehouse,
  lots,
  items,
  warehouses,
  onDone,
}: {
  mode: 'issue' | 'adjust';
  warehouse: Warehouse | null;
  lots: Lot[];
  items: InventoryItem[];
  warehouses: Warehouse[];
  onDone: () => void;
}) {
  const [lotId, setLotId] = useState('');
  const [qty, setQty] = useState('');
  const [unit, setUnit] = useState<(typeof UNITS)[number]>('kg');
  const [direction, setDirection] = useState<'increase' | 'decrease'>('decrease');
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [pendingConfirm, setPendingConfirm] = useState(false);
  const warehouseOperation =
    mode === 'issue' ? 'issue' : direction === 'increase' ? 'adjust-increase' : 'adjust-decrease';
  const warehouseEligible =
    warehouse !== null && isWarehouseEligibleForStockOperation(warehouse, warehouseOperation);

  async function doSubmit() {
    if (!warehouse) return;
    if (!isWarehouseEligibleForStockOperation(warehouse, warehouseOperation)) {
      setPendingConfirm(false);
      return;
    }
    // Sprint 5.1 review round #2 — cross-org guardrails.
    if (!isWarehouseInCurrentOrg(warehouse.id, warehouses)) {
      toast('Selected warehouse no longer belongs to this organization.', 'error');
      setPendingConfirm(false);
      return;
    }
    if (!isLotInCurrentOrg(lotId, lots, warehouses, items)) {
      toast('Selected lot no longer belongs to this organization.', 'error');
      setPendingConfirm(false);
      return;
    }
    setBusy(true);
    try {
      if (mode === 'issue') {
        await postWithKey(
          `/v1/warehouses/${warehouse.id}/inventory:issue`,
          { lot_id: lotId, quantity: Number(qty), unit, reason: reason || undefined },
          idem('issue'),
        );
        toast(`Issued ${qty} ${unit}.`, 'success');
      } else {
        await postWithKey(
          `/v1/warehouses/${warehouse.id}/inventory:adjust`,
          { lot_id: lotId, quantity: Number(qty), unit, direction, reason },
          idem('adjust'),
        );
        toast(`Adjustment (${direction}) posted.`, 'success');
      }
      setQty('');
      setReason('');
      setLotId('');
      onDone();
    } catch (e) {
      toast(friendlyError(e), 'error');
    } finally {
      setBusy(false);
      setPendingConfirm(false);
    }
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    if (!warehouseEligible) return;
    // Adjust always confirms; Issue confirms when decreasing.
    setPendingConfirm(true);
  }

  if (!warehouse || lots.length === 0) {
    return (
      <EmptyStateCard
        testId={`inv-${mode}-blocked`}
        title="No lots available"
        description="Receive stock into this warehouse before issuing or adjusting."
      />
    );
  }

  const lot = lots.find((l) => l.id === lotId);
  const item = lot ? items.find((i) => i.id === lot.item_id) : null;

  return (
    <>
      <form
        onSubmit={submit}
        className="grid gap-3 rounded-md border border-border p-4 sm:grid-cols-2"
        data-testid={`inv-${mode}-form`}
      >
        <label className="block text-sm">
          Lot
          <select
            data-testid={`inv-${mode}-lot`}
            className="mt-1 w-full rounded-md border border-border bg-background px-2 py-1"
            value={lotId}
            onChange={(e) => setLotId(e.target.value)}
            required
          >
            <option value="">— select —</option>
            {lots.map((l) => {
              const it = items.find((i) => i.id === l.item_id);
              return (
                <option key={l.id} value={l.id}>
                  {l.lot_code} · {it?.name} · {l.balance} {l.balance_unit}
                </option>
              );
            })}
          </select>
        </label>
        {mode === 'adjust' && (
          <label className="block text-sm">
            Direction
            <select
              data-testid="inv-adjust-direction"
              className="mt-1 w-full rounded-md border border-border bg-background px-2 py-1"
              value={direction}
              onChange={(e) => {
                const nextDirection = e.target.value as 'increase' | 'decrease';
                setDirection(nextDirection);
                if (
                  warehouse &&
                  !isWarehouseEligibleForStockOperation(
                    warehouse,
                    nextDirection === 'increase' ? 'adjust-increase' : 'adjust-decrease',
                  )
                ) {
                  setLotId('');
                  setPendingConfirm(false);
                }
              }}
            >
              <option value="decrease">Decrease</option>
              <option value="increase">Increase</option>
            </select>
          </label>
        )}
        <div className="grid grid-cols-2 gap-2">
          <label className="block text-sm">
            Quantity
            <input
              data-testid={`inv-${mode}-qty`}
              type="number"
              step="0.001"
              min="0.001"
              className="mt-1 w-full rounded-md border border-border bg-background px-2 py-1"
              value={qty}
              onChange={(e) => setQty(e.target.value)}
              required
            />
          </label>
          <label className="block text-sm">
            Unit
            <select
              data-testid={`inv-${mode}-unit`}
              className="mt-1 w-full rounded-md border border-border bg-background px-2 py-1"
              value={unit}
              onChange={(e) => setUnit(e.target.value as (typeof UNITS)[number])}
            >
              {UNITS.map((u) => (
                <option key={u} value={u}>
                  {u}
                </option>
              ))}
            </select>
          </label>
        </div>
        <label className="col-span-full block text-sm">
          Reason {mode === 'adjust' ? '(required)' : '(optional)'}
          <textarea
            data-testid={`inv-${mode}-reason`}
            rows={2}
            className="mt-1 w-full rounded-md border border-border bg-background px-2 py-1"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            required={mode === 'adjust'}
          />
        </label>
        <button
          type="submit"
          disabled={busy || !warehouseEligible || !lotId}
          data-testid={`inv-${mode}-submit`}
          className="col-span-full rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-60"
        >
          {busy ? 'Posting…' : mode === 'issue' ? 'Post issue' : 'Post adjustment'}
        </button>
      </form>
      <ConfirmDialog
        open={pendingConfirm}
        busy={busy}
        destructive={mode === 'adjust' || direction === 'decrease'}
        testId={`inv-${mode}-confirm`}
        title={mode === 'issue' ? 'Confirm inventory issue' : 'Confirm inventory adjustment'}
        description={
          mode === 'issue'
            ? `Issue ${qty} ${unit} from lot ${lot?.lot_code ?? ''} (${item?.name ?? ''}). This is an append-only ledger entry.`
            : `${direction === 'decrease' ? 'Decrease' : 'Increase'} lot ${lot?.lot_code ?? ''} (${item?.name ?? ''}) by ${qty} ${unit}. Provide a written reason for the audit log.`
        }
        confirmLabel={mode === 'issue' ? 'Post issue' : 'Post adjustment'}
        onConfirm={doSubmit}
        onCancel={() => setPendingConfirm(false)}
      />
    </>
  );
}

// -------------------------------------------------------------------- //
// Transfer
// -------------------------------------------------------------------- //
function TransferPanel({
  warehouses,
  warehouse,
  lots,
  items,
  onDone,
}: {
  warehouses: Warehouse[];
  warehouse: Warehouse | null;
  lots: Lot[];
  items: InventoryItem[];
  onDone: () => void;
}) {
  const [lotId, setLotId] = useState('');
  const [dstWh, setDstWh] = useState('');
  const [qty, setQty] = useState('');
  const [unit, setUnit] = useState<(typeof UNITS)[number]>('kg');
  const [busy, setBusy] = useState(false);
  const sourceEligible =
    warehouse !== null && isWarehouseEligibleForStockOperation(warehouse, 'transfer-source');

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!warehouse || busy || !sourceEligible) return;
    // Sprint 5.1 review round #2 — validate source, destination and
    // lot all belong to the currently active organization before
    // POSTing. Prevents a lingering post-org-switch selection from
    // sending a cross-tenant reference to the backend.
    if (!isWarehouseInCurrentOrg(warehouse.id, warehouses)) {
      toast('Source warehouse no longer belongs to this organization.', 'error');
      return;
    }
    if (!isWarehouseInCurrentOrg(dstWh, warehouses)) {
      toast('Destination warehouse no longer belongs to this organization.', 'error');
      return;
    }
    const destinationWarehouse = warehouses.find((candidate) => candidate.id === dstWh);
    if (
      !destinationWarehouse ||
      !isWarehouseEligibleForStockOperation(destinationWarehouse, 'transfer-destination')
    ) {
      toast('Destination warehouse is not available for receiving stock.', 'error');
      return;
    }
    if (!isLotInCurrentOrg(lotId, lots, warehouses, items)) {
      toast('Selected lot no longer belongs to this organization.', 'error');
      return;
    }
    setBusy(true);
    try {
      await postWithKey(
        `/v1/warehouses/${warehouse.id}/inventory:transfer`,
        {
          lot_id: lotId,
          destination_warehouse_id: dstWh,
          quantity: Number(qty),
          unit,
        },
        idem('transfer'),
      );
      toast(`Transferred ${qty} ${unit}.`, 'success');
      setLotId('');
      setDstWh('');
      setQty('');
      onDone();
    } catch (e) {
      toast(friendlyError(e), 'error');
    } finally {
      setBusy(false);
    }
  }

  if (!warehouse || lots.length === 0 || warehouses.length < 2) {
    return (
      <EmptyStateCard
        testId="inv-transfer-blocked"
        title="Transfers require two warehouses with stock"
        description="Create a second warehouse and receive stock into the source lot before transferring."
      />
    );
  }

  return (
    <form
      onSubmit={submit}
      className="grid gap-3 rounded-md border border-border p-4 sm:grid-cols-2"
      data-testid="inv-transfer-form"
    >
      <label className="block text-sm">
        Source lot
        <select
          data-testid="inv-transfer-lot"
          className="mt-1 w-full rounded-md border border-border bg-background px-2 py-1"
          value={lotId}
          onChange={(e) => setLotId(e.target.value)}
          required
        >
          <option value="">— select —</option>
          {lots.map((lot) => {
            const item = items.find((i) => i.id === lot.item_id);
            return (
              <option key={lot.id} value={lot.id}>
                {lot.lot_code} · {item?.name} · {lot.balance} {lot.balance_unit}
              </option>
            );
          })}
        </select>
      </label>
      <label className="block text-sm">
        Destination warehouse
        <select
          data-testid="inv-transfer-destination"
          className="mt-1 w-full rounded-md border border-border bg-background px-2 py-1"
          value={dstWh}
          onChange={(e) => setDstWh(e.target.value)}
          required
        >
          <option value="">— select —</option>
          {warehouses
            .filter(
              (w) =>
                w.id !== warehouse?.id &&
                isWarehouseEligibleForStockOperation(w, 'transfer-destination'),
            )
            .map((w) => (
              <option key={w.id} value={w.id}>
                {w.name}
              </option>
            ))}
        </select>
      </label>
      <div className="grid grid-cols-2 gap-2">
        <label className="block text-sm">
          Quantity
          <input
            data-testid="inv-transfer-qty"
            type="number"
            step="0.001"
            min="0.001"
            className="mt-1 w-full rounded-md border border-border bg-background px-2 py-1"
            value={qty}
            onChange={(e) => setQty(e.target.value)}
            required
          />
        </label>
        <label className="block text-sm">
          Unit
          <select
            data-testid="inv-transfer-unit"
            className="mt-1 w-full rounded-md border border-border bg-background px-2 py-1"
            value={unit}
            onChange={(e) => setUnit(e.target.value as (typeof UNITS)[number])}
          >
            {UNITS.map((u) => (
              <option key={u} value={u}>
                {u}
              </option>
            ))}
          </select>
        </label>
      </div>
      <button
        type="submit"
        disabled={busy || !sourceEligible || !lotId || !dstWh}
        data-testid="inv-transfer-submit"
        className="col-span-full rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-60"
      >
        {busy ? 'Posting…' : 'Post transfer'}
      </button>
    </form>
  );
}

// -------------------------------------------------------------------- //
// Transaction history
// -------------------------------------------------------------------- //
const TX_TYPES = [
  'receipt',
  'issue',
  'adjustment_increase',
  'adjustment_decrease',
  'transfer_out',
  'transfer_in',
  'reversal',
  'consumption',
] as const;

function HistoryPanel({
  loading,
  lots,
  selectedLot,
  onSelect,
  history,
}: {
  loading: boolean;
  lots: Lot[];
  selectedLot: string;
  onSelect: (id: string) => void;
  history: LedgerTx[];
}) {
  const [filterType, setFilterType] = useState<string>('');

  const filtered = useMemo(() => {
    if (!filterType) return history;
    return history.filter((h) => h.transaction_type === filterType);
  }, [history, filterType]);

  return (
    <section data-testid="inv-history">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h2 className="font-display text-lg">Transaction history</h2>
        <div className="flex items-center gap-2">
          <select
            data-testid="inv-history-filter-type"
            value={filterType}
            onChange={(e) => setFilterType(e.target.value)}
            className="rounded-md border border-border bg-background px-2 py-1 text-sm"
          >
            <option value="">All types</option>
            {TX_TYPES.map((t) => (
              <option key={t} value={t}>
                {t.replace(/_/g, ' ')}
              </option>
            ))}
          </select>
          <select
            data-testid="inv-history-lot"
            className="rounded-md border border-border bg-background px-2 py-1 text-sm"
            value={selectedLot}
            onChange={(e) => onSelect(e.target.value)}
          >
            <option value="">— pick a lot —</option>
            {lots.map((lot) => (
              <option key={lot.id} value={lot.id}>
                {lot.lot_code}
              </option>
            ))}
          </select>
        </div>
      </div>
      {!selectedLot ? (
        <EmptyStateCard
          testId="inv-history-pick"
          title="Pick a lot to view its ledger"
          description="Every receipt, issue, transfer, adjustment, reversal and feeding consumption for a lot appears here."
        />
      ) : loading ? (
        <SkeletonRows rows={5} testId="inv-history-loading" />
      ) : filtered.length === 0 ? (
        <EmptyStateCard
          testId="inv-history-empty"
          title={history.length === 0 ? 'No transactions yet' : 'No transactions match this filter'}
          description={
            history.length === 0
              ? 'Post a receipt into this lot to see its first ledger entry.'
              : 'Try clearing the type filter.'
          }
        />
      ) : (
        <div className="overflow-x-auto rounded-md border border-border">
          <table className="w-full text-sm">
            <thead className="bg-secondary/40 text-xs uppercase tracking-widest text-muted-foreground">
              <tr>
                <th className="px-3 py-2 text-left">When</th>
                <th className="px-3 py-2 text-left">Type</th>
                <th className="px-3 py-2 text-right">Qty</th>
                <th className="px-3 py-2 text-left">Reason</th>
                <th className="px-3 py-2 text-left">Ref</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((tx) => (
                <tr key={tx.id} className="border-t border-border">
                  <td className="px-3 py-2 font-mono text-xs">{tx.performed_at}</td>
                  <td className="px-3 py-2">{tx.transaction_type}</td>
                  <td className="px-3 py-2 text-right font-mono">
                    {tx.quantity} {tx.unit}
                  </td>
                  <td className="px-3 py-2">{tx.reason ?? '—'}</td>
                  <td className="px-3 py-2 text-xs text-muted-foreground">
                    {tx.reference_type ?? ''}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
