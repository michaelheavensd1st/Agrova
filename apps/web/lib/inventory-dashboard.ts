/**
 * Sprint 5.1 — Inventory Dashboard types and pure aggregation helpers.
 *
 * The dashboard is a read-only projection over the existing Sprint 4
 * inventory endpoints. It performs NO writes and MUST NOT duplicate
 * business logic that already lives in the backend inventory engine.
 *
 * Every metric in this module is derived from data returned by:
 *   - GET /v1/organizations/{org}/warehouses
 *   - GET /v1/organizations/{org}/inventory-items
 *   - GET /v1/warehouses/{wh}/lots      (returns balance in canonical unit)
 *
 * Anything the backend does not currently expose (per-item
 * reorder_level, estimated stock value, warehouse utilization,
 * pending / in-transit transfer states, cross-warehouse recent
 * activity) is DELIBERATELY omitted. See
 * docs/sprint_5/API_MAPPING.md for the full gap list.
 *
 * Sprint 5.1 review update: the previous `recent_activity` list
 * (ordered by `lot.updated_at`) was removed. Backend tracing
 * confirmed that receipts, issues, transfers, adjustments and
 * reversals do NOT update the parent lot's timestamp, so ordering
 * by `updated_at` was misleading. Cross-warehouse recent activity
 * remains explicitly deferred to a future sprint that ships a
 * dedicated transaction-feed endpoint.
 */

import type { UUID } from '@/lib/types';

// --------------------------------------------------------------------- //
// Types mirroring the FastAPI response models we consume.
// --------------------------------------------------------------------- //

export type InventoryItemCategory = 'feed' | 'medicine' | 'chemical' | 'supply';

export type WarehouseStatus = 'active' | 'closed' | 'maintenance';

export interface DashboardOrganization {
  id: UUID;
  name: string;
  slug: string;
}

export interface DashboardWarehouse {
  id: UUID;
  code: string;
  name: string;
  status: WarehouseStatus;
  farm_id: UUID | null;
  organization_id: UUID;
}

export interface DashboardInventoryItem {
  id: UUID;
  code: string;
  name: string;
  category: InventoryItemCategory;
  canonical_unit: string;
  is_active: boolean;
}

/**
 * Mirrors `InventoryLotWithBalance` on the backend. `balance` is
 * returned as a JSON number from FastAPI's Decimal serializer.
 */
export interface DashboardLot {
  id: UUID;
  item_id: UUID;
  warehouse_id: UUID;
  storage_location_id: UUID | null;
  lot_code: string;
  expiry_date: string | null; // ISO date, no time
  balance: number | string; // arrives as string from Decimal serializer
  balance_unit: string;
  updated_at: string; // ISO datetime — NOT used as an activity proxy.
  created_at: string;
}

// --------------------------------------------------------------------- //
// Derived shapes surfaced to the UI. These are frontend-only helpers
// — the raw shapes above remain the authoritative API contract.
// --------------------------------------------------------------------- //

/** Categorised, humanised stock status for a single lot. */
export type LotStockStatus = 'out_of_stock' | 'expiring_soon' | 'expired' | 'ok';

export interface LotAttentionRow {
  lot_id: UUID;
  item_name: string;
  item_category: InventoryItemCategory | 'unknown';
  warehouse_name: string;
  lot_code: string;
  balance: number;
  balance_unit: string;
  expiry_date: string | null;
  status: LotStockStatus;
  /** Whole days remaining until expiry, or negative if already expired. */
  days_until_expiry: number | null;
}

export interface DashboardSummary {
  total_active_items: number;
  total_warehouses: number;
  total_active_warehouses: number;
  total_lots: number;
  out_of_stock_lots: number;
  expiring_soon_lots: number;
  expired_lots: number;
}

/** Structure returned by the aggregator, ready for rendering. */
export interface DashboardProjection {
  summary: DashboardSummary;
  attention: LotAttentionRow[];
}

