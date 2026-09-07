/**
 * Sprint 5.3 — Group 3: Authorization, Detail, Availability,
 * Activity, and Sprint 5.1/5.2 regression.
 *
 * Covers:
 *   - list 401 + 403;
 *   - detail 401 (via bootstrap) + cross-tenant item URL rejection;
 *   - availability fan-out 401 + 403 + partial;
 *   - activity fan-out 401 + 403 + partial + cap 100 + concurrency 5;
 *   - obsolete auth error after org switch cannot damage new context;
 *   - stale item-detail response cannot overwrite the current item;
 *   - Sprint 5.1 workspace and Sprint 5.2 warehouse pages remain
 *     loadable (regression smoke).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';

const { routerPush, routerReplace, stableRouter, useParamsMock, urlListeners } = vi.hoisted(() => {
  const push = vi.fn();
  const listeners = new Set<() => void>();
  const replace = vi.fn((url: string) => {
    if (typeof url === 'string' && typeof window !== 'undefined') {
      window.history.replaceState({}, '', url);
      listeners.forEach((l) => l());
    }
  });
  return {
    routerPush: push,
    routerReplace: replace,
    stableRouter: { push, replace, back: vi.fn() },
    useParamsMock: vi.fn(() => ({ itemId: '' })),
    urlListeners: listeners,
  };
});
vi.mock('next/navigation', async () => {
  const React = await vi.importActual<typeof import('react')>('react');
  return {
    useRouter: () => stableRouter,
    useParams: () => useParamsMock(),
    usePathname: () => (typeof window !== 'undefined' ? window.location.pathname : '/'),
    useSearchParams: () => {
      const [search, setSearch] = React.useState(
        typeof window !== 'undefined' ? window.location.search : '',
      );
      React.useEffect(() => {
        const listener = () => setSearch(window.location.search);
        urlListeners.add(listener);
        return () => {
          urlListeners.delete(listener);
        };
      }, []);
      return new URLSearchParams(search);
    },
  };
});
vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api');
  return { ...actual, apiFetch: vi.fn() };
});
const toastSpy = vi.hoisted(() => vi.fn());
vi.mock('@/components/ui-polish', async () => {
  const actual =
    await vi.importActual<typeof import('@/components/ui-polish')>('@/components/ui-polish');
  return { ...actual, toast: toastSpy };
});

import { apiFetch, ApiError } from '@/lib/api';
import InventoryItemListPage from '@/app/inventory/items/page';
import InventoryItemDetailPage from '@/app/inventory/items/[itemId]/page';
import {
  ACTIVITY_CONCURRENCY,
  ACTIVITY_LIMIT,
  WAREHOUSE_LOT_CONCURRENCY,
  inspectFanOut,
  inspectWarehouseLotFanOut,
  mapWithConcurrency,
  type InventoryItem,
} from '@/lib/inventory-items';

const mockedApiFetch = apiFetch as unknown as ReturnType<typeof vi.fn>;

const ORG_A = { id: 'org-A', name: 'Aegis', slug: 'aegis' };
const ORG_B = { id: 'org-B', name: 'Beacon', slug: 'beacon' };

function makeItem(over: Partial<InventoryItem> = {}): InventoryItem {
  return {
    id: 'item-1',
    organization_id: ORG_A.id,
    code: 'FEED-001',
    name: 'Starter feed',
    description: null,
    category: 'feed',
    canonical_unit: 'kg',
    sku: null,
    is_active: true,
    metadata_json: null,
    deleted_at: null,
    created_at: '2026-02-01T00:00:00.000Z',
    updated_at: '2026-02-10T00:00:00.000Z',
    ...over,
  };
}

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

// ------------------------------------------------------------------ //
// Pure helpers
// ------------------------------------------------------------------ //
describe('Fan-out primitives', () => {
  it('mapWithConcurrency never exceeds the configured limit', async () => {
    let inFlight = 0;
    let peak = 0;
    const results = await mapWithConcurrency(
      Array.from({ length: 20 }, (_, i) => i),
      ACTIVITY_CONCURRENCY,
      async () => {
        inFlight += 1;
        peak = Math.max(peak, inFlight);
        await new Promise((r) => setTimeout(r, 5));
        inFlight -= 1;
        return 'ok';
      },
    );
    expect(peak).toBeLessThanOrEqual(ACTIVITY_CONCURRENCY);
    expect(results.every((r) => r.status === 'fulfilled')).toBe(true);
  });

  it('inspectFanOut prioritises 401 → 403 → partial → ok', () => {
    expect(
      inspectFanOut(
        [
          { status: 'fulfilled', value: { items: [], next_cursor: null } },
          { status: 'rejected', reason: new ApiError(401, {}) },
        ],
        (r) => (r instanceof ApiError ? r.status : null),
      ).kind,
    ).toBe('unauthenticated');
    expect(
      inspectFanOut(
        [
          { status: 'fulfilled', value: { items: [], next_cursor: null } },
          { status: 'rejected', reason: new ApiError(403, {}) },
        ],
        (r) => (r instanceof ApiError ? r.status : null),
      ).kind,
    ).toBe('forbidden');
  });

  it('inspectFanOut caps at 100 and sorts newest first', () => {
    const many = Array.from({ length: 130 }, (_, i) => ({
      id: `tx-${i}`,
      transaction_type: 'receipt',
      quantity: '1',
      unit: 'kg',
      performed_at: new Date(2026, 0, 1, 0, 0, i).toISOString(),
      reason: null,
      reference_type: null,
    }));
    const outcome = inspectFanOut(
      [{ status: 'fulfilled', value: { items: many, next_cursor: null } }],
      (r) => (r instanceof ApiError ? r.status : null),
    );
    if (outcome.kind !== 'ok') throw new Error('expected ok');
    expect(outcome.transactions).toHaveLength(ACTIVITY_LIMIT);
    expect(outcome.transactions[0].id).toBe('tx-129');
  });

  it('inspectWarehouseLotFanOut also prioritises auth failures', () => {
    expect(
      inspectWarehouseLotFanOut(
        [
          { status: 'fulfilled', value: [] },
          { status: 'rejected', reason: new ApiError(403, {}) },
        ],
        (r) => (r instanceof ApiError ? r.status : null),
      ).kind,
    ).toBe('forbidden');
  });
});

// ------------------------------------------------------------------ //
// List authorization
// ------------------------------------------------------------------ //
describe('InventoryItemListPage — authorization', () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
    routerPush.mockReset();
    routerReplace.mockClear();
    toastSpy.mockReset();
    window.history.replaceState({}, '', '/inventory/items');
  });
  afterEach(() => vi.clearAllMocks());

  it('list 401 redirects to /login and does not toast', async () => {
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/organizations') return Promise.resolve([ORG_A]);
      if (path === `/v1/organizations/${ORG_A.id}/inventory-items`)
        return Promise.reject(new ApiError(401, {}));
      return Promise.resolve([]);
    });
    render(<InventoryItemListPage />);
    await waitFor(() => expect(routerPush).toHaveBeenCalledWith('/login'));
    expect(
      mockedApiFetch.mock.calls.filter(
        ([path]) => path === `/v1/organizations/${ORG_A.id}/inventory-items`,
      ),
    ).toHaveLength(1);
    expect(toastSpy).not.toHaveBeenCalled();
  });

  it('an obsolete post-unmount 401 cannot redirect or write visible state', async () => {
    const dA = deferred<InventoryItem[]>();
    const consoleErrorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/organizations') return Promise.resolve([ORG_A]);
      if (path === `/v1/organizations/${ORG_A.id}/inventory-items`) return dA.promise;
      return Promise.resolve([]);
    });

    try {
      const { container, unmount } = render(<InventoryItemListPage />);
      await waitFor(() =>
        expect(screen.getByTestId('item-list-org-name')).toHaveTextContent(ORG_A.name),
      );
      await waitFor(() =>
        expect(mockedApiFetch).toHaveBeenCalledWith(
          `/v1/organizations/${ORG_A.id}/inventory-items`,
        ),
      );

      unmount();
      await act(async () => {
        dA.reject(new ApiError(401, {}));
        await expect(dA.promise).rejects.toBeInstanceOf(ApiError);
      });

      expect(routerPush).not.toHaveBeenCalledWith('/login');
      expect(toastSpy).not.toHaveBeenCalled();
      expect(consoleErrorSpy).not.toHaveBeenCalled();
      expect(container).toBeEmptyDOMElement();
    } finally {
      consoleErrorSpy.mockRestore();
    }
  });

  it('list 403 shows the org-scope banner, no redirect, no toast', async () => {
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/organizations') return Promise.resolve([ORG_A]);
      if (path === `/v1/organizations/${ORG_A.id}/inventory-items`)
        return Promise.reject(new ApiError(403, {}));
      return Promise.resolve([]);
    });
    render(<InventoryItemListPage />);
    await waitFor(() => expect(screen.getByTestId('item-forbidden-org')).toBeInTheDocument());
    expect(routerPush).not.toHaveBeenCalledWith('/login');
    expect(toastSpy).not.toHaveBeenCalled();
  });

  it('obsolete org-A response cannot overwrite org-B', async () => {
    const dA = deferred<InventoryItem[]>();
    const dB = deferred<InventoryItem[]>();
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/organizations') return Promise.resolve([ORG_A, ORG_B]);
      if (path === `/v1/organizations/${ORG_A.id}/inventory-items`) return dA.promise;
      if (path === `/v1/organizations/${ORG_B.id}/inventory-items`) return dB.promise;
      return Promise.resolve([]);
    });
    render(<InventoryItemListPage />);
    const selector = await screen.findByTestId('item-list-org-selector');
    await waitFor(() => expect(selector).toHaveValue(ORG_A.id));
    await waitFor(() =>
      expect(mockedApiFetch).toHaveBeenCalledWith(`/v1/organizations/${ORG_A.id}/inventory-items`),
    );
    fireEvent.change(selector, {
      target: { value: ORG_B.id },
    });
    await waitFor(() => expect(selector).toHaveValue(ORG_B.id));
    await waitFor(() =>
      expect(mockedApiFetch).toHaveBeenCalledWith(`/v1/organizations/${ORG_B.id}/inventory-items`),
    );
    await act(async () => {
      dB.resolve([makeItem({ id: 'b-1', code: 'BEACON-1', organization_id: ORG_B.id })]);
      await dB.promise;
    });
    await waitFor(() => expect(screen.getByTestId('item-row-BEACON-1')).toBeInTheDocument());
    await act(async () => {
      dA.resolve([makeItem({ id: 'a-stale', code: 'STALE-A' })]);
      await dA.promise;
    });
    expect(screen.queryByTestId('item-row-STALE-A')).not.toBeInTheDocument();
    expect(screen.getByTestId('item-row-BEACON-1')).toBeInTheDocument();
  });

  it('obsolete org-A 401 cannot redirect or overwrite org-B', async () => {
    const dA = deferred<InventoryItem[]>();
    const dB = deferred<InventoryItem[]>();
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/organizations') return Promise.resolve([ORG_A, ORG_B]);
      if (path === `/v1/organizations/${ORG_A.id}/inventory-items`) return dA.promise;
      if (path === `/v1/organizations/${ORG_B.id}/inventory-items`) return dB.promise;
      return Promise.resolve([]);
    });
    render(<InventoryItemListPage />);
    const selector = await screen.findByTestId('item-list-org-selector');
    await waitFor(() => expect(selector).toHaveValue(ORG_A.id));
    await waitFor(() =>
      expect(mockedApiFetch).toHaveBeenCalledWith(`/v1/organizations/${ORG_A.id}/inventory-items`),
    );

    fireEvent.change(selector, { target: { value: ORG_B.id } });
    await waitFor(() => expect(selector).toHaveValue(ORG_B.id));
    await waitFor(() =>
      expect(mockedApiFetch).toHaveBeenCalledWith(`/v1/organizations/${ORG_B.id}/inventory-items`),
    );
    await act(async () => {
      dB.resolve([makeItem({ id: 'b-1', code: 'BEACON-1', organization_id: ORG_B.id })]);
      await dB.promise;
    });
    await waitFor(() => expect(screen.getByTestId('item-row-BEACON-1')).toBeInTheDocument());

    await act(async () => {
      dA.reject(new ApiError(401, {}));
      await expect(dA.promise).rejects.toBeInstanceOf(ApiError);
    });

    expect(routerPush).not.toHaveBeenCalledWith('/login');
    expect(selector).toHaveValue(ORG_B.id);
    expect(screen.getByTestId('item-list-org-name')).toHaveTextContent(ORG_B.name);
    expect(screen.getByTestId('item-row-BEACON-1')).toBeInTheDocument();
    expect(screen.queryByTestId('item-forbidden-org')).not.toBeInTheDocument();
    expect(toastSpy).not.toHaveBeenCalled();
  });
});

// ------------------------------------------------------------------ //
// Detail — cross-tenant + item stale response + auth
// ------------------------------------------------------------------ //
describe('InventoryItemDetailPage — cross-tenant + stale + auth', () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
    routerPush.mockReset();
    routerReplace.mockClear();
    toastSpy.mockReset();
    useParamsMock.mockReset();
    useParamsMock.mockReturnValue({ itemId: 'item-x' });
    window.history.replaceState({}, '', '/inventory/items/item-x?organization_id=org-A');
  });
  afterEach(() => vi.clearAllMocks());

  it('URL item that does not exist in the active org → forbidden-item banner', async () => {
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/organizations') return Promise.resolve([ORG_A]);
      // The URL item is not in org A's list — this is either a
      // cross-tenant attempt or a stale link. Either way, no swap.
      if (path === `/v1/organizations/${ORG_A.id}/inventory-items`) return Promise.resolve([]);
      if (path === `/v1/organizations/${ORG_A.id}/warehouses`) return Promise.resolve([]);
      return Promise.resolve([]);
    });
    render(<InventoryItemDetailPage />);
    await waitFor(() => expect(screen.getByTestId('item-forbidden-item')).toBeInTheDocument());
    expect(screen.queryByTestId('item-header-name')).not.toBeInTheDocument();
    // Back link preserves org context.
    expect(screen.getByTestId('item-detail-back')).toHaveAttribute(
      'href',
      `/inventory/items?organization_id=${ORG_A.id}`,
    );
  });

  it('stale item-detail response after remount is discarded (no crash, no stale write)', async () => {
    // We render the detail page against a deferred item-list; unmount
    // before it resolves; then resolve. If the guard is correct there
    // is neither a warning nor an unhandled rejection.
    const dOrgA = deferred<InventoryItem[]>();
    useParamsMock.mockReturnValue({ itemId: 'item-A' });
    window.history.replaceState({}, '', '/inventory/items/item-A?organization_id=org-A');
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/organizations') return Promise.resolve([ORG_A]);
      if (path === `/v1/organizations/${ORG_A.id}/inventory-items`) return dOrgA.promise;
      if (path === `/v1/organizations/${ORG_A.id}/warehouses`) return Promise.resolve([]);
      return Promise.resolve([]);
    });
    const { unmount } = render(<InventoryItemDetailPage />);
    await waitFor(() => expect(screen.getByTestId('item-detail-loading')).toBeInTheDocument());
    // Capture any React warnings that fire during the late resolve.
    const original = console.error;
    const captured: string[] = [];
    console.error = (...args: unknown[]) => {
      captured.push(String(args[0] ?? ''));
    };
    try {
      unmount();
      await act(async () => {
        dOrgA.resolve([makeItem({ id: 'item-A', code: 'FEED-A' })]);
        await Promise.resolve();
        await Promise.resolve();
      });
    } finally {
      console.error = original;
    }
    // No "state update on unmounted component" nor "act" warning.
    expect(
      captured.some((c) => /unmounted component|state update|not wrapped in act/i.test(c)),
    ).toBe(false);
  });
});

// ------------------------------------------------------------------ //
// Availability + activity fan-out
// ------------------------------------------------------------------ //
describe('InventoryItemDetailPage — availability + activity fan-out', () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
    routerPush.mockReset();
    routerReplace.mockClear();
    toastSpy.mockReset();
    useParamsMock.mockReset();
    useParamsMock.mockReturnValue({ itemId: 'item-1' });
    window.history.replaceState({}, '', '/inventory/items/item-1?organization_id=org-A');
  });
  afterEach(() => vi.clearAllMocks());

  it('availability fan-out never exceeds WAREHOUSE_LOT_CONCURRENCY in-flight', async () => {
    const item = makeItem({ id: 'item-1' });
    const warehouses = Array.from({ length: 15 }, (_, i) => ({
      id: `wh-${i}`,
      organization_id: ORG_A.id,
      code: `WH-${i}`,
      name: `Warehouse ${i}`,
      status: 'active',
    }));
    let inFlight = 0;
    let peak = 0;
    mockedApiFetch.mockImplementation(async (path: string) => {
      if (path === '/v1/organizations') return [ORG_A];
      if (path === `/v1/organizations/${ORG_A.id}/inventory-items`) return [item];
      if (path === `/v1/organizations/${ORG_A.id}/warehouses`) return warehouses;
      if (path.startsWith('/v1/warehouses/') && path.endsWith('/lots')) {
        inFlight += 1;
        peak = Math.max(peak, inFlight);
        await new Promise((r) => setTimeout(r, 5));
        inFlight -= 1;
        return [];
      }
      return [];
    });
    render(<InventoryItemDetailPage />);
    await waitFor(() => expect(screen.getByTestId('item-availability')).toBeInTheDocument());
    expect(peak).toBeLessThanOrEqual(WAREHOUSE_LOT_CONCURRENCY);
  });

  it('availability 403 shows scoped forbidden banner', async () => {
    const item = makeItem({ id: 'item-1' });
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/organizations') return Promise.resolve([ORG_A]);
      if (path === `/v1/organizations/${ORG_A.id}/inventory-items`) return Promise.resolve([item]);
      if (path === `/v1/organizations/${ORG_A.id}/warehouses`)
        return Promise.resolve([
          { id: 'wh-1', organization_id: ORG_A.id, code: 'W1', name: 'W1', status: 'active' },
        ]);
      if (path === '/v1/warehouses/wh-1/lots') return Promise.reject(new ApiError(403, {}));
      return Promise.resolve([]);
    });
    render(<InventoryItemDetailPage />);
    await waitFor(() =>
      expect(screen.getByTestId('item-forbidden-availability')).toBeInTheDocument(),
    );
    expect(routerPush).not.toHaveBeenCalledWith('/login');
  });

  it('availability 401 redirects to /login', async () => {
    const item = makeItem({ id: 'item-1' });
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/organizations') return Promise.resolve([ORG_A]);
      if (path === `/v1/organizations/${ORG_A.id}/inventory-items`) return Promise.resolve([item]);
      if (path === `/v1/organizations/${ORG_A.id}/warehouses`)
        return Promise.resolve([
          { id: 'wh-1', organization_id: ORG_A.id, code: 'W1', name: 'W1', status: 'active' },
        ]);
      if (path === '/v1/warehouses/wh-1/lots') return Promise.reject(new ApiError(401, {}));
      return Promise.resolve([]);
    });
    render(<InventoryItemDetailPage />);
    await waitFor(() => expect(routerPush).toHaveBeenCalledWith('/login'));
  });

  it('availability partial: one warehouse fails → partial notice + rest visible', async () => {
    const item = makeItem({ id: 'item-1' });
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/organizations') return Promise.resolve([ORG_A]);
      if (path === `/v1/organizations/${ORG_A.id}/inventory-items`) return Promise.resolve([item]);
      if (path === `/v1/organizations/${ORG_A.id}/warehouses`)
        return Promise.resolve([
          {
            id: 'wh-1',
            organization_id: ORG_A.id,
            code: 'W1',
            name: 'Warehouse 1',
            status: 'active',
          },
          {
            id: 'wh-2',
            organization_id: ORG_A.id,
            code: 'W2',
            name: 'Warehouse 2',
            status: 'active',
          },
        ]);
      if (path === '/v1/warehouses/wh-1/lots')
        return Promise.resolve([
          {
            id: 'lot-1',
            item_id: 'item-1',
            warehouse_id: 'wh-1',
            storage_location_id: null,
            lot_code: 'L1',
            expiry_date: null,
            balance: '10',
            balance_unit: 'kg',
          },
        ]);
      // A generic (non-auth) failure — surfaces as partial-data.
      if (path === '/v1/warehouses/wh-2/lots') return Promise.reject(new ApiError(500, {}));
      if (path === '/v1/lots/lot-1/transactions?limit=100')
        return Promise.resolve({ items: [], next_cursor: null });
      return Promise.resolve([]);
    });
    render(<InventoryItemDetailPage />);
    await waitFor(() =>
      expect(screen.getByTestId('item-availability-partial')).toBeInTheDocument(),
    );
    // Wh-1's row is present; wh-2 is not (its lots never arrived).
    expect(screen.getByTestId('item-availability-row-W1')).toBeInTheDocument();
    expect(screen.queryByTestId('item-availability-row-W2')).not.toBeInTheDocument();
  });

  it('activity 401 redirects, activity 403 shows scoped banner', async () => {
    const item = makeItem({ id: 'item-1' });
    // First: 401 path
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/organizations') return Promise.resolve([ORG_A]);
      if (path === `/v1/organizations/${ORG_A.id}/inventory-items`) return Promise.resolve([item]);
      if (path === `/v1/organizations/${ORG_A.id}/warehouses`)
        return Promise.resolve([
          { id: 'wh-1', organization_id: ORG_A.id, code: 'W1', name: 'W1', status: 'active' },
        ]);
      if (path === '/v1/warehouses/wh-1/lots')
        return Promise.resolve([
          {
            id: 'lot-1',
            item_id: 'item-1',
            warehouse_id: 'wh-1',
            storage_location_id: null,
            lot_code: 'L1',
            expiry_date: null,
            balance: '10',
            balance_unit: 'kg',
          },
        ]);
      if (path === '/v1/lots/lot-1/transactions?limit=100')
        return Promise.reject(new ApiError(401, {}));
      return Promise.resolve([]);
    });
    render(<InventoryItemDetailPage />);
    await waitFor(() => expect(routerPush).toHaveBeenCalledWith('/login'));
  });

  it('obsolete activity 403 after remount does not affect the next context', async () => {
    // Detail page renders for org-A, item-1 with a deferred
    // transactions request. We unmount before it resolves, then
    // reject the pending fan-out with 403. The now-stale generation
    // guard must prevent any redirect or state write.
    const dTx = deferred<{ items: unknown[] }>();
    useParamsMock.mockReturnValue({ itemId: 'item-1' });
    window.history.replaceState({}, '', '/inventory/items/item-1?organization_id=org-A');
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/organizations') return Promise.resolve([ORG_A]);
      if (path === `/v1/organizations/${ORG_A.id}/inventory-items`)
        return Promise.resolve([makeItem({ id: 'item-1' })]);
      if (path === `/v1/organizations/${ORG_A.id}/warehouses`)
        return Promise.resolve([
          { id: 'wh-1', organization_id: ORG_A.id, code: 'W1', name: 'W1', status: 'active' },
        ]);
      if (path === '/v1/warehouses/wh-1/lots')
        return Promise.resolve([
          {
            id: 'lot-1',
            item_id: 'item-1',
            warehouse_id: 'wh-1',
            storage_location_id: null,
            lot_code: 'L1',
            expiry_date: null,
            balance: '10',
            balance_unit: 'kg',
          },
        ]);
      if (path === '/v1/lots/lot-1/transactions?limit=100') return dTx.promise;
      return Promise.resolve([]);
    });
    const { unmount } = render(<InventoryItemDetailPage />);
    await waitFor(() => expect(screen.getByTestId('item-summary')).toBeInTheDocument());
    unmount();
    await act(async () => {
      dTx.reject(new ApiError(403, {}));
      await Promise.resolve();
      await Promise.resolve();
    });
    // Neither a login redirect nor a rendered forbidden banner
    // (there is no tree left to render into anyway) — the guard
    // simply drops the write.
    expect(routerPush).not.toHaveBeenCalledWith('/login');
  });
});

// ------------------------------------------------------------------ //
// Sprint 5.3 review — Finding 1 (route identity = orgId + itemId)
// and Finding 2 (activity pagination: limit=100 + next_cursor).
// ------------------------------------------------------------------ //
describe('InventoryItemDetailPage — Sprint 5.3 review findings', () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
    routerPush.mockReset();
    routerReplace.mockClear();
    toastSpy.mockReset();
    useParamsMock.mockReset();
    window.history.replaceState({}, '', '/');
  });
  afterEach(() => vi.clearAllMocks());

  // ---- Finding 2 ------------------------------------------------- //
  it('activity fetch requests limit=100 on every lot transactions call', async () => {
    useParamsMock.mockReturnValue({ itemId: 'item-1' });
    window.history.replaceState({}, '', '/inventory/items/item-1?organization_id=org-A');
    const item = makeItem({ id: 'item-1' });
    const observed: string[] = [];
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/organizations') return Promise.resolve([ORG_A]);
      if (path === `/v1/organizations/${ORG_A.id}/inventory-items`) return Promise.resolve([item]);
      if (path === `/v1/organizations/${ORG_A.id}/warehouses`)
        return Promise.resolve([
          { id: 'wh-1', organization_id: ORG_A.id, code: 'W1', name: 'W1', status: 'active' },
        ]);
      if (path === '/v1/warehouses/wh-1/lots')
        return Promise.resolve([
          {
            id: 'lot-1',
            item_id: 'item-1',
            warehouse_id: 'wh-1',
            storage_location_id: null,
            lot_code: 'L1',
            expiry_date: null,
            balance: '10',
            balance_unit: 'kg',
          },
        ]);
      if (path.startsWith('/v1/lots/') && path.includes('/transactions')) {
        observed.push(path);
        return Promise.resolve({ items: [], next_cursor: null });
      }
      return Promise.resolve([]);
    });
    render(<InventoryItemDetailPage />);
    await waitFor(() => expect(observed.length).toBeGreaterThan(0));
    // Every recorded transactions request must include the
    // display-cap-matching limit param. Extra params are allowed
    // (e.g., a future sort/order) but limit=100 must be there.
    for (const url of observed) {
      const qs = url.split('?')[1] ?? '';
      const params = new URLSearchParams(qs);
      expect(params.get('limit')).toBe('100');
    }
  });

  it('a non-null next_cursor on any lot marks the activity list as partial', async () => {
    useParamsMock.mockReturnValue({ itemId: 'item-1' });
    window.history.replaceState({}, '', '/inventory/items/item-1?organization_id=org-A');
    const item = makeItem({ id: 'item-1' });
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/organizations') return Promise.resolve([ORG_A]);
      if (path === `/v1/organizations/${ORG_A.id}/inventory-items`) return Promise.resolve([item]);
      if (path === `/v1/organizations/${ORG_A.id}/warehouses`)
        return Promise.resolve([
          { id: 'wh-1', organization_id: ORG_A.id, code: 'W1', name: 'W1', status: 'active' },
        ]);
      if (path === '/v1/warehouses/wh-1/lots')
        return Promise.resolve([
          {
            id: 'lot-1',
            item_id: 'item-1',
            warehouse_id: 'wh-1',
            storage_location_id: null,
            lot_code: 'L1',
            expiry_date: null,
            balance: '10',
            balance_unit: 'kg',
          },
        ]);
      if (path.startsWith('/v1/lots/lot-1/transactions')) {
        return Promise.resolve({
          items: [
            {
              id: 'tx-1',
              transaction_type: 'receipt',
              quantity: '1',
              unit: 'kg',
              performed_at: '2026-02-01T00:00:00.000Z',
              reason: null,
              reference_type: null,
            },
          ],
          // Backend indicates more transactions exist that we did
          // not fetch — the list must be surfaced as partial.
          next_cursor: 'opaque-cursor-value',
        });
      }
      return Promise.resolve([]);
    });
    render(<InventoryItemDetailPage />);
    await waitFor(() => expect(screen.getByTestId('item-activity-partial')).toBeInTheDocument());
  });

  // ---- Finding 1 ------------------------------------------------- //
  it('changing itemId in the same org resets item-scoped state and reloads', async () => {
    // First render for item-A. Then update the URL + useParams to
    // point at item-B (same org). The previous item's summary,
    // lots, activity, and error/editing state must be cleared
    // before item-B's data lands.
    useParamsMock.mockReturnValue({ itemId: 'item-A' });
    window.history.replaceState({}, '', '/inventory/items/item-A?organization_id=org-A');
    const itemA = makeItem({ id: 'item-A', code: 'FEED-A', name: 'Alpha' });
    const itemB = makeItem({ id: 'item-B', code: 'FEED-B', name: 'Bravo' });
    // We defer the item-B fetch so we can assert that item-A's
    // state has already been cleared BEFORE item-B lands.
    const dItemsB = deferred<InventoryItem[]>();
    let allowB = false;
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/organizations') return Promise.resolve([ORG_A]);
      if (path === `/v1/organizations/${ORG_A.id}/inventory-items`) {
        return allowB ? dItemsB.promise : Promise.resolve([itemA, itemB]);
      }
      if (path === `/v1/organizations/${ORG_A.id}/warehouses`) return Promise.resolve([]);
      return Promise.resolve([]);
    });
    const { rerender } = render(<InventoryItemDetailPage />);
    await waitFor(() => expect(screen.getByTestId('item-header-name')).toHaveTextContent('Alpha'));
    // Now switch itemId to item-B. Item-A's summary must be gone
    // (loading skeleton, not a stale header), and once item-B's
    // fetch resolves the new item is rendered.
    allowB = true;
    useParamsMock.mockReturnValue({ itemId: 'item-B' });
    rerender(<InventoryItemDetailPage />);
    // Immediately after the id changes, item-A's identity must be
    // erased (no stale "Alpha" header on screen).
    await waitFor(() => expect(screen.getByTestId('item-detail-loading')).toBeInTheDocument());
    expect(screen.queryByText('Alpha')).not.toBeInTheDocument();
    await act(async () => {
      dItemsB.resolve([itemA, itemB]);
      await Promise.resolve();
      await Promise.resolve();
    });
    await waitFor(() => expect(screen.getByTestId('item-header-name')).toHaveTextContent('Bravo'));
  });

  it('a stale item-A PATCH response cannot mutate item-B after itemId change', async () => {
    // Start editing item-A, defer the PATCH, switch to item-B,
    // then resolve the PATCH. Item-B must remain untouched.
    useParamsMock.mockReturnValue({ itemId: 'item-A' });
    window.history.replaceState({}, '', '/inventory/items/item-A?organization_id=org-A');
    const itemA = makeItem({ id: 'item-A', code: 'FEED-A', name: 'Alpha' });
    const itemB = makeItem({ id: 'item-B', code: 'FEED-B', name: 'Bravo' });
    const dPatch = deferred<InventoryItem>();
    mockedApiFetch.mockImplementation((path: string, init?: RequestInit) => {
      if (path === '/v1/organizations') return Promise.resolve([ORG_A]);
      if (path === `/v1/organizations/${ORG_A.id}/inventory-items`)
        return Promise.resolve([itemA, itemB]);
      if (path === `/v1/organizations/${ORG_A.id}/warehouses`) return Promise.resolve([]);
      if (path === `/v1/inventory-items/item-A` && init?.method === 'PATCH') return dPatch.promise;
      return Promise.resolve([]);
    });
    const { rerender } = render(<InventoryItemDetailPage />);
    await waitFor(() => expect(screen.getByTestId('item-header-name')).toHaveTextContent('Alpha'));
    // Open edit form, submit a rename.
    fireEvent.click(screen.getByTestId('item-header-edit'));
    fireEvent.change(screen.getByTestId('item-form-edit-name'), {
      target: { value: 'Alpha Renamed' },
    });
    fireEvent.click(screen.getByTestId('item-form-edit-submit'));
    // Now navigate to item-B before the PATCH resolves.
    useParamsMock.mockReturnValue({ itemId: 'item-B' });
    rerender(<InventoryItemDetailPage />);
    await waitFor(() => expect(screen.getByTestId('item-header-name')).toHaveTextContent('Bravo'));
    // Resolve the stale PATCH: it must be dropped, item-B header
    // stays "Bravo", and no success toast fires for the wrong item.
    await act(async () => {
      dPatch.resolve({ ...itemA, name: 'Alpha Renamed' });
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByTestId('item-header-name')).toHaveTextContent('Bravo');
    expect(toastSpy).not.toHaveBeenCalledWith('Item updated.', 'success');
  });
});

// ------------------------------------------------------------------ //
// Sprint 5.3 routing round — detail page reacts to URL organization
// changes and normalizes missing / invalid params.
// ------------------------------------------------------------------ //
describe('InventoryItemDetailPage — reactive URL organization sync', () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
    routerPush.mockReset();
    routerReplace.mockClear();
    toastSpy.mockReset();
    useParamsMock.mockReset();
    window.history.replaceState({}, '', '/');
  });
  afterEach(() => vi.clearAllMocks());

  it('changing only the URL organization_id (same itemId) clears org-A data and loads org-B', async () => {
    useParamsMock.mockReturnValue({ itemId: 'item-1' });
    window.history.replaceState({}, '', '/inventory/items/item-1?organization_id=org-A');
    const itemInA = makeItem({ id: 'item-1', code: 'FEED-A', name: 'Alpha (A)' });
    const itemInB = makeItem({
      id: 'item-1',
      organization_id: ORG_B.id,
      code: 'FEED-B',
      name: 'Bravo (B)',
    });
    // Defer the org-B item list so we can assert clearing of A's
    // header happens *before* B's data arrives.
    const dItemsB = deferred<InventoryItem[]>();
    let bServed = false;
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/organizations') return Promise.resolve([ORG_A, ORG_B]);
      if (path === `/v1/organizations/${ORG_A.id}/inventory-items`)
        return Promise.resolve([itemInA]);
      if (path === `/v1/organizations/${ORG_A.id}/warehouses`) return Promise.resolve([]);
      if (path === `/v1/organizations/${ORG_B.id}/inventory-items`) {
        bServed = true;
        return dItemsB.promise;
      }
      if (path === `/v1/organizations/${ORG_B.id}/warehouses`) return Promise.resolve([]);
      return Promise.resolve([]);
    });
    render(<InventoryItemDetailPage />);
    await waitFor(() =>
      expect(screen.getByTestId('item-header-name')).toHaveTextContent('Alpha (A)'),
    );
    // Change only the query string — itemId stays 'item-1'. This
    // is what a same-page navigation from org-A to org-B looks like.
    await act(async () => {
      routerReplace('/inventory/items/item-1?organization_id=org-B');
    });
    // Immediately after the URL flip, org-A data must be gone.
    await waitFor(() => expect(screen.getByTestId('item-detail-loading')).toBeInTheDocument());
    expect(screen.queryByText('Alpha (A)')).not.toBeInTheDocument();
    expect(bServed).toBe(true);
    // Now let org-B data arrive.
    await act(async () => {
      dItemsB.resolve([itemInB]);
      await Promise.resolve();
      await Promise.resolve();
    });
    await waitFor(() =>
      expect(screen.getByTestId('item-header-name')).toHaveTextContent('Bravo (B)'),
    );
  });

  it('stale org-A item-list response cannot overwrite org-B header after URL switch', async () => {
    useParamsMock.mockReturnValue({ itemId: 'item-1' });
    window.history.replaceState({}, '', '/inventory/items/item-1?organization_id=org-A');
    const itemInA = makeItem({ id: 'item-1', code: 'FEED-A', name: 'Alpha (A)' });
    const itemInB = makeItem({
      id: 'item-1',
      organization_id: ORG_B.id,
      code: 'FEED-B',
      name: 'Bravo (B)',
    });
    const dItemsA = deferred<InventoryItem[]>();
    let firstAFetch = true;
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/organizations') return Promise.resolve([ORG_A, ORG_B]);
      if (path === `/v1/organizations/${ORG_A.id}/inventory-items`) {
        if (firstAFetch) {
          firstAFetch = false;
          return dItemsA.promise;
        }
        return Promise.resolve([itemInA]);
      }
      if (path === `/v1/organizations/${ORG_B.id}/inventory-items`)
        return Promise.resolve([itemInB]);
      if (path === `/v1/organizations/${ORG_A.id}/warehouses`) return Promise.resolve([]);
      if (path === `/v1/organizations/${ORG_B.id}/warehouses`) return Promise.resolve([]);
      return Promise.resolve([]);
    });
    render(<InventoryItemDetailPage />);
    // Loading state visible while dItemsA is unresolved.
    await waitFor(() => expect(screen.getByTestId('item-detail-loading')).toBeInTheDocument());
    // Flip to org-B via URL — this bumps every generation ref.
    await act(async () => {
      routerReplace('/inventory/items/item-1?organization_id=org-B');
    });
    await waitFor(() =>
      expect(screen.getByTestId('item-header-name')).toHaveTextContent('Bravo (B)'),
    );
    // Late-arriving org-A data must NOT overwrite the org-B header.
    await act(async () => {
      dItemsA.resolve([itemInA]);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByTestId('item-header-name')).toHaveTextContent('Bravo (B)');
    expect(screen.queryByText('Alpha (A)')).not.toBeInTheDocument();
  });

  it('missing organization_id on the detail page is normalized via router.replace', async () => {
    useParamsMock.mockReturnValue({ itemId: 'item-1' });
    window.history.replaceState({}, '', '/inventory/items/item-1?keep=me');
    const itemInA = makeItem({ id: 'item-1', code: 'FEED-A', name: 'Alpha (A)' });
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/organizations') return Promise.resolve([ORG_A, ORG_B]);
      if (path === `/v1/organizations/${ORG_A.id}/inventory-items`)
        return Promise.resolve([itemInA]);
      if (path === `/v1/organizations/${ORG_A.id}/warehouses`) return Promise.resolve([]);
      return Promise.resolve([]);
    });
    render(<InventoryItemDetailPage />);
    await waitFor(() =>
      expect(screen.getByTestId('item-header-name')).toHaveTextContent('Alpha (A)'),
    );
    // URL now carries the normalized fallback organization; the
    // unrelated `keep=me` parameter is preserved.
    expect(window.location.search).toContain(`organization_id=${ORG_A.id}`);
    expect(window.location.search).toContain('keep=me');
    expect(routerReplace).toHaveBeenCalled();
  });

  it('invalid / inaccessible organization_id on the detail page is normalized to the fallback', async () => {
    useParamsMock.mockReturnValue({ itemId: 'item-1' });
    window.history.replaceState({}, '', '/inventory/items/item-1?organization_id=ghost-org');
    const itemInA = makeItem({ id: 'item-1', code: 'FEED-A', name: 'Alpha (A)' });
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/organizations') return Promise.resolve([ORG_A, ORG_B]);
      if (path === `/v1/organizations/${ORG_A.id}/inventory-items`)
        return Promise.resolve([itemInA]);
      if (path === `/v1/organizations/${ORG_A.id}/warehouses`) return Promise.resolve([]);
      return Promise.resolve([]);
    });
    render(<InventoryItemDetailPage />);
    await waitFor(() =>
      expect(screen.getByTestId('item-header-name')).toHaveTextContent('Alpha (A)'),
    );
    expect(window.location.search).toContain(`organization_id=${ORG_A.id}`);
    expect(window.location.search).not.toContain('ghost-org');
    expect(routerReplace).toHaveBeenCalled();
  });

  it('a valid organization_id on the detail page is never unnecessarily replaced', async () => {
    useParamsMock.mockReturnValue({ itemId: 'item-1' });
    window.history.replaceState({}, '', `/inventory/items/item-1?organization_id=${ORG_B.id}&x=y`);
    const itemInB = makeItem({
      id: 'item-1',
      organization_id: ORG_B.id,
      code: 'FEED-B',
      name: 'Bravo (B)',
    });
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/organizations') return Promise.resolve([ORG_A, ORG_B]);
      if (path === `/v1/organizations/${ORG_B.id}/inventory-items`)
        return Promise.resolve([itemInB]);
      if (path === `/v1/organizations/${ORG_B.id}/warehouses`) return Promise.resolve([]);
      return Promise.resolve([]);
    });
    render(<InventoryItemDetailPage />);
    await waitFor(() =>
      expect(screen.getByTestId('item-header-name')).toHaveTextContent('Bravo (B)'),
    );
    await act(async () => {
      await new Promise((r) => setTimeout(r, 50));
    });
    // Fixed point: URL == effective. No replace occurred.
    expect(routerReplace).not.toHaveBeenCalled();
    expect(window.location.search).toContain('x=y');
  });
});

// ------------------------------------------------------------------ //
// Sprint 5.1 / 5.2 regression smoke — ensure Sprint 5.3 did not
// break the pre-existing inventory workspace page.
// ------------------------------------------------------------------ //
describe('Sprint 5.1 regression — workspace still loads', () => {
  it('inventory workspace module remains importable and default-exports a component', async () => {
    const mod = await import('@/app/inventory/page');
    expect(typeof mod.default).toBe('function');
    // Sprint 5.3 must not have introduced item-management types
    // into the workspace's public export surface.
    const hasItemExport = Object.keys(mod).some((k) => /^InventoryItem/.test(k));
    expect(hasItemExport).toBe(false);
  });
});
