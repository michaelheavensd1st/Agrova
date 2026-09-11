'use client';

/**
 * Sprint 5.3 — Inventory Item detail route.
 *
 * `/inventory/items/[itemId]?organization_id=…`
 *
 * There is no dedicated `GET /inventory-items/{id}` endpoint on
 * the backend, so we resolve the item via the org's list
 * endpoint. That has the useful side effect of enforcing tenant
 * membership at the same time: if the URL points at an item that
 * does not belong to the active org, we render a scoped
 * forbidden banner rather than silently switching tenants.
 *
 * Availability + activity are derived via bounded fan-out
 * (max 5 concurrent) against `/warehouses/{whId}/lots` +
 * `/lots/{lotId}/transactions`. Partial failures show an
 * explicit "understated" indicator; 401 redirects to /login;
 * 403 shows a scoped banner and clears the affected slice.
 *
 * Sprint 5.3 review round:
 *  - Finding 1 (route identity): the effective page identity is
 *    `orgId + itemId`. Every async operation captures both at
 *    start and guards its state write with a `sameRoute()` check
 *    so a stale completion from a previous item can never write
 *    into a newly-navigated item's context. Refs are bumped on
 *    either dimension changing so obsolete fan-outs drop cleanly.
 *  - Finding 2 (activity pagination): transactions requests
 *    include `?limit=100` (the display cap). If any lot returns
 *    a `next_cursor` we surface the activity list as `partial`
 *    rather than following the cursor, keeping the fan-out
 *    bounded.
 */

import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import Link from 'next/link';
import { useParams, usePathname, useRouter, useSearchParams } from 'next/navigation';
import { apiFetch, ApiError } from '@/lib/api';
import {
  ACTIVITY_CONCURRENCY,
  ACTIVITY_PER_LOT_LIMIT,
  WAREHOUSE_LOT_CONCURRENCY,
  buildItemAvailability,
  inspectFanOut,
  inspectWarehouseLotFanOut,
  mapWithConcurrency,
  type InventoryItem,
  type ItemLedgerTx,
  type ItemLot,
  type ItemOrganization,
  type ItemWarehouse,
  type TransactionPage,
} from '@/lib/inventory-items';
import { friendlyError, toast, ConfirmDialog, SkeletonRows } from '@/components/ui-polish';
import { ErrorBanner } from '@/components/ape-ui';
import { InventoryItemHeader } from '@/components/inventory-items/inventory-item-header';
import { InventoryItemSummary } from '@/components/inventory-items/inventory-item-summary';
import { InventoryItemAvailabilityTable } from '@/components/inventory-items/inventory-item-availability-table';
import { InventoryItemLotsTable } from '@/components/inventory-items/inventory-item-lots-table';
import { InventoryItemActivity } from '@/components/inventory-items/inventory-item-activity';
import { InventoryItemQuickActions } from '@/components/inventory-items/inventory-item-quick-actions';
import { InventoryItemForbiddenBanner } from '@/components/inventory-items/inventory-item-forbidden-banner';
import {
  InventoryItemForm,
  type ItemFormPayload,
} from '@/components/inventory-items/inventory-item-form';
import { StockOperationDialog } from '@/components/inventory-items/stock-operation-dialog';
import type { StockOperationType } from '@/lib/stock-operations';

type ForbiddenScope = 'org' | 'item' | 'availability' | 'activity';

function apiErrorStatus(reason: unknown): number | null {
  return reason instanceof ApiError ? reason.status : null;
}

// `useSearchParams` requires the containing tree to sit inside a
// Suspense boundary during static generation. The inner component
// owns every piece of client state.
export default function InventoryItemDetailPage() {
  return (
    <Suspense
      fallback={
        <main className="mx-auto max-w-6xl px-6 py-10" data-testid="item-detail-page-loading">
          <SkeletonRows rows={8} />
        </main>
      }
    >
      <InventoryItemDetailInner />
    </Suspense>
  );
}