// --------------------------------------------------------------------- //
// Pure aggregation helpers — no I/O, easy to unit-test.
// --------------------------------------------------------------------- //

/** Configurable expiry-window (calendar days). Kept as a constant so
 * every screen — and every test — sees the same value. */
export const EXPIRING_SOON_DAYS = 30;

/** Cap on the number of rows surfaced in the attention list. */
export const ATTENTION_LIST_LIMIT = 20;

/**
 * Release 6.0.6 production receipt-acceptance fixture.
 *
 * Its immutable receipt and ledger history must remain available through
 * administrative/history views, but its synthetic stock must not enter the
 * operational dashboard projection. Match the documented immutable UUID so
 * legitimate closed warehouses (including similarly named ones) remain
 * visible in reporting.
 */
export const QUARANTINED_RECEIPT_FIXTURE_WAREHOUSE_ID = '94e15351-d4b4-46bc-ac36-a304c675ba8f';

export function isQuarantinedReceiptFixtureWarehouse(warehouse: { id: string }): boolean {
  return warehouse.id === QUARANTINED_RECEIPT_FIXTURE_WAREHOUSE_ID;
}

/** Parse the JSON balance (arrives as string from FastAPI Decimal). */
export function parseBalance(raw: number | string): number {
  const n = typeof raw === 'string' ? Number(raw) : raw;
  return Number.isFinite(n) ? n : 0;
}

/**
 * Choose the effective organization for a request-scoped operation:
 *   - if `requestedId` is present AND belongs to `orgs`, use it;
 *   - otherwise fall back to the first `orgs` entry;
 *   - return `null` when the caller has no organizations at all.
 *
 * Never trust a query parameter without validating it against the
 * authenticated user's own list of organizations.
 */
export function resolveOrganizationId(
  requestedId: string | null | undefined,
  orgs: readonly { id: string }[],
): string | null {
  if (orgs.length === 0) return null;
  if (requestedId && orgs.some((o) => o.id === requestedId)) return requestedId;
  return orgs[0].id;
}

/**
 * Sprint 5.1 review round #2 — Inventory workspace organization
 * context retention guards.
 *
 * These helpers exist so the same "does this thing belong to the
 * current org?" question has a single, testable answer everywhere
 * a write is about to happen. They intentionally do NOT hit the
 * backend — they are last-line-of-defence UI checks that catch
 * stale selections after an organization switch. The backend
 * remains the ultimate authority.
 */

export function isWarehouseInCurrentOrg(
  warehouseId: string | null | undefined,
  warehouses: readonly { id: string }[],
): boolean {
  if (!warehouseId) return false;
  return warehouses.some((w) => w.id === warehouseId);
}

export function isItemInCurrentOrg(
  itemId: string | null | undefined,
  items: readonly { id: string }[],
): boolean {
  if (!itemId) return false;
  return items.some((i) => i.id === itemId);
}

export function isLotInCurrentOrg(
  lotId: string | null | undefined,
  lots: readonly { id: string; item_id: string; warehouse_id: string }[],
  warehouses: readonly { id: string }[],
  items: readonly { id: string }[],
): boolean {
  if (!lotId) return false;
  const lot = lots.find((l) => l.id === lotId);
  if (!lot) return false;
  return (
    isWarehouseInCurrentOrg(lot.warehouse_id, warehouses) && isItemInCurrentOrg(lot.item_id, items)
  );
}

/** Whole calendar days between two ISO timestamps (UTC day boundaries).
 *
 * Uses UTC day-floor semantics so a lot expiring `2026-02-15T00:00:00Z`
 * compared against a "now" of `2026-02-15T12:00:00Z` is treated as
 * expiring today (0), not "1 day ago". */
