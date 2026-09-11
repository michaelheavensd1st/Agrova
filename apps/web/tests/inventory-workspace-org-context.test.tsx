/**
 * Sprint 5.1 review rounds #2 + #3 — inventory workspace
 * organization context retention.
 *
 * Round #2 (already covered) proved that:
 *   - synchronous org switches clear org-dependent state;
 *   - populated forms are cleared via `key={orgId}` remounts;
 *   - cross-org submits are blocked by the pure guard helpers.
 *
 * Round #3 (this file) covers the async race conditions: obsolete
 * organization-A warehouse / item / lot / history responses that
 * resolve AFTER the user has switched to organization B must not
 * overwrite B's state, must not make organization-A identifiers
 * available to write-capable forms, and must never surface under
 * the B heading.
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

if (!('randomUUID' in (globalThis.crypto ?? {}))) {
  Object.assign(globalThis, {
    crypto: { ...(globalThis.crypto ?? {}), randomUUID: () => 'test-uuid' },
  });
}

import { apiFetch } from '@/lib/api';
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

/**
 * Manual deferred: `promise` never resolves until `resolve` is called
 * from the test. Lets us reliably order responses to reproduce a race.
 */
function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function setLocationSearch(search: string) {
  window.history.replaceState({}, '', `/inventory${search}`);
}

async function switchTab(key: string) {
  const tab = await screen.findByTestId(`inv-tab-${key}`);
  fireEvent.click(tab);
}

describe('/inventory workspace — organization context retention (sync)', () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
    routerPush.mockReset();
    setLocationSearch('');
    // Basic wiring: A and B both resolve immediately.
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/organizations') return Promise.resolve([ORG_A, ORG_B]);
      if (path.startsWith(`/v1/organizations/${ORG_A.id}/warehouses`))
        return Promise.resolve([WH_A]);
      if (path.startsWith(`/v1/organizations/${ORG_B.id}/warehouses`))
        return Promise.resolve([WH_B]);
      if (path.startsWith(`/v1/organizations/${ORG_A.id}/inventory-items`))
        return Promise.resolve([ITEM_A]);
      if (path.startsWith(`/v1/organizations/${ORG_B.id}/inventory-items`))
        return Promise.resolve([ITEM_B]);
      if (/^\/v1\/warehouses\/[^/]+\/lots$/.test(path)) return Promise.resolve([]);
      if (/^\/v1\/warehouses\/[^/]+\/storage-locations$/.test(path)) return Promise.resolve([]);
      if (/^\/v1\/lots\/[^/]+\/transactions/.test(path)) return Promise.resolve({ items: [] });
      return Promise.resolve([]);
    });
  });
  afterEach(() => {
    vi.clearAllMocks();
  });

  it('clears the Receive form fields when the organization changes', async () => {
    render(<InventoryPage />);
    await waitFor(() => expect(screen.getByTestId('inv-org-selector')).toBeInTheDocument());
    await switchTab('receive');
    const itemSelectA = (await screen.findByTestId('inv-receive-item')) as HTMLSelectElement;
    const lotCodeA = screen.getByTestId('inv-receive-lot-code') as HTMLInputElement;
    const qtyA = screen.getByTestId('inv-receive-quantity') as HTMLInputElement;
    await waitFor(() =>
      expect(Array.from(itemSelectA.options).some((o) => o.value === ITEM_A.id)).toBe(true),
    );
    fireEvent.change(itemSelectA, { target: { value: ITEM_A.id } });
    fireEvent.change(lotCodeA, { target: { value: 'LOT-A-STASH' } });
    fireEvent.change(qtyA, { target: { value: '42' } });

    fireEvent.change(screen.getByTestId('inv-org-selector'), { target: { value: ORG_B.id } });

    await waitFor(() => {
      const s = screen.getByTestId('inv-receive-item') as HTMLSelectElement;
      expect(Array.from(s.options).some((o) => o.value === ITEM_A.id)).toBe(false);
    });
    expect((screen.getByTestId('inv-receive-lot-code') as HTMLInputElement).value).toBe('');
    expect((screen.getByTestId('inv-receive-quantity') as HTMLInputElement).value).toBe('');
  });

  it('drops org-A warehouses from the Receive warehouse selector after switching', async () => {
    render(<InventoryPage />);
    await waitFor(() => expect(screen.getByTestId('inv-org-selector')).toBeInTheDocument());
    await switchTab('receive');
    const receiveWhA = (await screen.findByTestId('inv-receive-warehouse')) as HTMLSelectElement;
    await waitFor(() =>
      expect(Array.from(receiveWhA.options).some((o) => o.value === WH_A.id)).toBe(true),
    );

    fireEvent.change(screen.getByTestId('inv-org-selector'), { target: { value: ORG_B.id } });

    await waitFor(() => {
      const s = screen.getByTestId('inv-receive-warehouse') as HTMLSelectElement;
      expect(Array.from(s.options).some((o) => o.value === WH_A.id)).toBe(false);
      expect(Array.from(s.options).some((o) => o.value === WH_B.id)).toBe(true);
    });
  });
});