function InventoryItemDetailInner() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const params = useParams<{ itemId: string }>();
  const itemId = params?.itemId ?? '';

  const [orgs, setOrgs] = useState<ItemOrganization[] | null>(null);
  const [orgId, setOrgId] = useState('');
  const [item, setItem] = useState<InventoryItem | null>(null);
  const [warehouses, setWarehouses] = useState<ItemWarehouse[]>([]);
  const [lots, setLots] = useState<ItemLot[]>([]);
  const [availabilityPartial, setAvailabilityPartial] = useState(false);
  const [activity, setActivity] = useState<ItemLedgerTx[]>([]);
  const [activityPartial, setActivityPartial] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadingActivity, setLoadingActivity] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [forbidden, setForbidden] = useState<{ scope: ForbiddenScope; message: string } | null>(
    null,
  );
  const [editing, setEditing] = useState(false);
  const [editBusy, setEditBusy] = useState(false);
  const [editError, setEditError] = useState<string | null>(null);
  const [pendingActive, setPendingActive] = useState<boolean | null>(null);
  const [statusBusy, setStatusBusy] = useState(false);
  // Sprint 5.4: which stock-operation dialog is open (if any) and,
  // for reversals, the transaction the caller wants to reverse.
  const [activeStockOp, setActiveStockOp] = useState<StockOperationType | null>(null);
  const [reversalTx, setReversalTx] = useState<ItemLedgerTx | null>(null);

  const itemRef = useRef(0);
  const availabilityRef = useRef(0);
  const activityRef = useRef(0);
  const currentOrgIdRef = useRef<string>('');
  const currentItemIdRef = useRef<string>('');
  useEffect(() => {
    currentOrgIdRef.current = orgId;
  }, [orgId]);
  // Track the *route* itemId (not the resolved-item id) so
  // mutation guards can detect a URL change even while the
  // previous item's data is still being loaded.
  useEffect(() => {
    currentItemIdRef.current = itemId;
  }, [itemId]);

  // Read the URL organization_id reactively. `useSearchParams` is
  // the single source of truth for the active org — the effect
  // below reconciles it into local state and normalizes missing
  // or invalid values back into the URL, so navigation between
  //   /inventory/items/item-1?organization_id=org-A
  //   /inventory/items/item-1?organization_id=org-B
  // (same itemId, different org query) is fully reactive and
  // clears org-A state before org-B data arrives.
  const requestedOrgId = searchParams.get('organization_id');

  const handleAuthError = useCallback(
    (err: unknown, scope: ForbiddenScope, isCurrent: () => boolean): 'auth' | 'unhandled' => {
      if (!(err instanceof ApiError)) return 'unhandled';
      if (err.status === 401) {
        router.push('/login');
        return 'auth';
      }
      if (err.status === 403) {
        if (!isCurrent()) return 'auth';
        itemRef.current += 1;
        availabilityRef.current += 1;
        activityRef.current += 1;
        if (scope === 'org' || scope === 'item') {
          setItem(null);
          setWarehouses([]);
          setLots([]);
          setActivity([]);
        } else if (scope === 'availability') {
          setLots([]);
          setActivity([]);
        } else {
          setActivity([]);
        }
        const messages: Record<ForbiddenScope, string> = {
          org: "You don't have permission to view organizations.",
          item: "You don't have permission to view this item.",
          availability: "You don't have permission to view warehouse availability.",
          activity: "You don't have permission to view activity for this item.",
        };
        setForbidden({ scope, message: messages[scope] });
        return 'auth';
      }
      return 'unhandled';
    },
    [router],
  );

  // Bootstrap: load the list of accessible organizations. Selection
  // itself is derived reactively from the URL — see the sync effect
  // immediately after.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const list = await apiFetch<ItemOrganization[]>('/v1/organizations');
        if (cancelled) return;
        setOrgs(list);
        if (list.length === 0) {
          router.push('/onboarding');
          return;
        }
      } catch (err) {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 401) {
          router.push('/login');
          return;
        }
        if (err instanceof ApiError && err.status === 403) {
          setForbidden({
            scope: 'org',
            message: "You don't have permission to view organizations.",
          });
          setLoading(false);
          return;
        }
        setError(friendlyError(err));
        setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [router]);

  // Unified URL ↔ state reconciliation.
  //   1. Requested org is valid   → use it.
  //   2. Requested org is missing → normalize URL to fallback.
  //   3. Requested org is invalid → normalize URL to fallback.
  // Loop prevention: only replace when the URL's raw parameter
  // differs from the effective value.
  useEffect(() => {
    if (!orgs || orgs.length === 0) return;
    const isRequestedOrgValid =
      requestedOrgId !== null && orgs.some((o) => o.id === requestedOrgId);
    const effectiveOrgId = isRequestedOrgValid ? requestedOrgId : (orgs[0]?.id ?? null);
    if (!effectiveOrgId) return;
    if (effectiveOrgId !== orgId) setOrgId(effectiveOrgId);
    if (requestedOrgId !== effectiveOrgId) {
      const next = new URLSearchParams(searchParams.toString());
      next.set('organization_id', effectiveOrgId);
      router.replace(`${pathname}?${next.toString()}`);
    }
  }, [requestedOrgId, orgs, orgId, pathname, router, searchParams]);

  // Load item (from list) + warehouses. Two independent guarded
  // fetches so a slow warehouses call cannot invalidate the item.
  const loadDetail = useCallback(async () => {
    if (!orgId || !itemId) return;
    const capturedOrgId = orgId;
    const capturedItemId = itemId;
    const gen = ++itemRef.current;
    const invGen = ++availabilityRef.current;
    const isCurrent = () =>
      itemRef.current === gen &&
      availabilityRef.current === invGen &&
      capturedOrgId === currentOrgIdRef.current &&
      capturedItemId === currentItemIdRef.current;
    setLoading(true);
    setError(null);
    try {
      const [orgItems, orgWarehouses] = await Promise.all([
        apiFetch<InventoryItem[]>(`/v1/organizations/${capturedOrgId}/inventory-items`),
        apiFetch<ItemWarehouse[]>(`/v1/organizations/${capturedOrgId}/warehouses`),
      ]);
      if (!isCurrent()) return;
      const match = orgItems.find((i) => i.id === capturedItemId);
      if (!match) {
        // Cross-tenant / not-found: never silently swap the org.
        setItem(null);
        setWarehouses([]);
        setLots([]);
        setActivity([]);
        setForbidden({
          scope: 'item',
          message:
            'This item does not belong to the active organization, or you do not have access to it.',
        });
        return;
      }
      setItem(match);
      setWarehouses(orgWarehouses);
      setForbidden((f) => (f && f.scope === 'org' ? null : f));
    } catch (err) {
      if (handleAuthError(err, 'item', isCurrent) === 'auth') return;
      if (!isCurrent()) return;
      setError(friendlyError(err));
    } finally {
      if (isCurrent()) setLoading(false);
    }
  }, [orgId, itemId, handleAuthError]);

  // Availability fan-out (bounded 5): fetch lots per warehouse,
  // then keep only lots that reference this item.
  const loadAvailability = useCallback(async () => {
    if (!item || warehouses.length === 0) {
      setLots([]);
      setAvailabilityPartial(false);
      return;
    }
    const capturedOrgId = orgId;
    const capturedItemId = item.id;
    const gen = ++availabilityRef.current;
    const isCurrent = () =>
      availabilityRef.current === gen &&
      capturedOrgId === currentOrgIdRef.current &&
      capturedItemId === currentItemIdRef.current;
    try {
      const settled = await mapWithConcurrency(warehouses, WAREHOUSE_LOT_CONCURRENCY, (wh) =>
        apiFetch<ItemLot[]>(`/v1/warehouses/${wh.id}/lots`),
      );
      if (!isCurrent()) return;
      const outcome = inspectWarehouseLotFanOut(settled, apiErrorStatus);
      if (outcome.kind === 'unauthenticated') {
        router.push('/login');
        return;
      }
      if (outcome.kind === 'forbidden') {
        availabilityRef.current += 1;
        setLots([]);
        setForbidden({
          scope: 'availability',
          message: "You don't have permission to view warehouse availability for this item.",
        });
        return;
      }
      const filtered = outcome.lots.filter((l) => l.item_id === capturedItemId);
      setLots(filtered);
      setAvailabilityPartial(outcome.kind === 'partial');
      setForbidden((f) => (f?.scope === 'availability' ? null : f));
    } catch {
      // mapWithConcurrency itself never throws; keep the block for
      // future robustness.
    }
  }, [item, warehouses, orgId, router]);

  // Activity fan-out (bounded 5) over the lots that reference
  // this item. Never over the full org's lots. Each per-lot
  // request explicitly requests `limit=100`; if any lot returns
  // a `next_cursor` the merged activity is surfaced as `partial`.
  const loadActivity = useCallback(async () => {
    if (!item || lots.length === 0) {
      setActivity([]);
      setActivityPartial(false);
      return;
    }
    const capturedOrgId = orgId;
    const capturedItemId = item.id;
    const gen = ++activityRef.current;
    const isCurrent = () =>
      activityRef.current === gen &&
      capturedOrgId === currentOrgIdRef.current &&
      capturedItemId === currentItemIdRef.current;
    setLoadingActivity(true);
    try {
      const settled = await mapWithConcurrency(lots, ACTIVITY_CONCURRENCY, (lot) =>
        apiFetch<TransactionPage>(
          `/v1/lots/${lot.id}/transactions?limit=${ACTIVITY_PER_LOT_LIMIT}`,
        ),
      );
      if (!isCurrent()) return;
      const outcome = inspectFanOut(settled, apiErrorStatus);
      if (outcome.kind === 'unauthenticated') {
        router.push('/login');
        return;
      }
      if (outcome.kind === 'forbidden') {
        activityRef.current += 1;
        setActivity([]);
        setForbidden({
          scope: 'activity',
          message: "You don't have permission to view activity for this item.",
        });
        return;
      }
      setActivity(outcome.transactions);
      setActivityPartial(outcome.kind === 'partial');
      setForbidden((f) => (f?.scope === 'activity' ? null : f));
    } finally {
      if (isCurrent()) setLoadingActivity(false);
    }
  }, [item, lots, orgId, router]);

  // Route identity is `orgId + itemId`. Reset every item-scoped
  // piece of state whenever either dimension changes, and bump
  // the generation refs so any in-flight fan-out from the
  // previous identity is neutralised the moment it lands.
  useEffect(() => {
    itemRef.current += 1;
    availabilityRef.current += 1;
    activityRef.current += 1;
    setItem(null);
    setWarehouses([]);
    setLots([]);
    setActivity([]);
    setActivityPartial(false);
    setAvailabilityPartial(false);
    setForbidden(null);
    setError(null);
    setEditing(false);
    setEditError(null);
    setEditBusy(false);
    setPendingActive(null);
    setStatusBusy(false);
    setLoading(true);
    setLoadingActivity(false);
    // Sprint 5.4: close any open stock-op dialog on identity change.
    setActiveStockOp(null);
    setReversalTx(null);
  }, [orgId, itemId]);

  useEffect(() => {
    void loadDetail();
  }, [loadDetail]);

  useEffect(() => {
    void loadAvailability();
  }, [loadAvailability]);

  useEffect(() => {
    void loadActivity();
  }, [loadActivity]);

  const availabilityRows = useMemo(() => {
    if (!item) return [];
    return buildItemAvailability({
      item,
      warehouses,
      lots,
      nowIso: new Date().toISOString(),
    });
  }, [item, warehouses, lots]);

  const warehousesById = useMemo(() => new Map(warehouses.map((w) => [w.id, w])), [warehouses]);

  const operationalWarehouses = useMemo(
    () => warehouses.filter((warehouse) => warehouse.status !== 'closed'),
    [warehouses],
  );

  const activeOrg = useMemo(() => orgs?.find((o) => o.id === orgId) ?? null, [orgs, orgId]);

  // ---- Edit + status ------------------------------------------------ //
  // Both mutations capture the full route identity (orgId + itemId)
  // and guard every state write against `sameRoute()` so a stale
  // completion from a previous item cannot patch a newly-loaded one.
  async function submitEdit(payload: ItemFormPayload) {
    if (payload.mode !== 'edit' || !item) return;
    const mutationOrgId = orgId;
    const mutationItemId = item.id;
    const sameRoute = () =>
      mutationOrgId === currentOrgIdRef.current && mutationItemId === currentItemIdRef.current;
    setEditBusy(true);
    setEditError(null);
    try {
      const updated = await apiFetch<InventoryItem>(`/v1/inventory-items/${mutationItemId}`, {
        method: 'PATCH',
        body: JSON.stringify({
          name: payload.name,
          description: payload.description,
          sku: payload.sku,
          is_active: payload.is_active,
        }),
      });
      if (!sameRoute()) return;
      setItem(updated);
      setEditing(false);
      toast('Item updated.', 'success');
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        router.push('/login');
        return;
      }
      if (!sameRoute()) return;
      if (err instanceof ApiError && err.status === 403) {
        setEditError("You don't have permission to edit this item.");
        return;
      }
      setEditError(friendlyError(err));
    } finally {
      if (sameRoute()) setEditBusy(false);
    }
  }

  async function confirmStatusChange() {
    if (!item || pendingActive === null) return;
    const mutationOrgId = orgId;
    const mutationItemId = item.id;
    const sameRoute = () =>
      mutationOrgId === currentOrgIdRef.current && mutationItemId === currentItemIdRef.current;
    setStatusBusy(true);
    try {
      const updated = await apiFetch<InventoryItem>(`/v1/inventory-items/${mutationItemId}`, {
        method: 'PATCH',
        body: JSON.stringify({ is_active: pendingActive }),
      });
      if (!sameRoute()) {
        setPendingActive(null);
        return;
      }
      setItem(updated);
      toast(pendingActive ? 'Item activated.' : 'Item deactivated.', 'success');
      setPendingActive(null);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        router.push('/login');
        return;
      }
      if (!sameRoute()) {
        setPendingActive(null);
        return;
      }
      if (err instanceof ApiError && err.status === 403) {
        toast("You don't have permission to change this item's status.", 'error');
        setPendingActive(null);
        return;
      }
      toast(friendlyError(err), 'error');
      setPendingActive(null);
    } finally {
      if (sameRoute()) setStatusBusy(false);
    }
  }

  // ---- Render ------------------------------------------------------- //
  if (forbidden?.scope === 'org') {
    return (
      <main className="mx-auto max-w-6xl px-6 py-10" data-testid="item-detail-page">
        <InventoryItemForbiddenBanner scope="org" message={forbidden.message} />
      </main>
    );
  }

  return (
    <main className="mx-auto max-w-6xl px-6 py-10" data-testid="item-detail-page">
      {loading ? (
        <div data-testid="item-detail-loading">
          <SkeletonRows rows={8} />
        </div>
      ) : forbidden?.scope === 'item' ? (
        <>
          <div className="mb-4">
            <Link
              href={
                orgId
                  ? `/inventory/items?organization_id=${encodeURIComponent(orgId)}`
                  : '/inventory/items'
              }
              data-testid="item-detail-back"
              className="text-xs uppercase tracking-widest text-muted-foreground hover:text-foreground"
            >
              ← All items
            </Link>
          </div>
          <InventoryItemForbiddenBanner scope="item" message={forbidden.message} />
        </>
      ) : item ? (
        <>
          <InventoryItemHeader
            item={item}
            organization={activeOrg}
            onEdit={() => {
              setEditing((v) => !v);
              setEditError(null);
            }}
            onToggleActive={(next) => setPendingActive(next)}
            editDisabled={editBusy}
          />
          {/* Sprint 5.4: primary stock operation entry points. Disabled
              when the item is inactive — backend authorization is still
              authoritative but this avoids the obvious deadend. */}
          <div className="mb-4 flex flex-wrap gap-2" data-testid="item-detail-stock-actions">
            {(['receive', 'issue', 'transfer', 'adjust'] as const).map((op) => (
              <button
                key={op}
                type="button"
                data-testid={`item-detail-stock-${op}`}
                onClick={() => {
                  setReversalTx(null);
                  setActiveStockOp(op);
                }}
                disabled={!item.is_active}
                className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-secondary disabled:opacity-60"
              >
                {op.charAt(0).toUpperCase() + op.slice(1)}
              </button>
            ))}
          </div>
          {error && (
            <div className="mb-4">
              <ErrorBanner message={error} />
            </div>
          )}
          {editing && (
            <div className="mb-6">
              <InventoryItemForm
                mode="edit"
                item={item}
                organizationName={activeOrg?.name ?? null}
                busy={editBusy}
                errorMessage={editError}
                onCancel={() => {
                  setEditing(false);
                  setEditError(null);
                }}
                onSubmit={submitEdit}
              />
            </div>
          )}
          <div className="grid gap-6 lg:grid-cols-3">
            <div className="lg:col-span-2 space-y-6">
              <InventoryItemSummary item={item} organization={activeOrg} />
              {forbidden?.scope === 'availability' ? (
                <InventoryItemForbiddenBanner scope="availability" message={forbidden.message} />
              ) : (
                <InventoryItemAvailabilityTable
                  rows={availabilityRows}
                  partial={availabilityPartial}
                  onOpenWarehouse={(warehouseId) => {
                    if (!orgId) return;
                    router.push(
                      `/inventory?organization_id=${encodeURIComponent(orgId)}&warehouse_id=${encodeURIComponent(warehouseId)}&tab=lots`,
                    );
                  }}
                />
              )}
              <InventoryItemLotsTable
                lots={lots}
                warehousesById={warehousesById}
                organizationId={orgId || null}
                onOpenHistory={({ lot }) => {
                  if (!orgId) return;
                  router.push(
                    `/inventory?organization_id=${encodeURIComponent(orgId)}&warehouse_id=${encodeURIComponent(lot.warehouse_id)}&lot_id=${encodeURIComponent(lot.id)}&tab=history`,
                  );
                }}
              />
              {forbidden?.scope === 'activity' ? (
                <InventoryItemForbiddenBanner scope="activity" message={forbidden.message} />
              ) : loadingActivity ? (
                <div data-testid="item-activity-loading">
                  <SkeletonRows rows={4} />
                </div>
              ) : (
                <InventoryItemActivity
                  transactions={activity}
                  partial={activityPartial}
                  onReverse={(tx) => {
                    setReversalTx(tx);
                    setActiveStockOp('reverse');
                  }}
                />
              )}
            </div>
            <div className="space-y-6">
              <InventoryItemQuickActions item={item} organizationId={orgId || null} />
            </div>
          </div>
          <ConfirmDialog
            open={pendingActive !== null}
            busy={statusBusy}
            destructive={pendingActive === false}
            testId="item-detail-status-confirm"
            title={pendingActive === false ? 'Deactivate item?' : 'Activate item?'}
            description={
              pendingActive === false
                ? 'Operators will not be able to receive, issue or transfer this item until it is reactivated.'
                : 'Operators will regain the ability to receive, issue and transfer this item.'
            }
            confirmLabel={pendingActive === false ? 'Deactivate' : 'Activate'}
            onConfirm={confirmStatusChange}
            onCancel={() => setPendingActive(null)}
          />
          {/* Sprint 5.4: unified stock operation dialog. Uses the
              same route-identity/generation guards as the parent
              page — the dialog's own guard fires on close, and
              `onSuccess` triggers a bounded refresh of every
              affected slice (item, availability, activity). */}
          {activeStockOp && orgId && (
            <StockOperationDialog
              open
              type={activeStockOp}
              organizationId={orgId}
              item={item}
              warehouses={operationalWarehouses}
              lots={lots}
              reversalTx={
                activeStockOp === 'reverse' && reversalTx
                  ? {
                      ...reversalTx,
                      // The activity API returns the owning warehouse
                      // on each row; we defensively fall back to the
                      // owning lot if not present.
                      warehouse_id:
                        (reversalTx as ItemLedgerTx & { warehouse_id?: string }).warehouse_id ??
                        lots.find((l) => l.id === (reversalTx as { lot_id?: string }).lot_id)
                          ?.warehouse_id,
                    }
                  : undefined
              }
              onClose={() => {
                setActiveStockOp(null);
                setReversalTx(null);
              }}
              onSuccess={async () => {
                // Refresh the two slices that a ledger operation can
                // change: warehouse availability (lots) and activity.
                // The item itself is not mutated by any ledger op,
                // so we don't touch loadDetail — reloading it in
                // parallel with loadAvailability would cross the
                // shared availability generation ref and produce
                // spurious loading flickers.
                await Promise.all([loadAvailability(), loadActivity()]);
              }}
              onUnauthenticated={() => router.push('/login')}
            />
          )}
        </>
      ) : (
        <div data-testid="item-detail-not-found">
          {error ? <ErrorBanner message={error} /> : null}
        </div>
      )}
    </main>
  );
}
