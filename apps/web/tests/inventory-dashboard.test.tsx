/**
 * Sprint 5.1 — Inventory Dashboard tests (post-review-round).
 *
 * Layers:
 *   1. Pure aggregation unit tests over `buildDashboardProjection`.
 *   2. Per-component render tests.
 *   3. Page-level integration tests with a mocked `apiFetch`:
 *      · loading / empty / populated
 *      · organization preservation across quick-action + workspace links
 *      · stale-response guard when the user switches organization
 *      · 401 / 403 propagation from the lot fan-out
 *      · 401 / 403 during the organization bootstrap
 *      · non-auth partial fan-out → "understated totals" warning
 *      · the deferred activity placeholder (no ranked list anywhere)
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';

import {
  ATTENTION_LIST_LIMIT,
  buildDashboardProjection,
  classifyLot,
  daysBetween,
  EXPIRING_SOON_DAYS,
  isItemInCurrentOrg,
  isLotInCurrentOrg,
  QUARANTINED_RECEIPT_FIXTURE_WAREHOUSE_ID,
  isWarehouseInCurrentOrg,
  parseBalance,
  resolveOrganizationId,
  type DashboardInventoryItem,
  type DashboardLot,
  type DashboardWarehouse,
} from '@/lib/inventory-dashboard';
import { InventoryDashboardSummaryCards } from '@/components/inventory-dashboard/summary-cards';
import { InventoryDashboardAttentionPanel } from '@/components/inventory-dashboard/attention-panel';
import { InventoryDashboardActivityPlaceholder } from '@/components/inventory-dashboard/activity-placeholder';
import {
  InventoryDashboardQuickActions,
  buildWorkspaceHref,
} from '@/components/inventory-dashboard/quick-actions';

// --------------------------------------------------------------------- //
// Fixtures
// --------------------------------------------------------------------- //

const NOW = '2026-02-15T12:00:00.000Z';
const CANONICAL_RELEASE_606_RECEIPT_FIXTURE_WAREHOUSE_ID = '94e15351-d4b4-46bc-ac36-a304c675ba8f';

const WH_MAIN: DashboardWarehouse = {
  id: 'wh-1',
  code: 'MAIN',
  name: 'Main store',
  status: 'active',
  farm_id: null,
  organization_id: 'org-1',
};
const WH_COLD: DashboardWarehouse = {
  id: 'wh-2',
  code: 'COLD',
  name: 'Cold room',
  status: 'active',
  farm_id: null,
  organization_id: 'org-1',
};
const WH_CLOSED: DashboardWarehouse = {
  id: 'wh-3',
  code: 'ARCH',
  name: 'Archived',
  status: 'closed',
  farm_id: null,
  organization_id: 'org-1',
};

const ITEM_FEED: DashboardInventoryItem = {
  id: 'item-1',
  code: 'F-STARTER',
  name: 'Starter feed',
  category: 'feed',
  canonical_unit: 'kg',
  is_active: true,
};
const ITEM_MED: DashboardInventoryItem = {
  id: 'item-2',
  code: 'M-VACC',
  name: 'Vaccine A',
  category: 'medicine',
  canonical_unit: 'mL',
  is_active: true,
};
const ITEM_INACTIVE: DashboardInventoryItem = {
  id: 'item-3',
  code: 'S-BAG',
  name: 'Old bags',
  category: 'supply',
  canonical_unit: 'count',
  is_active: false,
};

function makeLot(overrides: Partial<DashboardLot>): DashboardLot {
  return {
    id: 'lot-x',
    item_id: 'item-1',
    warehouse_id: 'wh-1',
    storage_location_id: null,
    lot_code: 'L-DEFAULT',
    expiry_date: null,
    balance: '100',
    balance_unit: 'kg',
    updated_at: '2026-02-14T10:00:00.000Z',
    created_at: '2026-02-01T10:00:00.000Z',
    ...overrides,
  };
}

// --------------------------------------------------------------------- //
// Pure aggregation unit tests
// --------------------------------------------------------------------- //

describe('inventory-dashboard aggregation', () => {
  it('anchors the quarantine to the canonical Release 6.0.6 fixture warehouse', () => {
    expect(QUARANTINED_RECEIPT_FIXTURE_WAREHOUSE_ID).toBe(
      CANONICAL_RELEASE_606_RECEIPT_FIXTURE_WAREHOUSE_ID,
    );
  });

  it('parseBalance handles strings, numbers and garbage', () => {
    expect(parseBalance('12.5')).toBe(12.5);
    expect(parseBalance(0)).toBe(0);
    expect(parseBalance('not-a-number')).toBe(0);
  });

  it('daysBetween computes whole calendar-day differences', () => {
    expect(daysBetween('2026-02-15T00:00:00.000Z', NOW)).toBe(0);
    expect(daysBetween('2026-03-17T12:00:00.000Z', NOW)).toBe(30);
    expect(daysBetween('2026-02-14T12:00:00.000Z', NOW)).toBe(-1);
  });

  it('classifyLot marks out-of-stock lots regardless of expiry', () => {
    expect(classifyLot(makeLot({ balance: '0' }), NOW)).toBe('out_of_stock');
    expect(classifyLot(makeLot({ balance: '0', expiry_date: '2027-01-01' }), NOW)).toBe(
      'out_of_stock',
    );
  });

  it('classifyLot marks expired vs expiring_soon based on days_until_expiry', () => {
    expect(classifyLot(makeLot({ balance: '10', expiry_date: '2026-02-01' }), NOW)).toBe('expired');
    expect(classifyLot(makeLot({ balance: '10', expiry_date: '2026-02-20' }), NOW)).toBe(
      'expiring_soon',
    );
    expect(classifyLot(makeLot({ balance: '10', expiry_date: '2027-01-01' }), NOW)).toBe('ok');
  });

  it('buildDashboardProjection produces expected summary + attention rows', () => {
    const projection = buildDashboardProjection({
      warehouses: [WH_MAIN, WH_COLD, WH_CLOSED],
      items: [ITEM_FEED, ITEM_MED, ITEM_INACTIVE],
      lots: [
        makeLot({ id: 'lot-ok', item_id: 'item-1', balance: '50' }),
        makeLot({ id: 'lot-out', item_id: 'item-1', balance: '0' }),
        makeLot({
          id: 'lot-expired',
          item_id: 'item-2',
          warehouse_id: 'wh-2',
          balance: '25',
          balance_unit: 'mL',
          expiry_date: '2026-01-31',
        }),
        makeLot({
          id: 'lot-soon',
          item_id: 'item-2',
          warehouse_id: 'wh-2',
          balance: '10',
          balance_unit: 'mL',
          expiry_date: '2026-02-25',
        }),
      ],
      nowIso: NOW,
    });

    expect(projection.summary).toEqual({
      total_active_items: 2,
      total_warehouses: 3,
      total_active_warehouses: 2,
      total_lots: 4,
      out_of_stock_lots: 1,
      expiring_soon_lots: 1,
      expired_lots: 1,
    });

    expect(projection.attention).toHaveLength(3);
    expect(projection.attention[0].status).toBe('out_of_stock');
    expect(projection.attention[1].status).toBe('expired');
    expect(projection.attention[2].status).toBe('expiring_soon');
  });

  it('buildDashboardProjection returns healthy empty projection when nothing to show', () => {
    const projection = buildDashboardProjection({
      warehouses: [],
      items: [],
      lots: [],
      nowIso: NOW,
    });
    expect(projection.summary.total_lots).toBe(0);
    expect(projection.attention).toEqual([]);
    // Sprint 5.1 review fix — recent_activity is intentionally removed.
    expect((projection as unknown as Record<string, unknown>).recent_activity).toBeUndefined();
  });

  it('attention list is capped by ATTENTION_LIST_LIMIT', () => {
    const many: DashboardLot[] = Array.from({ length: ATTENTION_LIST_LIMIT + 5 }, (_, i) =>
      makeLot({ id: `lot-${i}`, lot_code: `L-${i}`, balance: '0' }),
    );
    const projection = buildDashboardProjection({
      warehouses: [WH_MAIN],
      items: [ITEM_FEED],
      lots: many,
      nowIso: NOW,
    });
    expect(projection.summary.out_of_stock_lots).toBe(ATTENTION_LIST_LIMIT + 5);
    expect(projection.attention).toHaveLength(ATTENTION_LIST_LIMIT);
  });

  it('EXPIRING_SOON_DAYS is a stable public constant', () => {
    expect(EXPIRING_SOON_DAYS).toBe(30);
  });
});

// --------------------------------------------------------------------- //
// buildWorkspaceHref helper
// --------------------------------------------------------------------- //

describe('buildWorkspaceHref', () => {
  it('emits /inventory when both args are null', () => {
    expect(buildWorkspaceHref(null, null)).toBe('/inventory');
  });
  it('emits an org-only href when tab is null', () => {
    expect(buildWorkspaceHref('org-1', null)).toBe('/inventory?organization_id=org-1');
  });
  it('emits an org + tab href', () => {
    expect(buildWorkspaceHref('org-1', 'receive')).toBe(
      '/inventory?organization_id=org-1&tab=receive',
    );
  });
  it('URL-encodes ids that contain query-sensitive characters', () => {
    expect(buildWorkspaceHref('org-1&drop', 'items')).toBe(
      '/inventory?organization_id=org-1%26drop&tab=items',
    );
  });
});

describe('resolveOrganizationId', () => {
  const orgs = [
    { id: 'org-1', name: 'Aegis' },
    { id: 'org-2', name: 'Delta' },
  ];
  it('accepts a valid requested org', () => {
    expect(resolveOrganizationId('org-2', orgs)).toBe('org-2');
  });
  it('falls back to the first org when the requested id is unknown', () => {
    expect(resolveOrganizationId('spoofed-org', orgs)).toBe('org-1');
  });
  it('falls back to the first org when no requested id is provided', () => {
    expect(resolveOrganizationId(null, orgs)).toBe('org-1');
    expect(resolveOrganizationId(undefined, orgs)).toBe('org-1');
    expect(resolveOrganizationId('', orgs)).toBe('org-1');
  });
  it('returns null when the caller has no organizations at all', () => {
    expect(resolveOrganizationId('anything', [])).toBeNull();
  });
});

describe('inventory workspace cross-org guards', () => {
  const orgAWarehouses = [{ id: 'wh-A1' }, { id: 'wh-A2' }];
  const orgAItems = [{ id: 'item-A1' }, { id: 'item-A2' }];
  const orgALots = [
    { id: 'lot-A', item_id: 'item-A1', warehouse_id: 'wh-A1' },
    { id: 'lot-cross', item_id: 'item-A1', warehouse_id: 'wh-Z' }, // corrupted
  ];

  it('isWarehouseInCurrentOrg rejects unknown / null / empty ids', () => {
    expect(isWarehouseInCurrentOrg('wh-A1', orgAWarehouses)).toBe(true);
    expect(isWarehouseInCurrentOrg('wh-B1', orgAWarehouses)).toBe(false);
    expect(isWarehouseInCurrentOrg(null, orgAWarehouses)).toBe(false);
    expect(isWarehouseInCurrentOrg('', orgAWarehouses)).toBe(false);
    expect(isWarehouseInCurrentOrg('wh-A1', [])).toBe(false);
  });

  it('isItemInCurrentOrg rejects unknown / null / empty ids', () => {
    expect(isItemInCurrentOrg('item-A1', orgAItems)).toBe(true);
    expect(isItemInCurrentOrg('item-B1', orgAItems)).toBe(false);
    expect(isItemInCurrentOrg(null, orgAItems)).toBe(false);
    expect(isItemInCurrentOrg('', orgAItems)).toBe(false);
    expect(isItemInCurrentOrg('item-A1', [])).toBe(false);
  });

  it('isLotInCurrentOrg requires the lot AND its warehouse AND its item to all be in-org', () => {
    // Happy path.
    expect(isLotInCurrentOrg('lot-A', orgALots, orgAWarehouses, orgAItems)).toBe(true);
    // Lot references a warehouse not in this org → cross-tenant.
    expect(isLotInCurrentOrg('lot-cross', orgALots, orgAWarehouses, orgAItems)).toBe(false);
    // Unknown lot id.
    expect(isLotInCurrentOrg('lot-missing', orgALots, orgAWarehouses, orgAItems)).toBe(false);
    // Null / empty inputs.
    expect(isLotInCurrentOrg(null, orgALots, orgAWarehouses, orgAItems)).toBe(false);
    expect(isLotInCurrentOrg('', orgALots, orgAWarehouses, orgAItems)).toBe(false);
    // Empty support collections.
    expect(isLotInCurrentOrg('lot-A', [], orgAWarehouses, orgAItems)).toBe(false);
    expect(isLotInCurrentOrg('lot-A', orgALots, [], orgAItems)).toBe(false);
    expect(isLotInCurrentOrg('lot-A', orgALots, orgAWarehouses, [])).toBe(false);
  });
});

// --------------------------------------------------------------------- //
// Component render tests
// --------------------------------------------------------------------- //

describe('InventoryDashboardSummaryCards', () => {
  it('renders every metric with a testable value node', () => {
    render(
      <InventoryDashboardSummaryCards
        summary={{
          total_active_items: 7,
          total_warehouses: 2,
          total_active_warehouses: 2,
          total_lots: 14,
          out_of_stock_lots: 1,
          expiring_soon_lots: 3,
          expired_lots: 0,
        }}
      />,
    );
    expect(
      screen.getByTestId('inventory-dashboard-metric-total_active_items-value'),
    ).toHaveTextContent('7');
    expect(
      screen.getByTestId('inventory-dashboard-metric-out_of_stock_lots-value'),
    ).toHaveTextContent('1');
    expect(
      screen.getByTestId('inventory-dashboard-metric-expiring_soon_lots-value'),
    ).toHaveTextContent('3');
  });
});

describe('InventoryDashboardAttentionPanel', () => {
  it('shows the empty state when there are no attention rows', () => {
    render(<InventoryDashboardAttentionPanel rows={[]} />);
    expect(screen.getByTestId('inventory-dashboard-attention')).toBeInTheDocument();
    expect(screen.getByText(/Everything looks healthy/i)).toBeInTheDocument();
    expect(screen.queryByTestId('inventory-dashboard-attention-table')).not.toBeInTheDocument();
  });

  it('renders a table row per attention lot with the correct status label', () => {
    render(
      <InventoryDashboardAttentionPanel
        rows={[
          {
            lot_id: 'lot-out',
            item_name: 'Starter feed',
            item_category: 'feed',
            warehouse_name: 'Main store',
            lot_code: 'L-1',
            balance: 0,
            balance_unit: 'kg',
            expiry_date: null,
            status: 'out_of_stock',
            days_until_expiry: null,
          },
          {
            lot_id: 'lot-soon',
            item_name: 'Vaccine A',
            item_category: 'medicine',
            warehouse_name: 'Cold room',
            lot_code: 'L-2',
            balance: 5,
            balance_unit: 'mL',
            expiry_date: '2026-02-25',
            status: 'expiring_soon',
            days_until_expiry: 10,
          },
        ]}
      />,
    );
    expect(screen.getByTestId('inventory-dashboard-attention-status-lot-out')).toHaveTextContent(
      'Out of stock',
    );
    expect(screen.getByTestId('inventory-dashboard-attention-status-lot-soon')).toHaveTextContent(
      'Expiring soon',
    );
    expect(screen.getByTestId('inventory-dashboard-attention-count')).toHaveTextContent('2 lots');
  });
});

describe('InventoryDashboardActivityPlaceholder', () => {
  it('renders the deferred copy and history link (with org)', () => {
    render(<InventoryDashboardActivityPlaceholder organizationId="org-1" />);
    expect(screen.getByTestId('inventory-dashboard-activity-placeholder')).toBeInTheDocument();
    expect(
      screen.getByText(/A cross-warehouse transaction feed is not yet available/i),
    ).toBeInTheDocument();
    expect(screen.getByTestId('inventory-dashboard-activity-history-link')).toHaveAttribute(
      'href',
      '/inventory?organization_id=org-1&tab=history',
    );
  });

  it('falls back to a plain history link when no org is selected yet', () => {
    render(<InventoryDashboardActivityPlaceholder organizationId={null} />);
    expect(screen.getByTestId('inventory-dashboard-activity-history-link')).toHaveAttribute(
      'href',
      '/inventory?tab=history',
    );
  });
});

describe('InventoryDashboardQuickActions', () => {
  it('preserves the organization on every functional action', () => {
    render(<InventoryDashboardQuickActions organizationId="org-42" />);
    const cases: Array<[string, string]> = [
      ['view-items', '/inventory?organization_id=org-42&tab=items'],
      ['view-warehouses', '/inventory?organization_id=org-42&tab=warehouses'],
      ['receive-stock', '/inventory?organization_id=org-42&tab=receive'],
      ['issue-stock', '/inventory?organization_id=org-42&tab=issue'],
      ['transfer-stock', '/inventory?organization_id=org-42&tab=transfer'],
      ['transaction-history', '/inventory?organization_id=org-42&tab=history'],
    ];
    for (const [key, href] of cases) {
      const el = screen.getByTestId(`inventory-dashboard-action-${key}`);
      expect(el.tagName).toBe('A');
      expect(el).toHaveAttribute('href', href);
    }
    // Deferred actions remain non-interactive with the coming-later badge.
    const suppliers = screen.getByTestId('inventory-dashboard-action-suppliers');
    expect(suppliers.tagName).toBe('DIV');
    expect(suppliers).toHaveAttribute('aria-disabled', 'true');
    expect(within(suppliers).getByText(/Coming later in Sprint 5/i)).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------- //
// Page-level rendering with mocked apiFetch
// --------------------------------------------------------------------- //

const { routerPush, stableRouter } = vi.hoisted(() => {
  const push = vi.fn();
  return {
    routerPush: push,
    stableRouter: { push, replace: push, back: vi.fn() },
  };
});
vi.mock('next/navigation', () => ({
  useRouter: () => stableRouter,
}));

vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api');
  return {
    ...actual,
    apiFetch: vi.fn(),
  };
});

import { apiFetch, ApiError } from '@/lib/api';
import InventoryDashboardPage from '@/app/inventory/dashboard/page';

const mockedApiFetch = apiFetch as unknown as ReturnType<typeof vi.fn>;

type LotSource = DashboardLot[] | ApiError;

function primeApi(config: {
  orgs?: unknown;
  warehousesByOrg?: Record<string, unknown>;
  itemsByOrg?: Record<string, unknown>;
  lotsByWh?: Record<string, LotSource>;
  throwOnOrgs?: ApiError;
}) {
  mockedApiFetch.mockImplementation((path: string) => {
    if (path === '/v1/organizations') {
      if (config.throwOnOrgs) return Promise.reject(config.throwOnOrgs);
      return Promise.resolve(config.orgs ?? []);
    }
    const whMatch = path.match(/^\/v1\/organizations\/([^/]+)\/warehouses$/);
    if (whMatch) {
      return Promise.resolve(config.warehousesByOrg?.[whMatch[1]] ?? []);
    }
    const itemMatch = path.match(/^\/v1\/organizations\/([^/]+)\/inventory-items$/);
    if (itemMatch) {
      return Promise.resolve(config.itemsByOrg?.[itemMatch[1]] ?? []);
    }
    const lotsMatch = path.match(/^\/v1\/warehouses\/([^/]+)\/lots$/);
    if (lotsMatch) {
      const src = config.lotsByWh?.[lotsMatch[1]];
      if (src instanceof ApiError) return Promise.reject(src);
      return Promise.resolve(src ?? []);
    }
    return Promise.reject(new ApiError(404, { detail: `unmocked ${path}` }));
  });
}

function setLocationSearch(search: string) {
  // jsdom does allow overriding window.location.search via pushState.
  window.history.replaceState({}, '', `/inventory/dashboard${search}`);
}

describe('InventoryDashboardPage', () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
    routerPush.mockReset();
    setLocationSearch('');
  });
  afterEach(() => {
    vi.clearAllMocks();
  });

  it('shows loading state initially', async () => {
    primeApi({ orgs: [{ id: 'org-1', name: 'Aegis', slug: 'aegis' }] });
    render(<InventoryDashboardPage />);
    expect(await screen.findByTestId('ape-loading')).toBeInTheDocument();
  });

  it('renders the empty state with an org-scoped CTA when the org has no data', async () => {
    primeApi({
      orgs: [{ id: 'org-1', name: 'Aegis', slug: 'aegis' }],
      warehousesByOrg: { 'org-1': [] },
      itemsByOrg: { 'org-1': [] },
    });
    render(<InventoryDashboardPage />);
    await waitFor(() =>
      expect(screen.getByTestId('inventory-dashboard-empty')).toBeInTheDocument(),
    );
    expect(screen.getByTestId('inventory-dashboard-empty-cta')).toHaveAttribute(
      'href',
      '/inventory?organization_id=org-1&tab=warehouses',
    );
    // Open workspace link also carries the org.
    expect(screen.getByTestId('inventory-dashboard-workspace-link')).toHaveAttribute(
      'href',
      '/inventory?organization_id=org-1',
    );
  });

  it('renders summary + attention + activity-placeholder when the org has data', async () => {
    primeApi({
      orgs: [{ id: 'org-1', name: 'Aegis', slug: 'aegis' }],
      warehousesByOrg: { 'org-1': [WH_MAIN, WH_COLD] },
      itemsByOrg: { 'org-1': [ITEM_FEED, ITEM_MED] },
      lotsByWh: {
        [WH_MAIN.id]: [
          makeLot({ id: 'lot-ok', balance: '50' }),
          makeLot({ id: 'lot-out', balance: '0' }),
        ],
        [WH_COLD.id]: [
          makeLot({
            id: 'lot-soon',
            item_id: 'item-2',
            warehouse_id: 'wh-2',
            balance: '10',
            balance_unit: 'mL',
            expiry_date: '2099-01-01',
          }),
        ],
      },
    });
    render(<InventoryDashboardPage />);
    await waitFor(() =>
      expect(screen.getByTestId('inventory-dashboard-summary')).toBeInTheDocument(),
    );
    expect(screen.getByTestId('inventory-dashboard-metric-total_lots-value')).toHaveTextContent(
      '3',
    );
    expect(
      screen.getByTestId('inventory-dashboard-metric-out_of_stock_lots-value'),
    ).toHaveTextContent('1');
    expect(screen.getByTestId('inventory-dashboard-attention-row-lot-out')).toBeInTheDocument();
    // No ranked-activity list anywhere.
    expect(screen.queryByTestId('inventory-dashboard-recent')).not.toBeInTheDocument();
    // The deferred activity placeholder is shown instead.
    expect(screen.getByTestId('inventory-dashboard-activity-placeholder')).toBeInTheDocument();
    expect(screen.getByTestId('inventory-dashboard-activity-history-link')).toHaveAttribute(
      'href',
      '/inventory?organization_id=org-1&tab=history',
    );
  });

  it('preserves legitimate closed stock and inactive metadata while excluding the receipt fixture', async () => {
    const receiptFixtureWarehouse: DashboardWarehouse = {
      ...WH_CLOSED,
      id: CANONICAL_RELEASE_606_RECEIPT_FIXTURE_WAREHOUSE_ID,
      code: 'UAT_RECEIPT_WH_A',
      name: 'UAT Receipt Warehouse A',
    };
    primeApi({
      orgs: [{ id: 'org-1', name: 'Aegis', slug: 'aegis' }],
      warehousesByOrg: { 'org-1': [WH_MAIN, WH_CLOSED, receiptFixtureWarehouse] },
      itemsByOrg: { 'org-1': [ITEM_FEED, ITEM_INACTIVE] },
      lotsByWh: {
        [WH_MAIN.id]: [makeLot({ id: 'operational-lot', balance: '5' })],
        [WH_CLOSED.id]: [
          makeLot({
            id: 'legitimate-closed-lot',
            item_id: ITEM_INACTIVE.id,
            warehouse_id: WH_CLOSED.id,
            lot_code: 'LEGIT-CLOSED-LOT',
            balance: '0',
          }),
        ],
        [receiptFixtureWarehouse.id]: [
          makeLot({
            id: 'quarantined-receipt-lot',
            warehouse_id: receiptFixtureWarehouse.id,
            balance: '100',
          }),
        ],
      },
    });

    render(<InventoryDashboardPage />);
    await screen.findByTestId('inventory-dashboard-summary');

    expect(mockedApiFetch).toHaveBeenCalledWith('/v1/organizations/org-1/warehouses');
    expect(mockedApiFetch).toHaveBeenCalledWith('/v1/organizations/org-1/inventory-items');
    expect(mockedApiFetch).toHaveBeenCalledWith(`/v1/warehouses/${WH_CLOSED.id}/lots`);
    expect(mockedApiFetch).not.toHaveBeenCalledWith(
      `/v1/warehouses/${receiptFixtureWarehouse.id}/lots`,
    );
    expect(
      screen.getByTestId('inventory-dashboard-metric-total_warehouses-value'),
    ).toHaveTextContent('2');
    expect(screen.getByTestId('inventory-dashboard-metric-total_lots-value')).toHaveTextContent(
      '2',
    );
    expect(
      screen.getByTestId('inventory-dashboard-metric-total_active_items-value'),
    ).toHaveTextContent('1');
    expect(
      screen.getByTestId('inventory-dashboard-attention-row-legitimate-closed-lot'),
    ).toHaveTextContent('Old bags');
    expect(
      screen.getByTestId('inventory-dashboard-attention-row-legitimate-closed-lot'),
    ).toHaveTextContent('Archived');
    expect(screen.queryByText('100')).not.toBeInTheDocument();
  });

  // ------------------------------------------------------------------- //
  // Sprint 5.1 review fix #1 — organization preservation.
  // ------------------------------------------------------------------- //

  it('honours ?organization_id when it matches a real org for the caller', async () => {
    primeApi({
      orgs: [
        { id: 'org-1', name: 'Aegis', slug: 'aegis' },
        { id: 'org-2', name: 'Delta', slug: 'delta' },
      ],
      warehousesByOrg: { 'org-2': [WH_MAIN] },
      itemsByOrg: { 'org-2': [ITEM_FEED] },
      lotsByWh: { [WH_MAIN.id]: [makeLot({ id: 'lot-ok', balance: '5' })] },
    });
    setLocationSearch('?organization_id=org-2');
    render(<InventoryDashboardPage />);
    await waitFor(() =>
      expect(screen.getByTestId('inventory-dashboard-action-receive-stock')).toBeInTheDocument(),
    );
    expect(screen.getByTestId('inventory-dashboard-org-name')).toHaveTextContent('Delta');
    // Quick action retains the URL-selected org.
    expect(screen.getByTestId('inventory-dashboard-action-receive-stock')).toHaveAttribute(
      'href',
      '/inventory?organization_id=org-2&tab=receive',
    );
  });

  it('falls back to the first org when ?organization_id is unknown to the caller', async () => {
    primeApi({
      orgs: [{ id: 'org-1', name: 'Aegis', slug: 'aegis' }],
      warehousesByOrg: { 'org-1': [] },
      itemsByOrg: { 'org-1': [] },
    });
    setLocationSearch('?organization_id=spoofed-org');
    render(<InventoryDashboardPage />);
    await waitFor(() =>
      expect(screen.getByTestId('inventory-dashboard-org-name')).toHaveTextContent('Aegis'),
    );
  });

  it('propagates the selected org into every functional quick-action link', async () => {
    primeApi({
      orgs: [{ id: 'org-9', name: 'Nine', slug: 'nine' }],
      warehousesByOrg: { 'org-9': [WH_MAIN] },
      itemsByOrg: { 'org-9': [ITEM_FEED] },
      lotsByWh: { [WH_MAIN.id]: [] },
    });
    render(<InventoryDashboardPage />);
    await waitFor(() =>
      expect(screen.getByTestId('inventory-dashboard-quick-actions')).toBeInTheDocument(),
    );
    const receive = screen.getByTestId('inventory-dashboard-action-receive-stock');
    expect(receive).toHaveAttribute('href', '/inventory?organization_id=org-9&tab=receive');
    const items = screen.getByTestId('inventory-dashboard-action-view-items');
    expect(items).toHaveAttribute('href', '/inventory?organization_id=org-9&tab=items');
  });

  it('changes every link when the org selector switches', async () => {
    primeApi({
      orgs: [
        { id: 'org-1', name: 'Aegis', slug: 'aegis' },
        { id: 'org-2', name: 'Delta', slug: 'delta' },
      ],
      warehousesByOrg: { 'org-1': [], 'org-2': [] },
      itemsByOrg: { 'org-1': [], 'org-2': [] },
    });
    render(<InventoryDashboardPage />);
    await waitFor(() =>
      expect(screen.getByTestId('inventory-dashboard-org-selector')).toBeInTheDocument(),
    );
    const selector = screen.getByTestId('inventory-dashboard-org-selector') as HTMLSelectElement;
    fireEvent.change(selector, { target: { value: 'org-2' } });
    await waitFor(() =>
      expect(screen.getByTestId('inventory-dashboard-workspace-link')).toHaveAttribute(
        'href',
        '/inventory?organization_id=org-2',
      ),
    );
  });

  // ------------------------------------------------------------------- //
  // Sprint 5.1 review fix #2 — stale-response guard.
  // ------------------------------------------------------------------- //

  it('ignores a late organization-A response when the user has switched to B', async () => {
    // Sprint 5.1 review round #3 — dashboard stale-write regression.
    //
    // We used to stall organization A's fan-out behind a never-
    // resolving `new Promise(() => {})`, which "proved" the guard
    // only by leaving A's promises pending for the lifetime of the
    // test. That leaked React updates outside of `act()` and never
    // actually exercised the write path we care about.
    //
    // The correct shape: use deferred promises so we can complete
    // organization A's warehouses, items AND lot fan-out AFTER B has
    // rendered, and then assert that the generation guard has
    // prevented A's completed projection from stomping on B.
    function deferred<T>() {
      let resolve!: (v: T) => void;
      const promise = new Promise<T>((res) => {
        resolve = res;
      });
      return { promise, resolve };
    }

    const dAWarehouses = deferred<DashboardWarehouse[]>();
    const dAItems = deferred<DashboardInventoryItem[]>();
    const dALots = deferred<DashboardLot[]>();

    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/organizations') {
        return Promise.resolve([
          { id: 'org-1', name: 'Aegis', slug: 'aegis' },
          { id: 'org-2', name: 'Delta', slug: 'delta' },
        ]);
      }
      if (path === '/v1/organizations/org-1/warehouses') return dAWarehouses.promise;
      if (path === '/v1/organizations/org-1/inventory-items') return dAItems.promise;
      if (path === '/v1/organizations/org-2/warehouses') return Promise.resolve([WH_COLD]);
      if (path === '/v1/organizations/org-2/inventory-items') return Promise.resolve([ITEM_MED]);
      if (path === `/v1/warehouses/${WH_MAIN.id}/lots`) return dALots.promise;
      if (path === `/v1/warehouses/${WH_COLD.id}/lots`) {
        return Promise.resolve([
          makeLot({
            id: 'lot-b',
            warehouse_id: WH_COLD.id,
            item_id: 'item-2',
            balance: '0',
            balance_unit: 'mL',
          }),
        ]);
      }
      return Promise.reject(new ApiError(404, { detail: `unmocked ${path}` }));
    });

    render(<InventoryDashboardPage />);

    // Wait for A's org bootstrap to land (selector is now on org-1)
    // while its warehouses / items are still stalled.
    await waitFor(() =>
      expect(screen.getByTestId('inventory-dashboard-org-selector')).toBeInTheDocument(),
    );
    // Switch to B before A completes.
    fireEvent.change(screen.getByTestId('inventory-dashboard-org-selector'), {
      target: { value: 'org-2' },
    });

    // B rendered successfully.
    await waitFor(() =>
      expect(screen.getByTestId('inventory-dashboard-org-name')).toHaveTextContent('Delta'),
    );
    expect(screen.getByTestId('inventory-dashboard-attention')).toBeInTheDocument();
    expect(screen.getByText('Vaccine A')).toBeInTheDocument();

    // Now let organization A fully complete — warehouses, items AND
    // the warehouse-lots fan-out. Under a broken guard this would
    // finish `loadDashboard(org-1)` and stomp B's projection.
    await act(async () => {
      dAWarehouses.resolve([WH_MAIN]);
      dAItems.resolve([ITEM_FEED]);
      dALots.resolve([makeLot({ id: 'lot-a', warehouse_id: WH_MAIN.id, balance: '77' })]);
      // Let every awaited-in-flight continuation from A settle so we
      // are certain the obsolete `setState(...)` branch has had its
      // chance to run (and, thanks to the generation guard, silently
      // no-op).
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    // B's data must still be on-screen; nothing from A leaked.
    expect(screen.getByTestId('inventory-dashboard-org-name')).toHaveTextContent('Delta');
    expect(screen.getByText('Vaccine A')).toBeInTheDocument();
    expect(screen.queryByText('Starter feed')).not.toBeInTheDocument();
    // And critically: no summary metric was recomputed from A's lot
    // (which would put out_of_stock_lots at 0, not the B lot's 1).
    expect(
      screen.getByTestId('inventory-dashboard-metric-out_of_stock_lots-value'),
    ).toHaveTextContent('1');
  });

  // ------------------------------------------------------------------- //
  // Sprint 5.1 review fix #3 — fan-out 401 / 403 propagation.
  // ------------------------------------------------------------------- //

  it('redirects to /login when a lot fan-out request returns 401', async () => {
    primeApi({
      orgs: [{ id: 'org-1', name: 'Aegis', slug: 'aegis' }],
      warehousesByOrg: { 'org-1': [WH_MAIN, WH_COLD] },
      itemsByOrg: { 'org-1': [ITEM_FEED] },
      lotsByWh: {
        [WH_MAIN.id]: [makeLot({ id: 'lot-ok', balance: '5' })],
        [WH_COLD.id]: new ApiError(401, { detail: 'reauth' }),
      },
    });
    render(<InventoryDashboardPage />);
    await waitFor(() => expect(routerPush).toHaveBeenCalledWith('/login'));
    // No partial projection should be visible.
    expect(screen.queryByTestId('inventory-dashboard-summary')).not.toBeInTheDocument();
  });

  it('renders ForbiddenBanner when a lot fan-out request returns 403', async () => {
    primeApi({
      orgs: [{ id: 'org-1', name: 'Aegis', slug: 'aegis' }],
      warehousesByOrg: { 'org-1': [WH_MAIN, WH_COLD] },
      itemsByOrg: { 'org-1': [ITEM_FEED] },
      lotsByWh: {
        [WH_MAIN.id]: [makeLot({ id: 'lot-ok', balance: '5' })],
        [WH_COLD.id]: new ApiError(403, { detail: 'no access' }),
      },
    });
    render(<InventoryDashboardPage />);
    await waitFor(() => expect(screen.getByTestId('ape-forbidden')).toBeInTheDocument());
    expect(screen.queryByTestId('inventory-dashboard-summary')).not.toBeInTheDocument();
  });

  it('only shows the "understated totals" warning for non-auth partial failures', async () => {
    primeApi({
      orgs: [{ id: 'org-1', name: 'Aegis', slug: 'aegis' }],
      warehousesByOrg: { 'org-1': [WH_MAIN, WH_COLD] },
      itemsByOrg: { 'org-1': [ITEM_FEED] },
      lotsByWh: {
        [WH_MAIN.id]: [makeLot({ id: 'lot-ok', balance: '5' })],
        [WH_COLD.id]: new ApiError(500, { detail: 'boom' }),
      },
    });
    render(<InventoryDashboardPage />);
    await waitFor(() =>
      expect(screen.getByTestId('inventory-dashboard-summary')).toBeInTheDocument(),
    );
    expect(screen.getByTestId('ape-error')).toHaveTextContent(
      /One or more warehouses could not be loaded/i,
    );
    expect(screen.queryByTestId('ape-forbidden')).not.toBeInTheDocument();
  });

  // ------------------------------------------------------------------- //
  // Sprint 5.1 review fix #4 — 401 / 403 during bootstrap.
  // ------------------------------------------------------------------- //

  it('redirects to /login on 401 from the organization bootstrap', async () => {
    primeApi({ throwOnOrgs: new ApiError(401, { detail: 'unauthenticated' }) });
    render(<InventoryDashboardPage />);
    await waitFor(() => expect(routerPush).toHaveBeenCalledWith('/login'));
    expect(screen.queryByTestId('ape-forbidden')).not.toBeInTheDocument();
  });

  it('renders ForbiddenBanner on 403 from the organization bootstrap', async () => {
    primeApi({ throwOnOrgs: new ApiError(403, { detail: 'forbidden' }) });
    render(<InventoryDashboardPage />);
    await waitFor(() => expect(screen.getByTestId('ape-forbidden')).toBeInTheDocument());
    expect(routerPush).not.toHaveBeenCalledWith('/login');
  });

  it('surfaces a friendly ErrorBanner on a generic 500 during bootstrap', async () => {
    primeApi({ throwOnOrgs: new ApiError(500, { detail: 'downstream 500' }) });
    render(<InventoryDashboardPage />);
    await waitFor(() => expect(screen.getByTestId('ape-error')).toBeInTheDocument());
  });

  // ------------------------------------------------------------------- //
  // Sprint 5.1 review fix #5 — activity placeholder replaces the list.
  // ------------------------------------------------------------------- //

  it('never renders a ranked lot-activity list, only the deferred panel', async () => {
    primeApi({
      orgs: [{ id: 'org-1', name: 'Aegis', slug: 'aegis' }],
      warehousesByOrg: { 'org-1': [WH_MAIN] },
      itemsByOrg: { 'org-1': [ITEM_FEED] },
      lotsByWh: { [WH_MAIN.id]: [makeLot({ id: 'lot-1', balance: '5' })] },
    });
    render(<InventoryDashboardPage />);
    await waitFor(() =>
      expect(screen.getByTestId('inventory-dashboard-activity-placeholder')).toBeInTheDocument(),
    );
    expect(screen.queryByTestId('inventory-dashboard-recent')).not.toBeInTheDocument();
    expect(screen.queryByText(/Recent lot activity/i)).not.toBeInTheDocument();
    expect(screen.getByTestId('inventory-dashboard-activity-history-link')).toHaveAttribute(
      'href',
      '/inventory?organization_id=org-1&tab=history',
    );
  });
});