// --------------------------------------------------------------------- //
// Async race tests (Sprint 5.1 review round #3)
// --------------------------------------------------------------------- //

describe('/inventory workspace — async organization-race protection', () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
    routerPush.mockReset();
    setLocationSearch('');
  });
  afterEach(() => {
    vi.clearAllMocks();
  });

  it('Test A — obsolete organization-A warehouse+item response cannot overwrite B', async () => {
    // We stall the org-A warehouse AND item fetch behind manual
    // resolvers so we can complete them AFTER org B has rendered.
    const dAWh = deferred<(typeof WH_A)[]>();
    const dAItems = deferred<(typeof ITEM_A)[]>();

    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/organizations') return Promise.resolve([ORG_A, ORG_B]);
      if (path.startsWith(`/v1/organizations/${ORG_A.id}/warehouses`)) return dAWh.promise;
      if (path.startsWith(`/v1/organizations/${ORG_A.id}/inventory-items`)) return dAItems.promise;
      if (path.startsWith(`/v1/organizations/${ORG_B.id}/warehouses`))
        return Promise.resolve([WH_B]);
      if (path.startsWith(`/v1/organizations/${ORG_B.id}/inventory-items`))
        return Promise.resolve([ITEM_B]);
      if (/^\/v1\/warehouses\/[^/]+\/lots$/.test(path)) return Promise.resolve([]);
      if (/^\/v1\/warehouses\/[^/]+\/storage-locations$/.test(path)) return Promise.resolve([]);
      if (/^\/v1\/lots\/[^/]+\/transactions/.test(path)) return Promise.resolve({ items: [] });
      return Promise.resolve([]);
    });

    render(<InventoryPage />);
    await waitFor(() => expect(screen.getByTestId('inv-org-selector')).toBeInTheDocument());
    // Selector is now on org-A but its warehouses/items are stalled.
    // Switch to B.
    fireEvent.change(screen.getByTestId('inv-org-selector'), { target: { value: ORG_B.id } });

    // Wait until B has fully rendered its warehouse in the Receive tab.
    await switchTab('receive');
    const receiveWh = (await screen.findByTestId('inv-receive-warehouse')) as HTMLSelectElement;
    await waitFor(() =>
      expect(Array.from(receiveWh.options).some((o) => o.value === WH_B.id)).toBe(true),
    );
    const receiveItem = screen.getByTestId('inv-receive-item') as HTMLSelectElement;
    await waitFor(() =>
      expect(Array.from(receiveItem.options).some((o) => o.value === ITEM_B.id)).toBe(true),
    );

    // Now let org-A finally resolve — LATE.
    dAWh.resolve([WH_A]);
    dAItems.resolve([ITEM_A]);
    // Give React a couple of ticks to flush (and prove nothing stomped).
    await new Promise((r) => setTimeout(r, 30));

    // Assertions: selector still on B; only B options present.
    expect((screen.getByTestId('inv-org-selector') as HTMLSelectElement).value).toBe(ORG_B.id);
    const wh = screen.getByTestId('inv-receive-warehouse') as HTMLSelectElement;
    const it = screen.getByTestId('inv-receive-item') as HTMLSelectElement;
    expect(Array.from(wh.options).some((o) => o.value === WH_A.id)).toBe(false);
    expect(Array.from(wh.options).some((o) => o.value === WH_B.id)).toBe(true);
    expect(wh.value).toBe(WH_B.id);
    expect(Array.from(it.options).some((o) => o.value === ITEM_A.id)).toBe(false);
    expect(Array.from(it.options).some((o) => o.value === ITEM_B.id)).toBe(true);
  });

  it('Test B — obsolete organization-A lot response cannot repopulate lots under B', async () => {
    // Org A's warehouse resolves immediately so we can trigger a lot
    // fetch, but we stall the lot fetch itself. Org B's warehouse and
    // lot fetch resolve immediately.
    const dALots = deferred<(typeof LOT_A)[]>();

    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/organizations') return Promise.resolve([ORG_A, ORG_B]);
      if (path.startsWith(`/v1/organizations/${ORG_A.id}/warehouses`))
        return Promise.resolve([WH_A]);
      if (path.startsWith(`/v1/organizations/${ORG_A.id}/inventory-items`))
        return Promise.resolve([ITEM_A]);
      if (path.startsWith(`/v1/organizations/${ORG_B.id}/warehouses`))
        return Promise.resolve([WH_B]);
      if (path.startsWith(`/v1/organizations/${ORG_B.id}/inventory-items`))
        return Promise.resolve([ITEM_B]);
      if (path === `/v1/warehouses/${WH_A.id}/lots`) return dALots.promise;
      if (path === `/v1/warehouses/${WH_B.id}/lots`) return Promise.resolve([LOT_B]);
      if (/^\/v1\/warehouses\/[^/]+\/storage-locations$/.test(path)) return Promise.resolve([]);
      if (/^\/v1\/lots\/[^/]+\/transactions/.test(path)) return Promise.resolve({ items: [] });
      return Promise.resolve([]);
    });

    render(<InventoryPage />);
    await waitFor(() => expect(screen.getByTestId('inv-org-selector')).toBeInTheDocument());
    // Wait for org-A warehouses to hydrate so the lot fetch kicks off.
    await switchTab('lots');
    const lotsWhA = (await screen.findByTestId('inv-lots-warehouse')) as HTMLSelectElement;
    await waitFor(() => expect(lotsWhA.value).toBe(WH_A.id));

    // Switch to B before A's lot fetch resolves.
    fireEvent.change(screen.getByTestId('inv-org-selector'), { target: { value: ORG_B.id } });

    // Wait for B's warehouse and lot data to hydrate.
    await waitFor(() => {
      const wh = screen.getByTestId('inv-lots-warehouse') as HTMLSelectElement;
      expect(wh.value).toBe(WH_B.id);
    });
    // The B lot should be visible in the lots table (by lot code).
    await waitFor(() => expect(screen.getByText(LOT_B.lot_code)).toBeInTheDocument());

    // Now let A's lot fetch finally resolve — LATE.
    dALots.resolve([LOT_A]);
    await new Promise((r) => setTimeout(r, 30));

    // Assertions: A's lot must never appear.
    expect(screen.queryByText(LOT_A.lot_code)).not.toBeInTheDocument();
    // B lot still there.
    expect(screen.getByText(LOT_B.lot_code)).toBeInTheDocument();
  });

  it('Test C — obsolete lot history response does not render after org switch', async () => {
    // Give A a lot with a stalled history fetch; give B a different lot
    // with an immediate history fetch.
    const dAHistory = deferred<{ items: unknown[] }>();

    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/organizations') return Promise.resolve([ORG_A, ORG_B]);
      if (path.startsWith(`/v1/organizations/${ORG_A.id}/warehouses`))
        return Promise.resolve([WH_A]);
      if (path.startsWith(`/v1/organizations/${ORG_A.id}/inventory-items`))
        return Promise.resolve([ITEM_A]);
      if (path.startsWith(`/v1/organizations/${ORG_B.id}/warehouses`))
        return Promise.resolve([WH_B]);
      if (path.startsWith(`/v1/organizations/${ORG_B.id}/inventory-items`))
        return Promise.resolve([ITEM_B]);
      if (path === `/v1/warehouses/${WH_A.id}/lots`) return Promise.resolve([LOT_A]);
      if (path === `/v1/warehouses/${WH_B.id}/lots`) return Promise.resolve([LOT_B]);
      if (path === `/v1/lots/${LOT_A.id}/transactions`) return dAHistory.promise;
      if (path === `/v1/lots/${LOT_B.id}/transactions`)
        return Promise.resolve({
          items: [
            {
              id: 'tx-B',
              type: 'RECEIPT',
              lot_id: LOT_B.id,
              lot_code: LOT_B.lot_code,
              item_id: ITEM_B.id,
              item_name: ITEM_B.name,
              quantity: '10',
              unit: 'kg',
              performed_at: '2026-02-15T09:00:00.000Z',
            },
          ],
        });
      if (/^\/v1\/warehouses\/[^/]+\/storage-locations$/.test(path)) return Promise.resolve([]);
      return Promise.resolve([]);
    });

    render(<InventoryPage />);
    await waitFor(() => expect(screen.getByTestId('inv-org-selector')).toBeInTheDocument());
    // Switch to history tab, then select the org-A lot to trigger the
    // stalled history fetch.
    await switchTab('history');
    const historyLotA = (await screen.findByTestId('inv-history-lot')) as HTMLSelectElement;
    await waitFor(() =>
      expect(Array.from(historyLotA.options).some((o) => o.value === LOT_A.id)).toBe(true),
    );
    fireEvent.change(historyLotA, { target: { value: LOT_A.id } });

    // Switch org to B while A's history is stalled.
    fireEvent.change(screen.getByTestId('inv-org-selector'), { target: { value: ORG_B.id } });

    // Wait for B lot to hydrate, then select it → its history should
    // load and render immediately.
    await waitFor(() => {
      const lotSelect = screen.getByTestId('inv-history-lot') as HTMLSelectElement;
      expect(Array.from(lotSelect.options).some((o) => o.value === LOT_B.id)).toBe(true);
    });
    fireEvent.change(screen.getByTestId('inv-history-lot'), { target: { value: LOT_B.id } });
    await waitFor(() => expect(screen.getByText(/RECEIPT/i)).toBeInTheDocument());

    // Now let A's history finally resolve — LATE — with content that
    // must NOT appear.
    dAHistory.resolve({
      items: [
        {
          id: 'tx-A',
          type: 'RECEIPT',
          lot_id: LOT_A.id,
          lot_code: LOT_A.lot_code,
          item_id: ITEM_A.id,
          item_name: 'STALE-A-TRANSACTION',
          quantity: '999',
          unit: 'kg',
          performed_at: '2026-02-14T09:00:00.000Z',
        },
      ],
    });
    await new Promise((r) => setTimeout(r, 30));

    // The org-A stale history entry must NOT be on screen.
    expect(screen.queryByText('STALE-A-TRANSACTION')).not.toBeInTheDocument();
  });

  it('Test D — post-race, write forms cannot select an obsolete org-A warehouse or item', async () => {
    // Same race as Test A. After it finishes, we open the Receive form
    // and verify it cannot select WH_A or ITEM_A.
    const dAWh = deferred<(typeof WH_A)[]>();
    const dAItems = deferred<(typeof ITEM_A)[]>();

    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/organizations') return Promise.resolve([ORG_A, ORG_B]);
      if (path.startsWith(`/v1/organizations/${ORG_A.id}/warehouses`)) return dAWh.promise;
      if (path.startsWith(`/v1/organizations/${ORG_A.id}/inventory-items`)) return dAItems.promise;
      if (path.startsWith(`/v1/organizations/${ORG_B.id}/warehouses`))
        return Promise.resolve([WH_B]);
      if (path.startsWith(`/v1/organizations/${ORG_B.id}/inventory-items`))
        return Promise.resolve([ITEM_B]);
      if (/^\/v1\/warehouses\/[^/]+\/lots$/.test(path)) return Promise.resolve([]);
      if (/^\/v1\/warehouses\/[^/]+\/storage-locations$/.test(path)) return Promise.resolve([]);
      if (/^\/v1\/lots\/[^/]+\/transactions/.test(path)) return Promise.resolve({ items: [] });
      return Promise.resolve([]);
    });

    render(<InventoryPage />);
    await waitFor(() => expect(screen.getByTestId('inv-org-selector')).toBeInTheDocument());
    fireEvent.change(screen.getByTestId('inv-org-selector'), { target: { value: ORG_B.id } });
    await switchTab('receive');
    const whSelect = (await screen.findByTestId('inv-receive-warehouse')) as HTMLSelectElement;
    await waitFor(() =>
      expect(Array.from(whSelect.options).some((o) => o.value === WH_B.id)).toBe(true),
    );

    // Let A resolve late.
    dAWh.resolve([WH_A]);
    dAItems.resolve([ITEM_A]);
    await new Promise((r) => setTimeout(r, 30));

    // Even by forcing the DOM to WH_A / ITEM_A, no submit path should
    // succeed. React's controlled inputs will simply reject the value
    // (option not in the list) — verify that.
    const itemSelect = screen.getByTestId('inv-receive-item') as HTMLSelectElement;
    fireEvent.change(whSelect, { target: { value: WH_A.id } });
    fireEvent.change(itemSelect, { target: { value: ITEM_A.id } });
    expect(whSelect.value).not.toBe(WH_A.id);
    expect(itemSelect.value).not.toBe(ITEM_A.id);
    // And critically: no /warehouses/wh-A1/inventory:receive call was
    // (or could be) issued.
    const receiveCalls = mockedApiFetch.mock.calls.filter(([p]) =>
      String(p).endsWith('/inventory:receive'),
    );
    expect(receiveCalls).toHaveLength(0);
  });
});