export function daysBetween(fromIso: string, referenceIso: string): number {
  const from = new Date(fromIso);
  const ref = new Date(referenceIso);
  const fromDay = Date.UTC(from.getUTCFullYear(), from.getUTCMonth(), from.getUTCDate());
  const refDay = Date.UTC(ref.getUTCFullYear(), ref.getUTCMonth(), ref.getUTCDate());
  return Math.floor((fromDay - refDay) / (1000 * 60 * 60 * 24));
}

/** Classify a single lot into a rendering status. */
export function classifyLot(
  lot: DashboardLot,
  nowIso: string,
  expiringSoonDays = EXPIRING_SOON_DAYS,
): LotStockStatus {
  const balance = parseBalance(lot.balance);
  if (balance <= 0) return 'out_of_stock';
  if (lot.expiry_date) {
    const daysLeft = daysBetween(lot.expiry_date, nowIso);
    if (daysLeft < 0) return 'expired';
    if (daysLeft <= expiringSoonDays) return 'expiring_soon';
  }
  return 'ok';
}

/**
 * Aggregate the three raw API result sets into the dashboard projection.
 *
 * @param nowIso a wall-clock ISO timestamp — passed in so the caller
 *   (and tests) can freeze time deterministically.
 */
export function buildDashboardProjection(input: {
  warehouses: DashboardWarehouse[];
  items: DashboardInventoryItem[];
  lots: DashboardLot[];
  nowIso: string;
}): DashboardProjection {
  const { warehouses, items, lots, nowIso } = input;

  const itemsById = new Map(items.map((i) => [i.id, i]));
  const warehousesById = new Map(warehouses.map((w) => [w.id, w]));

  let outOfStock = 0;
  let expiringSoon = 0;
  let expired = 0;

  const attentionRows: LotAttentionRow[] = [];

  for (const lot of lots) {
    const item = itemsById.get(lot.item_id);
    const warehouse = warehousesById.get(lot.warehouse_id);
    const item_name = item?.name ?? 'Unknown item';
    const item_category = item?.category ?? 'unknown';
    const warehouse_name = warehouse?.name ?? 'Unknown warehouse';
    const balance = parseBalance(lot.balance);
    const status = classifyLot(lot, nowIso);
    const days_until_expiry = lot.expiry_date ? daysBetween(lot.expiry_date, nowIso) : null;

    if (status === 'out_of_stock') outOfStock += 1;
    if (status === 'expiring_soon') expiringSoon += 1;
    if (status === 'expired') expired += 1;

    if (status !== 'ok') {
      attentionRows.push({
        lot_id: lot.id,
        item_name,
        item_category,
        warehouse_name,
        lot_code: lot.lot_code,
        balance,
        balance_unit: lot.balance_unit,
        expiry_date: lot.expiry_date,
        status,
        days_until_expiry,
      });
    }
  }

  // Sort attention: out-of-stock first, then expired, then expiring-soon
  // (nearest to expiry first).
  const STATUS_ORDER: Record<LotStockStatus, number> = {
    out_of_stock: 0,
    expired: 1,
    expiring_soon: 2,
    ok: 3,
  };
  attentionRows.sort((a, b) => {
    const s = STATUS_ORDER[a.status] - STATUS_ORDER[b.status];
    if (s !== 0) return s;
    // Within the same status, oldest expiry (or lowest balance) first.
    if (a.status === 'out_of_stock' && b.status === 'out_of_stock') {
      return a.balance - b.balance;
    }
    return (a.days_until_expiry ?? 0) - (b.days_until_expiry ?? 0);
  });

  const summary: DashboardSummary = {
    total_active_items: items.filter((i) => i.is_active).length,
    total_warehouses: warehouses.length,
    total_active_warehouses: warehouses.filter((w) => w.status === 'active').length,
    total_lots: lots.length,
    out_of_stock_lots: outOfStock,
    expiring_soon_lots: expiringSoon,
    expired_lots: expired,
  };

  return {
    summary,
    attention: attentionRows.slice(0, ATTENTION_LIST_LIMIT),
  };
}
