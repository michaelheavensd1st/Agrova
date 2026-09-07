/**
 * Sprint 5.1 review round #4 — inventory workspace authorization.
 *
 * The initial organization bootstrap redirected on 401 but every
 * downstream loader (reloadOrg / reloadLots / lot history) used to
 * fall back to a generic `toast(friendlyError(...))`. That meant a
 * 401 in a workspace fetch never reached /login, and a 403 was
 * indistinguishable from a 500.
 *
 * These tests pin the new centralized `handleLoadError` contract:
 *   - 401 anywhere → router.push('/login'), no state writes, no toast.
 *   - 403 → clear the affected slice of state (org / lot / history)
 *           and surface a scoped `inv-forbidden-{scope}` banner.
 *   - 403 must still respect the request-generation guard: an
 *     obsolete org-A 403 that arrives AFTER the user has switched
 *     to org-B must not damage org-B.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';

// --- shared mocks -------------------------------------------------- //

const { routerPush, stableRouter, searchParamsProxy } = vi.hoisted(() => {
  const push = vi.fn();
  return {
    routerPush: push,
    stableRouter: { push, replace: push, back: vi.fn() },
    searchParamsProxy: { current: new URLSearchParams() },
  };
});

vi.mock('next/navigation', () => ({
  useRouter: () => stableRouter,
  useSearchParams: () => searchParamsProxy.current,
}));

vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api');
  return {
    ...actual,
    apiFetch: vi.fn(),
  };
});

// Track any toast() calls (should not appear on auth paths).
const toastSpy = vi.hoisted(() => vi.fn());
vi.mock('@/components/ui-polish', async () => {
  const actual =
    await vi.importActual<typeof import('@/components/ui-polish')>('@/components/ui-polish');
  return {
    ...actual,
    toast: toastSpy,
  };
});

if (!('randomUUID' in (globalThis.crypto ?? {}))) {
  Object.assign(globalThis, {
    crypto: { ...(globalThis.crypto ?? {}), randomUUID: () => 'test-uuid' },
  });
}

import { apiFetch, ApiError } from '@/lib/api';
import InventoryPage from '@/app/inventory/page';

const mockedApiFetch = apiFetch as unknown as ReturnType<typeof vi.fn>;

// --- fixtures ------------------------------------------------------ //

const ORG_A = { id: 'org-A', name: 'Aegis', slug: 'aegis' };
const ORG_B = { id: 'org-B', name: 'Beacon', slug: 'beacon' };

const WH_A = {
  id: 'wh-A1',
  code: 'A-MAIN',
  name: 'Aegis main store',
  status: 'active',
  farm_id: null,
  organization_id: ORG_A.id,
};
const WH_B = {
  id: 'wh-B1',
  code: 'B-MAIN',
  name: 'Beacon main store',
  status: 'active',
  farm_id: null,
  organization_id: ORG_B.id,
};
const WH_MAINTENANCE = {
  ...WH_A,
  id: 'wh-maintenance',
  code: 'A-MAINT',
  name: 'Aegis maintenance store',
  status: 'maintenance',
};

const ITEM_A = {
  id: 'item-A1',
  code: 'F-A-STARTER',
  name: 'Aegis starter feed',
  category: 'feed',
  canonical_unit: 'kg',
  is_active: true,
};
const ITEM_B = {
  id: 'item-B1',
  code: 'F-B-STARTER',
  name: 'Beacon starter feed',
  category: 'feed',
  canonical_unit: 'kg',
  is_active: true,
};

const LOT_A = {
  id: 'lot-A',
  item_id: ITEM_A.id,
  warehouse_id: WH_A.id,
  storage_location_id: null,
  lot_code: 'LOT-A-STASH',
  expiry_date: null,
  balance: '100',
  balance_unit: 'kg',
  updated_at: '2026-02-10T10:00:00.000Z',
  created_at: '2026-02-01T10:00:00.000Z',
};
const LOT_B = {
  ...LOT_A,
  id: 'lot-B',
  item_id: ITEM_B.id,
  warehouse_id: WH_B.id,
  lot_code: 'LOT-B-STASH',
};
const LOT_MAINTENANCE = {
  ...LOT_A,
  id: 'lot-maintenance',
  warehouse_id: WH_MAINTENANCE.id,
  lot_code: 'LOT-MAINTENANCE-STASH',
};

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

async function switchTab(key: string) {
  const tab = await screen.findByTestId(`inv-tab-${key}`);
  fireEvent.click(tab);
}

function mockMaintenanceLifecycleWorkspace() {
  mockedApiFetch.mockImplementation((path: string, init?: RequestInit) => {
    if (path === '/v1/organizations') return Promise.resolve([ORG_A]);
    if (path === `/v1/organizations/${ORG_A.id}/warehouses`)
      return Promise.resolve([WH_A, WH_MAINTENANCE]);
    if (path === `/v1/organizations/${ORG_A.id}/inventory-items`)
      return Promise.resolve([ITEM_A]);
    if (path === `/v1/organizations/${ORG_A.id}/warehouses?operational_only=true`)
      return Promise.resolve([WH_A, WH_MAINTENANCE]);
    if (path === `/v1/organizations/${ORG_A.id}/inventory-items?operational_only=true`)
      return Promise.resolve([ITEM_A]);
    if (path === `/v1/warehouses/${WH_A.id}/lots`) return Promise.resolve([LOT_A]);
    if (path === `/v1/warehouses/${WH_MAINTENANCE.id}/lots`)
      return Promise.resolve([LOT_MAINTENANCE]);
    if (init?.method === 'POST') return Promise.resolve({ id: 'tx-new' });
    return Promise.resolve([]);
  });
}

async function selectMaintenanceWarehouse() {
  await switchTab('lots');
  const warehouseSelector = (await screen.findByTestId(
    'inv-lots-warehouse',
  )) as HTMLSelectElement;
  fireEvent.change(warehouseSelector, { target: { value: WH_MAINTENANCE.id } });
  await waitFor(() => {
    expect(warehouseSelector).toHaveValue(WH_MAINTENANCE.id);
    expect(mockedApiFetch).toHaveBeenCalledWith(
      `/v1/warehouses/${WH_MAINTENANCE.id}/lots`,
    );
  });
}

describe('/inventory workspace — authorization error handling', () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
    routerPush.mockReset();
    toastSpy.mockReset();
    window.history.replaceState({}, '', '/inventory');
  });
  afterEach(() => {
    vi.clearAllMocks();
  });

  // ---------------------------------------------------------------- //
  // Test 1 — org-scope 401
  // ---------------------------------------------------------------- //
  it('Test 1 — warehouse/item load 401 redirects to /login and clears org data', async () => {
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/organizations') return Promise.resolve([ORG_A]);
      if (path.startsWith(`/v1/organizations/${ORG_A.id}/warehouses`))
        return Promise.reject(new ApiError(401, { detail: 'session expired' }));
      if (path.startsWith(`/v1/organizations/${ORG_A.id}/inventory-items`))
        return Promise.reject(new ApiError(401, { detail: 'session expired' }));
      return Promise.resolve([]);
    });

    render(<InventoryPage />);
    await waitFor(() => expect(routerPush).toHaveBeenCalledWith('/login'));

    // Org-dependent data must be empty (never populated). The
    // selector remained on the bootstrapped org, but the workspace
    // body carries no rows from the failed request.
    const selector = (await screen.findByTestId('inv-org-selector')) as HTMLSelectElement;
    expect(selector.value).toBe(ORG_A.id);
    // No generic toast fired on the auth path.
    expect(toastSpy).not.toHaveBeenCalled();
  });

  // ---------------------------------------------------------------- //
  // Test 2 — org-scope 403
  // ---------------------------------------------------------------- //
  it('Test 2 — warehouse/item load 403 clears data and surfaces a permission banner', async () => {
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/organizations') return Promise.resolve([ORG_A]);
      if (path.startsWith(`/v1/organizations/${ORG_A.id}/warehouses`))
        return Promise.reject(new ApiError(403, { detail: 'forbidden' }));
      if (path.startsWith(`/v1/organizations/${ORG_A.id}/inventory-items`))
        return Promise.reject(new ApiError(403, { detail: 'forbidden' }));
      return Promise.resolve([]);
    });

    render(<InventoryPage />);
    await waitFor(() => expect(screen.getByTestId('inv-forbidden-org')).toBeInTheDocument());

    // No /login redirect on 403.
    expect(routerPush).not.toHaveBeenCalledWith('/login');
    // No generic toast on the auth path — banner replaces it.
    expect(toastSpy).not.toHaveBeenCalled();
    // Workspace tab bodies never rendered (banner replaces them).
    // The Warehouses tab, for instance, should have no warehouse list.
    await switchTab('warehouses');
    // The banner still occupies the body; no warehouse list appears.
    expect(screen.queryByTestId('inv-warehouses')).not.toBeInTheDocument();
    expect(screen.getByTestId('inv-forbidden-org')).toBeInTheDocument();
  });

  // ---------------------------------------------------------------- //
  // Test 3 — lot-scope 401 and 403
  // ---------------------------------------------------------------- //
  it('Test 3a — lot load 401 redirects to /login', async () => {
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/organizations') return Promise.resolve([ORG_A]);
      if (path.startsWith(`/v1/organizations/${ORG_A.id}/warehouses`))
        return Promise.resolve([WH_A]);
      if (path.startsWith(`/v1/organizations/${ORG_A.id}/inventory-items`))
        return Promise.resolve([ITEM_A]);
      if (path === `/v1/warehouses/${WH_A.id}/lots`)
        return Promise.reject(new ApiError(401, { detail: 'session expired' }));
      return Promise.resolve([]);
    });

    render(<InventoryPage />);
    await waitFor(() => expect(routerPush).toHaveBeenCalledWith('/login'));
    expect(toastSpy).not.toHaveBeenCalled();
  });

  it('Test 3b — first-load lot 403 clears lots and shows the lot-scope banner', async () => {
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/organizations') return Promise.resolve([ORG_A]);
      if (path.startsWith(`/v1/organizations/${ORG_A.id}/warehouses`))
        return Promise.resolve([WH_A]);
      if (path.startsWith(`/v1/organizations/${ORG_A.id}/inventory-items`))
        return Promise.resolve([ITEM_A]);
      if (path === `/v1/warehouses/${WH_A.id}/lots`)
        return Promise.reject(new ApiError(403, { detail: 'forbidden' }));
      return Promise.resolve([]);
    });

    render(<InventoryPage />);
    await switchTab('lots');
    // Banner appears in the Lots tab body.
    await waitFor(() => expect(screen.getByTestId('inv-forbidden-lot')).toBeInTheDocument());
    // Warehouses + items are still available (only lot-scope failed).
    await switchTab('warehouses');
    expect(await screen.findByText(WH_A.name)).toBeInTheDocument();
    // No /login redirect on 403.
    expect(routerPush).not.toHaveBeenCalledWith('/login');
    // No generic toast.
    expect(toastSpy).not.toHaveBeenCalled();
    // No stale lot row appears anywhere.
    expect(screen.queryByText(LOT_A.lot_code)).not.toBeInTheDocument();
  });

  // ---------------------------------------------------------------- //
  // Test 4 — history-scope 401 and 403
  // ---------------------------------------------------------------- //
  it('Test 4a — history load 401 redirects to /login', async () => {
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/organizations') return Promise.resolve([ORG_A]);
      if (path.startsWith(`/v1/organizations/${ORG_A.id}/warehouses`))
        return Promise.resolve([WH_A]);
      if (path.startsWith(`/v1/organizations/${ORG_A.id}/inventory-items`))
        return Promise.resolve([ITEM_A]);
      if (path === `/v1/warehouses/${WH_A.id}/lots`) return Promise.resolve([LOT_A]);
      if (path === `/v1/lots/${LOT_A.id}/transactions`)
        return Promise.reject(new ApiError(401, { detail: 'session expired' }));
      return Promise.resolve([]);
    });

    render(<InventoryPage />);
    await switchTab('history');
    fireEvent.change(await screen.findByTestId('inv-history-lot'), {
      target: { value: LOT_A.id },
    });
    await waitFor(() => expect(routerPush).toHaveBeenCalledWith('/login'));
    expect(toastSpy).not.toHaveBeenCalled();
  });

  it('Test 4b — history load 403 clears history rows and shows history-scope banner', async () => {
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/organizations') return Promise.resolve([ORG_A]);
      if (path.startsWith(`/v1/organizations/${ORG_A.id}/warehouses`))
        return Promise.resolve([WH_A]);
      if (path.startsWith(`/v1/organizations/${ORG_A.id}/inventory-items`))
        return Promise.resolve([ITEM_A]);
      if (path === `/v1/warehouses/${WH_A.id}/lots`) return Promise.resolve([LOT_A]);
      if (path === `/v1/lots/${LOT_A.id}/transactions`)
        return Promise.reject(new ApiError(403, { detail: 'forbidden' }));
      return Promise.resolve([]);
    });

    render(<InventoryPage />);
    await switchTab('history');
    fireEvent.change(await screen.findByTestId('inv-history-lot'), {
      target: { value: LOT_A.id },
    });
    // History-scope banner appears in the History tab body.
    await waitFor(() => expect(screen.getByTestId('inv-forbidden-history')).toBeInTheDocument());
    // No login redirect. No toast. No stale transaction rows.
    expect(routerPush).not.toHaveBeenCalledWith('/login');
    expect(toastSpy).not.toHaveBeenCalled();
    // The Lots data is still visible (only history-scope failed).
    await switchTab('lots');
    expect(await screen.findByText(LOT_A.lot_code)).toBeInTheDocument();
  });

  // ---------------------------------------------------------------- //
  // Test 5 — obsolete auth error must not damage new context
  // ---------------------------------------------------------------- //
  it('Test 5 — obsolete org-A 403 arriving after switch to B leaves B intact', async () => {
    const dAWarehouses = deferred<(typeof WH_A)[]>();
    const dAItems = deferred<(typeof ITEM_A)[]>();

    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/organizations') return Promise.resolve([ORG_A, ORG_B]);
      if (path.startsWith(`/v1/organizations/${ORG_A.id}/warehouses`)) return dAWarehouses.promise;
      if (path.startsWith(`/v1/organizations/${ORG_A.id}/inventory-items`)) return dAItems.promise;
      if (path.startsWith(`/v1/organizations/${ORG_B.id}/warehouses`))
        return Promise.resolve([WH_B]);
      if (path.startsWith(`/v1/organizations/${ORG_B.id}/inventory-items`))
        return Promise.resolve([ITEM_B]);
      if (path === `/v1/warehouses/${WH_B.id}/lots`) return Promise.resolve([LOT_B]);
      if (/^\/v1\/lots\/[^/]+\/transactions/.test(path)) return Promise.resolve({ items: [] });
      return Promise.resolve([]);
    });

    render(<InventoryPage />);
    await waitFor(() => expect(screen.getByTestId('inv-org-selector')).toBeInTheDocument());
    // Switch to B before A's fetches settle.
    fireEvent.change(screen.getByTestId('inv-org-selector'), { target: { value: ORG_B.id } });

    // Wait for B to hydrate — warehouses, items, lots all loaded.
    await switchTab('lots');
    await waitFor(() => expect(screen.getByText(LOT_B.lot_code)).toBeInTheDocument());

    // Now let A fail LATE with a 403. Under a broken guard this
    // would clear org state and show the org-scope banner.
    dAWarehouses.reject(new ApiError(403, { detail: 'forbidden' }));
    dAItems.reject(new ApiError(403, { detail: 'forbidden' }));
    // Let React flush the (now-stale) rejection.
    await new Promise((r) => setTimeout(r, 30));

    // B remains intact.
    expect((screen.getByTestId('inv-org-selector') as HTMLSelectElement).value).toBe(ORG_B.id);
    expect(screen.queryByTestId('inv-forbidden-org')).not.toBeInTheDocument();
    expect(screen.queryByTestId('inv-forbidden-lot')).not.toBeInTheDocument();
    expect(screen.getByText(LOT_B.lot_code)).toBeInTheDocument();
    // No login redirect either.
    expect(routerPush).not.toHaveBeenCalledWith('/login');
    // No toast either — the 403 was recognised as auth and dropped.
    expect(toastSpy).not.toHaveBeenCalled();
  });

  it('keeps quarantine resources in admin catalogs but out of operational flows', async () => {
    const closedWarehouse = {
      ...WH_A,
      id: 'wh-quarantine',
      code: 'UAT_RECEIPT_WH_A',
      name: 'UAT Receipt Warehouse A',
      status: 'closed',
    };
    const inactiveItem = {
      ...ITEM_A,
      id: 'item-quarantine',
      code: 'UAT-RECEIPT-FEED',
      name: 'UAT Receipt Feed',
      is_active: false,
    };

    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/organizations') return Promise.resolve([ORG_A]);
      if (path === `/v1/organizations/${ORG_A.id}/warehouses`)
        return Promise.resolve([WH_A, closedWarehouse]);
      if (path === `/v1/organizations/${ORG_A.id}/inventory-items`)
        return Promise.resolve([ITEM_A, inactiveItem]);
      if (path === `/v1/organizations/${ORG_A.id}/warehouses?operational_only=true`)
        return Promise.resolve([WH_A]);
      if (path === `/v1/organizations/${ORG_A.id}/inventory-items?operational_only=true`)
        return Promise.resolve([ITEM_A]);
      if (path === `/v1/warehouses/${WH_A.id}/lots`) return Promise.resolve([LOT_A]);
      if (path === `/v1/warehouses/${closedWarehouse.id}/lots`)
        return Promise.resolve([{ ...LOT_A, id: 'uat-100kg', balance: '100' }]);
      return Promise.resolve([]);
    });

    render(<InventoryPage />);

    await switchTab('warehouses');
    expect(await screen.findByText(closedWarehouse.name)).toBeInTheDocument();
    await switchTab('items');
    expect(await screen.findByText(inactiveItem.name)).toBeInTheDocument();

    await switchTab('receive');
    const warehouseSelector = await screen.findByTestId('inv-receive-warehouse');
    const itemSelector = screen.getByTestId('inv-receive-item');
    expect(warehouseSelector).toHaveTextContent(WH_A.name);
    expect(warehouseSelector).not.toHaveTextContent(closedWarehouse.name);
    expect(itemSelector).toHaveTextContent(ITEM_A.name);
    expect(itemSelector).not.toHaveTextContent(inactiveItem.name);
    expect(mockedApiFetch).not.toHaveBeenCalledWith(`/v1/warehouses/${closedWarehouse.id}/lots`);
  });

  it('allows a maintenance warehouse for receive', async () => {
    mockMaintenanceLifecycleWorkspace();
    render(<InventoryPage />);

    await switchTab('receive');

    const warehouseSelector = (await screen.findByTestId(
      'inv-receive-warehouse',
    )) as HTMLSelectElement;
    expect(Array.from(warehouseSelector.options).map((option) => option.value)).toContain(
      WH_MAINTENANCE.id,
    );
  });

  it('blocks issue from a maintenance warehouse', async () => {
    mockMaintenanceLifecycleWorkspace();
    render(<InventoryPage />);
    await selectMaintenanceWarehouse();
    await switchTab('issue');

    fireEvent.change(await screen.findByTestId('inv-issue-lot'), {
      target: { value: LOT_MAINTENANCE.id },
    });
    fireEvent.change(screen.getByTestId('inv-issue-qty'), { target: { value: '1' } });
    fireEvent.click(screen.getByTestId('inv-issue-submit'));

    expect(screen.queryByTestId('inv-issue-confirm')).not.toBeInTheDocument();
    expect(
      mockedApiFetch.mock.calls.some(([path]) =>
        String(path).endsWith('/inventory:issue'),
      ),
    ).toBe(false);
  });

  it('blocks transfer from a maintenance source warehouse', async () => {
    mockMaintenanceLifecycleWorkspace();
    render(<InventoryPage />);
    await selectMaintenanceWarehouse();
    await switchTab('transfer');

    fireEvent.change(await screen.findByTestId('inv-transfer-lot'), {
      target: { value: LOT_MAINTENANCE.id },
    });
    fireEvent.change(screen.getByTestId('inv-transfer-destination'), {
      target: { value: WH_A.id },
    });
    fireEvent.change(screen.getByTestId('inv-transfer-qty'), { target: { value: '1' } });
    fireEvent.click(screen.getByTestId('inv-transfer-submit'));

    expect(
      mockedApiFetch.mock.calls.some(([path]) =>
        String(path).endsWith('/inventory:transfer'),
      ),
    ).toBe(false);
  });

  it('allows maintenance as a transfer destination while excluding the active source', async () => {
    mockMaintenanceLifecycleWorkspace();
    render(<InventoryPage />);
    await switchTab('transfer');

    const destination = (await screen.findByTestId(
      'inv-transfer-destination',
    )) as HTMLSelectElement;
    const destinationIds = Array.from(destination.options).map((option) => option.value);
    expect(destinationIds).toContain(WH_MAINTENANCE.id);
    expect(destinationIds).not.toContain(WH_A.id);
  });

  it('allows an adjustment increase in a maintenance warehouse', async () => {
    mockMaintenanceLifecycleWorkspace();
    render(<InventoryPage />);
    await selectMaintenanceWarehouse();
    await switchTab('adjust');

    fireEvent.change(await screen.findByTestId('inv-adjust-direction'), {
      target: { value: 'increase' },
    });
    fireEvent.change(screen.getByTestId('inv-adjust-lot'), {
      target: { value: LOT_MAINTENANCE.id },
    });
    fireEvent.change(screen.getByTestId('inv-adjust-qty'), { target: { value: '1' } });
    fireEvent.change(screen.getByTestId('inv-adjust-reason'), {
      target: { value: 'Maintenance reconciliation increase' },
    });
    fireEvent.click(screen.getByTestId('inv-adjust-submit'));

    expect(screen.getByTestId('inv-adjust-confirm')).toBeInTheDocument();
  });

  it('blocks the default adjustment decrease in a maintenance warehouse', async () => {
    mockMaintenanceLifecycleWorkspace();
    render(<InventoryPage />);
    await selectMaintenanceWarehouse();
    await switchTab('adjust');

    expect(await screen.findByTestId('inv-adjust-direction')).toHaveValue('decrease');
    fireEvent.change(screen.getByTestId('inv-adjust-lot'), {
      target: { value: LOT_MAINTENANCE.id },
    });
    fireEvent.change(screen.getByTestId('inv-adjust-qty'), { target: { value: '1' } });
    fireEvent.change(screen.getByTestId('inv-adjust-reason'), {
      target: { value: 'Maintenance reconciliation decrease' },
    });
    fireEvent.click(screen.getByTestId('inv-adjust-submit'));

    expect(screen.queryByTestId('inv-adjust-confirm')).not.toBeInTheDocument();
    expect(
      mockedApiFetch.mock.calls.some(([path]) =>
        String(path).endsWith('/inventory:adjust'),
      ),
    ).toBe(false);
  });

  it('keeps inactive-item lots identifiable in reporting but out of stock operations', async () => {
    const destinationWarehouse = {
      ...WH_A,
      id: 'wh-A2',
      code: 'A-SECONDARY',
      name: 'Aegis secondary store',
    };
    const inactiveItem = {
      ...ITEM_A,
      id: 'item-inactive',
      code: 'F-A-ARCHIVED',
      name: 'Archived Aegis feed',
      is_active: false,
    };
    const inactiveItemLot = {
      ...LOT_A,
      id: 'lot-inactive-item',
      item_id: inactiveItem.id,
      lot_code: 'LOT-ARCHIVED-FEED',
      balance: '25',
    };
    const inactiveItemTransaction = {
      id: 'tx-inactive-item',
      transaction_type: 'receipt',
      quantity: '25',
      unit: 'kg',
      reason: 'Historical archived feed receipt',
      reference_type: 'purchase',
      performed_at: '2026-02-01T10:00:00.000Z',
    };

    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/organizations') return Promise.resolve([ORG_A]);
      if (path === `/v1/organizations/${ORG_A.id}/warehouses`)
        return Promise.resolve([WH_A, destinationWarehouse]);
      if (path === `/v1/organizations/${ORG_A.id}/inventory-items`)
        return Promise.resolve([ITEM_A, inactiveItem]);
      if (path === `/v1/organizations/${ORG_A.id}/warehouses?operational_only=true`)
        return Promise.resolve([WH_A, destinationWarehouse]);
      if (path === `/v1/organizations/${ORG_A.id}/inventory-items?operational_only=true`)
        return Promise.resolve([ITEM_A]);
      if (path === `/v1/warehouses/${WH_A.id}/lots`)
        return Promise.resolve([LOT_A, inactiveItemLot]);
      if (path === `/v1/lots/${inactiveItemLot.id}/transactions`)
        return Promise.resolve({ items: [inactiveItemTransaction] });
      return Promise.resolve([]);
    });

    render(<InventoryPage />);

    await waitFor(() => expect(screen.getByText('Lots').nextElementSibling).toHaveTextContent('1'));
    const overview = screen.getByTestId('inv-overview');
    expect(overview).toHaveTextContent(ITEM_A.name);
    expect(overview).not.toHaveTextContent(inactiveItem.name);

    await switchTab('lots');
    expect(await screen.findByText(inactiveItemLot.lot_code)).toBeInTheDocument();
    expect(screen.getByText(inactiveItem.name)).toBeInTheDocument();
    expect(screen.queryByText(inactiveItem.id)).not.toBeInTheDocument();

    for (const operation of ['issue', 'transfer', 'adjust']) {
      await switchTab(operation);
      const lotSelector = (await screen.findByTestId(`inv-${operation}-lot`)) as HTMLSelectElement;
      const optionValues = Array.from(lotSelector.options, (option) => option.value);
      expect(lotSelector).toHaveTextContent(LOT_A.lot_code);
      expect(lotSelector).not.toHaveTextContent(inactiveItemLot.lot_code);
      expect(optionValues).toContain(LOT_A.id);
      expect(optionValues).not.toContain(inactiveItemLot.id);
    }

    await switchTab('history');
    const historyLotSelector = await screen.findByTestId('inv-history-lot');
    expect(historyLotSelector).toHaveTextContent(inactiveItemLot.lot_code);
    fireEvent.change(historyLotSelector, { target: { value: inactiveItemLot.id } });
    await waitFor(() =>
      expect(mockedApiFetch).toHaveBeenCalledWith(`/v1/lots/${inactiveItemLot.id}/transactions`),
    );
    expect(await screen.findByText(inactiveItemTransaction.reason)).toBeInTheDocument();
  });
});
