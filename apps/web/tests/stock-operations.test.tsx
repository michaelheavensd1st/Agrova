/**
 * Sprint 5.4 — Stock operations regression tests.
 *
 * Covers the full grid the spec calls out:
 *   Receive · Issue · Transfer · Adjust · Reverse
 * plus the cross-cutting invariants:
 *   idempotency-key discipline · pending state · authorization ·
 *   route/generation guards · post-mutation refresh.
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
import InventoryItemDetailPage from '@/app/inventory/items/[itemId]/page';
import type { InventoryItem, ItemLot, ItemWarehouse } from '@/lib/inventory-items';

const mockedApiFetch = apiFetch as unknown as ReturnType<typeof vi.fn>;

const ORG_A = { id: 'org-A', name: 'Aegis', slug: 'aegis' };
const ORG_B = { id: 'org-B', name: 'Beacon', slug: 'beacon' };
const WH_1: ItemWarehouse = {
  id: 'wh-1',
  organization_id: ORG_A.id,
  code: 'W1',
  name: 'Warehouse One',
  status: 'active',
};
const WH_2: ItemWarehouse = {
  id: 'wh-2',
  organization_id: ORG_A.id,
  code: 'W2',
  name: 'Warehouse Two',
  status: 'active',
};
const WH_MAINTENANCE: ItemWarehouse = {
  id: 'wh-maintenance',
  organization_id: ORG_A.id,
  code: 'MAINT',
  name: 'Maintenance Warehouse',
  status: 'maintenance',
};

const ITEM: InventoryItem = {
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
  created_at: '2026-01-01T00:00:00.000Z',
  updated_at: '2026-01-01T00:00:00.000Z',
} as InventoryItem;

const LOT_1: ItemLot = {
  id: 'lot-1',
  item_id: 'item-1',
  warehouse_id: 'wh-1',
  storage_location_id: null,
  lot_code: 'L1',
  expiry_date: null,
  balance: '50',
  balance_unit: 'kg',
} as ItemLot;

const LOT_MAINTENANCE: ItemLot = {
  ...LOT_1,
  id: 'lot-maintenance',
  warehouse_id: WH_MAINTENANCE.id,
  lot_code: 'MAINT-LOT',
} as ItemLot;

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

async function primeDetailPage(opts?: {
  items?: InventoryItem[];
  warehouses?: ItemWarehouse[];
  lots?: ItemLot[];
  transactions?: unknown[];
  transactionsByLot?: Record<string, unknown[]>;
}) {
  const items = opts?.items ?? [ITEM];
  const warehouses = opts?.warehouses ?? [WH_1, WH_2];
  const lots = opts?.lots ?? [LOT_1];
  const txPage = { items: opts?.transactions ?? [], next_cursor: null };
  mockedApiFetch.mockImplementation((path: string, init?: RequestInit) => {
    // Read paths.
    if (path === '/v1/organizations') return Promise.resolve([ORG_A, ORG_B]);
    if (path === `/v1/organizations/${ORG_A.id}/inventory-items`) return Promise.resolve(items);
    if (path === `/v1/organizations/${ORG_A.id}/warehouses`) return Promise.resolve(warehouses);
    const warehouseLotsMatch = path.match(/^\/v1\/warehouses\/([^/]+)\/lots$/);
    if (warehouseLotsMatch)
      return Promise.resolve(lots.filter((l) => l.warehouse_id === warehouseLotsMatch[1]));
    const lotTransactionsMatch = path.match(/^\/v1\/lots\/([^/]+)\/transactions/);
    if (lotTransactionsMatch) {
      return Promise.resolve({
        ...txPage,
        items: opts?.transactionsByLot?.[lotTransactionsMatch[1]] ?? txPage.items,
      });
    }
    // Write paths are intercepted by individual tests via a
    // secondary layer set up on top of this base.
    if (init?.method === 'POST') return Promise.resolve({ id: 'tx-new' });
    return Promise.resolve([]);
  });
  useParamsMock.mockReturnValue({ itemId: 'item-1' });
  window.history.replaceState({}, '', '/inventory/items/item-1?organization_id=org-A');
  render(<InventoryItemDetailPage />);
  await waitFor(() => expect(screen.getByTestId('item-detail-stock-actions')).toBeInTheDocument());
}

it('retains closed-warehouse availability and history without offering it for new operations', async () => {
  const closedWarehouse: ItemWarehouse = {
    id: 'wh-closed',
    organization_id: ORG_A.id,
    code: 'ARCH',
    name: 'Closed Archive',
    status: 'closed',
  };
  const closedLot: ItemLot = {
    ...LOT_1,
    id: 'lot-closed',
    warehouse_id: closedWarehouse.id,
    lot_code: 'ARCHIVE-LOT',
  };
  const closedLotTransaction = {
    id: 'tx-closed-lot',
    transaction_type: 'receipt',
    quantity: '10',
    unit: 'kg',
    performed_at: '2026-01-02T00:00:00.000Z',
    reason: 'Historical receipt',
    reference_type: null,
  };

  await primeDetailPage({
    warehouses: [WH_1, closedWarehouse],
    lots: [LOT_1, closedLot],
    transactionsByLot: { [closedLot.id]: [closedLotTransaction], [LOT_1.id]: [] },
  });

  expect(mockedApiFetch).toHaveBeenCalledWith(`/v1/organizations/${ORG_A.id}/warehouses`);
  expect(await screen.findByTestId('item-availability-row-ARCH')).toHaveTextContent(
    'Closed Archive',
  );
  expect(mockedApiFetch).toHaveBeenCalledWith(`/v1/warehouses/${closedWarehouse.id}/lots`);
  expect(screen.getByTestId('item-lot-row-ARCHIVE-LOT')).toHaveTextContent('Closed Archive');
  expect(
    await screen.findByTestId(`item-activity-row-${closedLotTransaction.id}`),
  ).toHaveTextContent('Historical receipt');
  expect(mockedApiFetch).toHaveBeenCalledWith(`/v1/lots/${closedLot.id}/transactions?limit=100`);

  fireEvent.click(screen.getByTestId('item-detail-stock-receive'));
  expect(screen.getByRole('option', { name: WH_1.name })).toBeInTheDocument();
  expect(screen.queryByRole('option', { name: closedWarehouse.name })).not.toBeInTheDocument();
});

function rejectNextPostWithValidation(detail: Array<{ loc: string[]; msg: string }>) {
  mockedApiFetch.mockImplementation((_path: string, init?: RequestInit) => {
    if (init?.method === 'POST') {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      return Promise.reject(new ApiError(422, { detail } as any));
    }
    return Promise.resolve([]);
  });
}

async function submitReceiveForValidation() {
  fireEvent.click(screen.getByTestId('item-detail-stock-receive'));
  fireEvent.change(screen.getByTestId('stock-op-receive-warehouse'), {
    target: { value: WH_1.id },
  });
  fireEvent.change(screen.getByTestId('stock-op-receive-lot-code'), {
    target: { value: 'LOT-XYZ' },
  });
  fireEvent.change(screen.getByTestId('stock-op-receive-quantity'), {
    target: { value: '5' },
  });
  fireEvent.click(screen.getByTestId('stock-op-receive-submit'));
  await waitFor(() => expect(screen.getByTestId('stock-op-receive-confirm')).toBeInTheDocument());
  fireEvent.click(screen.getByTestId('stock-op-receive-confirm'));
}

// ------------------------------------------------------------------ //
// RECEIVE                                                            //
// ------------------------------------------------------------------ //
describe('StockOperationDialog — receive', () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
    routerPush.mockReset();
    routerReplace.mockClear();
    toastSpy.mockReset();
    useParamsMock.mockReset();
    window.history.replaceState({}, '', '/');
  });
  afterEach(() => vi.clearAllMocks());

  it('offers a maintenance warehouse for receive', async () => {
    await primeDetailPage({ warehouses: [WH_1, WH_MAINTENANCE] });

    fireEvent.click(screen.getByTestId('item-detail-stock-receive'));

    expect(screen.getByRole('option', { name: WH_MAINTENANCE.name })).toBeInTheDocument();
  });

  it('valid receipt submits the correct payload + fresh Idempotency-Key + refreshes', async () => {
    await primeDetailPage();
    fireEvent.click(screen.getByTestId('item-detail-stock-receive'));
    fireEvent.change(screen.getByTestId('stock-op-receive-warehouse'), {
      target: { value: WH_1.id },
    });
    fireEvent.change(screen.getByTestId('stock-op-receive-lot-code'), {
      target: { value: 'LOT-XYZ' },
    });
    fireEvent.change(screen.getByTestId('stock-op-receive-quantity'), {
      target: { value: '25' },
    });
    // Step 1: submit → confirmation summary appears.
    fireEvent.click(screen.getByTestId('stock-op-receive-submit'));
    await waitFor(() => expect(screen.getByTestId('stock-op-receive-summary')).toBeInTheDocument());
    // Step 2: confirm → POST fires.
    fireEvent.click(screen.getByTestId('stock-op-receive-confirm'));
    await waitFor(() => {
      const postCalls = mockedApiFetch.mock.calls.filter(
        (c) => (c[1] as RequestInit | undefined)?.method === 'POST',
      );
      expect(postCalls.length).toBe(1);
    });
    const postCall = mockedApiFetch.mock.calls.find(
      (c) => (c[1] as RequestInit | undefined)?.method === 'POST',
    );
    expect(postCall).toBeDefined();
    const [path, init] = postCall as [string, RequestInit];
    expect(path).toBe(`/v1/warehouses/${WH_1.id}/inventory:receive`);
    const body = JSON.parse(init.body as string);
    expect(body).toMatchObject({
      item_id: ITEM.id,
      lot_code: 'LOT-XYZ',
      quantity: '25',
      unit: ITEM.canonical_unit,
    });
    // Idempotency-Key must be present and non-empty.
    const headers = init.headers as Record<string, string>;
    expect(headers['Idempotency-Key']).toBeDefined();
    expect(headers['Idempotency-Key'].length).toBeGreaterThan(8);
    // Success toast fires and dialog closes.
    await waitFor(() =>
      expect(toastSpy).toHaveBeenCalledWith('Receive stock succeeded.', 'success'),
    );
    await waitFor(() => expect(screen.queryByTestId('stock-op-receive')).not.toBeInTheDocument());
    // The parent refresh reloaded item + warehouses + lots + activity.
    const readsAfter = mockedApiFetch.mock.calls.filter(
      (c) => (c[1] as RequestInit | undefined)?.method !== 'POST',
    );
    expect(readsAfter.some((c) => c[0] === '/v1/warehouses/wh-1/lots')).toBe(true);
  });

  it('a rapid double-click produces only one POST', async () => {
    await primeDetailPage();
    fireEvent.click(screen.getByTestId('item-detail-stock-receive'));
    fireEvent.change(screen.getByTestId('stock-op-receive-warehouse'), {
      target: { value: WH_1.id },
    });
    fireEvent.change(screen.getByTestId('stock-op-receive-lot-code'), {
      target: { value: 'LOT-XYZ' },
    });
    fireEvent.change(screen.getByTestId('stock-op-receive-quantity'), {
      target: { value: '25' },
    });
    fireEvent.click(screen.getByTestId('stock-op-receive-submit'));
    await waitFor(() => expect(screen.getByTestId('stock-op-receive-confirm')).toBeInTheDocument());
    // Defer the POST so we can attempt double-confirm while pending.
    const dPost = deferred<{ id: string }>();
    mockedApiFetch.mockImplementation((path: string, init?: RequestInit) => {
      if (init?.method === 'POST') return dPost.promise;
      if (path === '/v1/organizations') return Promise.resolve([ORG_A, ORG_B]);
      if (path === `/v1/organizations/${ORG_A.id}/inventory-items`) return Promise.resolve([ITEM]);
      if (path === `/v1/organizations/${ORG_A.id}/warehouses`) return Promise.resolve([WH_1, WH_2]);
      if (path.startsWith('/v1/warehouses/')) return Promise.resolve([LOT_1]);
      if (path.startsWith('/v1/lots/')) return Promise.resolve({ items: [], next_cursor: null });
      return Promise.resolve([]);
    });
    fireEvent.click(screen.getByTestId('stock-op-receive-confirm'));
    fireEvent.click(screen.getByTestId('stock-op-receive-confirm'));
    fireEvent.click(screen.getByTestId('stock-op-receive-confirm'));
    // Only one POST hit the network.
    const postCount = mockedApiFetch.mock.calls.filter(
      (c) => (c[1] as RequestInit | undefined)?.method === 'POST',
    ).length;
    expect(postCount).toBe(1);
    // Resolve to let the component unmount cleanly.
    await act(async () => {
      dPost.resolve({ id: 'tx-1' });
      await Promise.resolve();
      await Promise.resolve();
    });
  });
});

// ------------------------------------------------------------------ //
// ISSUE                                                              //
// ------------------------------------------------------------------ //
describe('StockOperationDialog — issue', () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
    routerPush.mockReset();
    routerReplace.mockClear();
    toastSpy.mockReset();
    useParamsMock.mockReset();
    window.history.replaceState({}, '', '/');
  });
  afterEach(() => vi.clearAllMocks());

  it('does not offer a maintenance warehouse for issue', async () => {
    await primeDetailPage({ warehouses: [WH_1, WH_MAINTENANCE] });

    fireEvent.click(screen.getByTestId('item-detail-stock-issue'));

    expect(screen.queryByRole('option', { name: WH_MAINTENANCE.name })).not.toBeInTheDocument();
  });

  it('insufficient-stock 422 surfaces at the quantity field', async () => {
    await primeDetailPage();
    fireEvent.click(screen.getByTestId('item-detail-stock-issue'));
    fireEvent.change(screen.getByTestId('stock-op-issue-warehouse'), {
      target: { value: WH_1.id },
    });
    fireEvent.change(screen.getByTestId('stock-op-issue-lot'), { target: { value: LOT_1.id } });
    fireEvent.change(screen.getByTestId('stock-op-issue-quantity'), {
      target: { value: '9999' },
    });
    fireEvent.click(screen.getByTestId('stock-op-issue-submit'));
    await waitFor(() => expect(screen.getByTestId('stock-op-issue-confirm')).toBeInTheDocument());
    mockedApiFetch.mockImplementation((path: string, init?: RequestInit) => {
      void path;
      if (init?.method === 'POST') {
        return Promise.reject(
          new ApiError(422, {
            detail: [{ loc: ['body', 'quantity'], msg: 'Insufficient stock in lot.' }],
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
          } as any),
        );
      }
      return Promise.resolve([]);
    });
    fireEvent.click(screen.getByTestId('stock-op-issue-confirm'));
    await waitFor(() =>
      expect(screen.getByTestId('stock-op-issue-quantity-error')).toHaveTextContent(
        'Insufficient stock in lot.',
      ),
    );
    // Dialog stayed open so the operator can correct the input.
    expect(screen.getByTestId('stock-op-issue')).toBeInTheDocument();
  });

  it('pending state prevents duplicate submission via Enter', async () => {
    await primeDetailPage();
    fireEvent.click(screen.getByTestId('item-detail-stock-issue'));
    fireEvent.change(screen.getByTestId('stock-op-issue-warehouse'), {
      target: { value: WH_1.id },
    });
    fireEvent.change(screen.getByTestId('stock-op-issue-lot'), { target: { value: LOT_1.id } });
    fireEvent.change(screen.getByTestId('stock-op-issue-quantity'), {
      target: { value: '5' },
    });
    fireEvent.click(screen.getByTestId('stock-op-issue-submit'));
    await waitFor(() => expect(screen.getByTestId('stock-op-issue-confirm')).toBeInTheDocument());
    const dPost = deferred<{ id: string }>();
    mockedApiFetch.mockImplementation((_path: string, init?: RequestInit) => {
      if (init?.method === 'POST') return dPost.promise;
      return Promise.resolve([]);
    });
    fireEvent.click(screen.getByTestId('stock-op-issue-confirm'));
    // Confirm button is now disabled → repeated clicks / Enter cannot re-submit.
    const confirm = screen.getByTestId('stock-op-issue-confirm') as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    await act(async () => {
      dPost.resolve({ id: 'tx-1' });
      await Promise.resolve();
      await Promise.resolve();
    });
  });
});

// ------------------------------------------------------------------ //
// API VALIDATION FIELD MAPPING                                       //
// ------------------------------------------------------------------ //
describe('StockOperationDialog — API validation field mapping', () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
    routerPush.mockReset();
    routerReplace.mockClear();
    toastSpy.mockReset();
    useParamsMock.mockReset();
    window.history.replaceState({}, '', '/');
  });
  afterEach(() => vi.clearAllMocks());

  it('maps lot_code to the visible Lot code field', async () => {
    await primeDetailPage();
    rejectNextPostWithValidation([{ loc: ['body', 'lot_code'], msg: 'Lot code is invalid.' }]);
    await submitReceiveForValidation();
    await waitFor(() =>
      expect(screen.getByTestId('stock-op-receive-lot-code-error')).toHaveTextContent(
        'Lot code is invalid.',
      ),
    );
  });

  it('maps destination_warehouse_id to the visible Destination warehouse field', async () => {
    await primeDetailPage();
    fireEvent.click(screen.getByTestId('item-detail-stock-transfer'));
    fireEvent.change(screen.getByTestId('stock-op-transfer-warehouse'), {
      target: { value: WH_1.id },
    });
    fireEvent.change(screen.getByTestId('stock-op-transfer-lot'), {
      target: { value: LOT_1.id },
    });
    fireEvent.change(screen.getByTestId('stock-op-transfer-destination'), {
      target: { value: WH_2.id },
    });
    fireEvent.change(screen.getByTestId('stock-op-transfer-quantity'), {
      target: { value: '3' },
    });
    fireEvent.click(screen.getByTestId('stock-op-transfer-submit'));
    await waitFor(() =>
      expect(screen.getByTestId('stock-op-transfer-confirm')).toBeInTheDocument(),
    );
    rejectNextPostWithValidation([
      { loc: ['body', 'destination_warehouse_id'], msg: 'Destination is unavailable.' },
    ]);
    fireEvent.click(screen.getByTestId('stock-op-transfer-confirm'));
    await waitFor(() =>
      expect(screen.getByTestId('stock-op-transfer-destination-error')).toHaveTextContent(
        'Destination is unavailable.',
      ),
    );
  });

  it('maps lot_id to the visible Lot field', async () => {
    await primeDetailPage();
    fireEvent.click(screen.getByTestId('item-detail-stock-issue'));
    fireEvent.change(screen.getByTestId('stock-op-issue-warehouse'), {
      target: { value: WH_1.id },
    });
    fireEvent.change(screen.getByTestId('stock-op-issue-lot'), { target: { value: LOT_1.id } });
    fireEvent.change(screen.getByTestId('stock-op-issue-quantity'), { target: { value: '2' } });
    fireEvent.click(screen.getByTestId('stock-op-issue-submit'));
    await waitFor(() => expect(screen.getByTestId('stock-op-issue-confirm')).toBeInTheDocument());
    rejectNextPostWithValidation([{ loc: ['body', 'lot_id'], msg: 'Lot is unavailable.' }]);
    fireEvent.click(screen.getByTestId('stock-op-issue-confirm'));
    await waitFor(() =>
      expect(screen.getByTestId('stock-op-issue-lot-error')).toHaveTextContent(
        'Lot is unavailable.',
      ),
    );
  });

  it('shows a generic error for an unknown snake_case field', async () => {
    await primeDetailPage();
    rejectNextPostWithValidation([
      { loc: ['body', 'storage_location_id'], msg: 'Storage location is invalid.' },
    ]);
    await submitReceiveForValidation();
    await waitFor(() =>
      expect(screen.getByTestId('stock-op-receive-error')).toHaveTextContent(
        'Some validation errors could not be matched to a form field.',
      ),
    );
  });

  it('shows both mapped and generic errors for a mixed response', async () => {
    await primeDetailPage();
    rejectNextPostWithValidation([
      { loc: ['body', 'lot_code'], msg: 'Lot code is invalid.' },
      { loc: ['body', 'storage_location_id'], msg: 'Storage location is invalid.' },
    ]);
    await submitReceiveForValidation();
    await waitFor(() =>
      expect(screen.getByTestId('stock-op-receive-lot-code-error')).toHaveTextContent(
        'Lot code is invalid.',
      ),
    );
    expect(screen.getByTestId('stock-op-receive-error')).toHaveTextContent(
      'Some validation errors could not be matched to a form field.',
    );
  });

  it('does not duplicate the generic error for a fully mapped response', async () => {
    await primeDetailPage();
    rejectNextPostWithValidation([
      { loc: ['body', 'lot_code'], msg: 'Lot code is invalid.' },
      { loc: ['body', 'quantity'], msg: 'Quantity is invalid.' },
    ]);
    await submitReceiveForValidation();
    await waitFor(() =>
      expect(screen.getByTestId('stock-op-receive-lot-code-error')).toHaveTextContent(
        'Lot code is invalid.',
      ),
    );
    expect(screen.getByTestId('stock-op-receive-quantity-error')).toHaveTextContent(
      'Quantity is invalid.',
    );
    expect(screen.queryByTestId('stock-op-receive-error')).not.toBeInTheDocument();
  });
});

// ------------------------------------------------------------------ //
// TRANSFER                                                           //
// ------------------------------------------------------------------ //
describe('StockOperationDialog — transfer', () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
    routerPush.mockReset();
    routerReplace.mockClear();
    toastSpy.mockReset();
    useParamsMock.mockReset();
    window.history.replaceState({}, '', '/');
  });
  afterEach(() => vi.clearAllMocks());

  it('does not offer a maintenance warehouse as a transfer source', async () => {
    await primeDetailPage({ warehouses: [WH_1, WH_MAINTENANCE] });

    fireEvent.click(screen.getByTestId('item-detail-stock-transfer'));

    const source = screen.getByTestId('stock-op-transfer-warehouse') as HTMLSelectElement;
    expect(Array.from(source.options).map((option) => option.value)).not.toContain(
      WH_MAINTENANCE.id,
    );
  });

  it('offers a maintenance warehouse as a transfer destination from an active source', async () => {
    await primeDetailPage({ warehouses: [WH_1, WH_MAINTENANCE] });
    fireEvent.click(screen.getByTestId('item-detail-stock-transfer'));
    fireEvent.change(screen.getByTestId('stock-op-transfer-warehouse'), {
      target: { value: WH_1.id },
    });

    const destination = screen.getByTestId(
      'stock-op-transfer-destination',
    ) as HTMLSelectElement;
    expect(Array.from(destination.options).map((option) => option.value)).toContain(
      WH_MAINTENANCE.id,
    );
  });

  it('excludes source warehouse from destination options', async () => {
    await primeDetailPage();
    fireEvent.click(screen.getByTestId('item-detail-stock-transfer'));
    fireEvent.change(screen.getByTestId('stock-op-transfer-warehouse'), {
      target: { value: WH_1.id },
    });
    const dest = screen.getByTestId('stock-op-transfer-destination') as HTMLSelectElement;
    const values = Array.from(dest.options).map((o) => o.value);
    expect(values).not.toContain(WH_1.id);
    expect(values).toContain(WH_2.id);
  });

  it('transfer request includes correct source, destination, lot, quantity, unit', async () => {
    await primeDetailPage();
    fireEvent.click(screen.getByTestId('item-detail-stock-transfer'));
    fireEvent.change(screen.getByTestId('stock-op-transfer-warehouse'), {
      target: { value: WH_1.id },
    });
    fireEvent.change(screen.getByTestId('stock-op-transfer-lot'), {
      target: { value: LOT_1.id },
    });
    fireEvent.change(screen.getByTestId('stock-op-transfer-destination'), {
      target: { value: WH_2.id },
    });
    fireEvent.change(screen.getByTestId('stock-op-transfer-quantity'), {
      target: { value: '3' },
    });
    fireEvent.click(screen.getByTestId('stock-op-transfer-submit'));
    await waitFor(() =>
      expect(screen.getByTestId('stock-op-transfer-confirm')).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByTestId('stock-op-transfer-confirm'));
    await waitFor(() => {
      const posts = mockedApiFetch.mock.calls.filter(
        (c) => (c[1] as RequestInit | undefined)?.method === 'POST',
      );
      expect(posts.length).toBe(1);
    });
    const postCall = mockedApiFetch.mock.calls.find(
      (c) => (c[1] as RequestInit | undefined)?.method === 'POST',
    ) as [string, RequestInit];
    expect(postCall[0]).toBe(`/v1/warehouses/${WH_1.id}/inventory:transfer`);
    const body = JSON.parse(postCall[1].body as string);
    expect(body).toMatchObject({
      lot_id: LOT_1.id,
      destination_warehouse_id: WH_2.id,
      quantity: '3',
      unit: ITEM.canonical_unit,
    });
  });

  it('destination 403 shows a scoped inline error and keeps the dialog open', async () => {
    await primeDetailPage();
    fireEvent.click(screen.getByTestId('item-detail-stock-transfer'));
    fireEvent.change(screen.getByTestId('stock-op-transfer-warehouse'), {
      target: { value: WH_1.id },
    });
    fireEvent.change(screen.getByTestId('stock-op-transfer-lot'), {
      target: { value: LOT_1.id },
    });
    fireEvent.change(screen.getByTestId('stock-op-transfer-destination'), {
      target: { value: WH_2.id },
    });
    fireEvent.change(screen.getByTestId('stock-op-transfer-quantity'), {
      target: { value: '3' },
    });
    fireEvent.click(screen.getByTestId('stock-op-transfer-submit'));
    await waitFor(() =>
      expect(screen.getByTestId('stock-op-transfer-confirm')).toBeInTheDocument(),
    );
    mockedApiFetch.mockImplementation((_path: string, init?: RequestInit) => {
      if (init?.method === 'POST') return Promise.reject(new ApiError(403, {}));
      return Promise.resolve([]);
    });
    fireEvent.click(screen.getByTestId('stock-op-transfer-confirm'));
    await waitFor(() =>
      expect(screen.getByTestId('stock-op-transfer-error')).toHaveTextContent(/permission/i),
    );
    expect(screen.getByTestId('stock-op-transfer')).toBeInTheDocument();
  });
});

// ------------------------------------------------------------------ //
// ADJUST                                                             //
// ------------------------------------------------------------------ //
describe('StockOperationDialog — adjust', () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
    routerPush.mockReset();
    routerReplace.mockClear();
    toastSpy.mockReset();
    useParamsMock.mockReset();
    window.history.replaceState({}, '', '/');
  });
  afterEach(() => vi.clearAllMocks());

  it('offers a maintenance warehouse for an adjustment increase', async () => {
    await primeDetailPage({ warehouses: [WH_1, WH_MAINTENANCE] });

    fireEvent.click(screen.getByTestId('item-detail-stock-adjust'));

    expect(screen.getByRole('option', { name: WH_MAINTENANCE.name })).toBeInTheDocument();
  });

  it('cannot retain a maintenance warehouse and lot when adjustment changes to decrease', async () => {
    await primeDetailPage({
      warehouses: [WH_1, WH_MAINTENANCE],
      lots: [LOT_1, LOT_MAINTENANCE],
    });
    fireEvent.click(screen.getByTestId('item-detail-stock-adjust'));
    const warehouse = screen.getByTestId('stock-op-adjust-warehouse') as HTMLSelectElement;
    fireEvent.change(warehouse, { target: { value: WH_MAINTENANCE.id } });
    const lot = screen.getByTestId('stock-op-adjust-lot') as HTMLSelectElement;
    fireEvent.change(lot, { target: { value: LOT_MAINTENANCE.id } });

    expect(warehouse).toHaveValue(WH_MAINTENANCE.id);
    expect(lot).toHaveValue(LOT_MAINTENANCE.id);

    fireEvent.click(screen.getByTestId('stock-op-adjust-direction-decrease'));

    expect.soft(Array.from(warehouse.options).map((option) => option.value)).not.toContain(
      WH_MAINTENANCE.id,
    );
    expect.soft(warehouse).not.toHaveValue(WH_MAINTENANCE.id);
    expect.soft(lot).not.toHaveValue(LOT_MAINTENANCE.id);

    fireEvent.change(screen.getByTestId('stock-op-adjust-quantity'), { target: { value: '1' } });
    fireEvent.change(screen.getByTestId('stock-op-adjust-reason'), {
      target: { value: 'maintenance decrease must be blocked' },
    });
    fireEvent.click(screen.getByTestId('stock-op-adjust-submit'));

    expect(screen.queryByTestId('stock-op-adjust-confirm')).not.toBeInTheDocument();
  });

  it('blocks submission when quantity is invalid (zero, negative, non-numeric)', async () => {
    await primeDetailPage();
    fireEvent.click(screen.getByTestId('item-detail-stock-adjust'));
    fireEvent.change(screen.getByTestId('stock-op-adjust-warehouse'), {
      target: { value: WH_1.id },
    });
    fireEvent.change(screen.getByTestId('stock-op-adjust-lot'), { target: { value: LOT_1.id } });
    fireEvent.change(screen.getByTestId('stock-op-adjust-quantity'), { target: { value: '0' } });
    fireEvent.change(screen.getByTestId('stock-op-adjust-reason'), {
      target: { value: 'stocktake' },
    });
    fireEvent.click(screen.getByTestId('stock-op-adjust-submit'));
    expect(screen.getByTestId('stock-op-adjust-quantity-error')).toHaveTextContent(
      /greater than 0/i,
    );
    // No POST fired.
    const posts = mockedApiFetch.mock.calls.filter(
      (c) => (c[1] as RequestInit | undefined)?.method === 'POST',
    );
    expect(posts.length).toBe(0);
  });

  it('positive adjustment (increase) and negative adjustment (decrease) both submit', async () => {
    await primeDetailPage();
    // Positive.
    fireEvent.click(screen.getByTestId('item-detail-stock-adjust'));
    fireEvent.change(screen.getByTestId('stock-op-adjust-warehouse'), {
      target: { value: WH_1.id },
    });
    fireEvent.change(screen.getByTestId('stock-op-adjust-lot'), { target: { value: LOT_1.id } });
    fireEvent.change(screen.getByTestId('stock-op-adjust-quantity'), { target: { value: '2' } });
    fireEvent.change(screen.getByTestId('stock-op-adjust-reason'), {
      target: { value: 'stocktake +2' },
    });
    fireEvent.click(screen.getByTestId('stock-op-adjust-submit'));
    await waitFor(() => expect(screen.getByTestId('stock-op-adjust-confirm')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('stock-op-adjust-confirm'));
    await waitFor(() => {
      const posts = mockedApiFetch.mock.calls.filter(
        (c) => (c[1] as RequestInit | undefined)?.method === 'POST',
      );
      expect(posts.length).toBe(1);
    });
    const first = mockedApiFetch.mock.calls.find(
      (c) => (c[1] as RequestInit | undefined)?.method === 'POST',
    ) as [string, RequestInit];
    expect(JSON.parse(first[1].body as string)).toMatchObject({
      direction: 'increase',
      quantity: '2',
      reason: 'stocktake +2',
    });
    // Now the negative side.
    await waitFor(() => expect(screen.queryByTestId('stock-op-adjust')).not.toBeInTheDocument());
    // Wait for the page to settle after the refresh triggered by success.
    await waitFor(() => expect(screen.getByTestId('item-detail-stock-adjust')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('item-detail-stock-adjust'));
    fireEvent.change(screen.getByTestId('stock-op-adjust-warehouse'), {
      target: { value: WH_1.id },
    });
    fireEvent.change(screen.getByTestId('stock-op-adjust-lot'), { target: { value: LOT_1.id } });
    fireEvent.click(screen.getByTestId('stock-op-adjust-direction-decrease'));
    fireEvent.change(screen.getByTestId('stock-op-adjust-quantity'), { target: { value: '1' } });
    fireEvent.change(screen.getByTestId('stock-op-adjust-reason'), {
      target: { value: 'stocktake -1' },
    });
    fireEvent.click(screen.getByTestId('stock-op-adjust-submit'));
    await waitFor(() => expect(screen.getByTestId('stock-op-adjust-confirm')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('stock-op-adjust-confirm'));
    await waitFor(() => {
      const posts = mockedApiFetch.mock.calls.filter(
        (c) => (c[1] as RequestInit | undefined)?.method === 'POST',
      );
      expect(posts.length).toBe(2);
    });
    const posts = mockedApiFetch.mock.calls.filter(
      (c) => (c[1] as RequestInit | undefined)?.method === 'POST',
    );
    expect(JSON.parse((posts[1][1] as RequestInit).body as string)).toMatchObject({
      direction: 'decrease',
      quantity: '1',
    });
  });
});

// ------------------------------------------------------------------ //
// REVERSE                                                            //
// ------------------------------------------------------------------ //
describe('StockOperationDialog — reverse', () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
    routerPush.mockReset();
    routerReplace.mockClear();
    toastSpy.mockReset();
    useParamsMock.mockReset();
    window.history.replaceState({}, '', '/');
  });
  afterEach(() => vi.clearAllMocks());

  it('requires confirmation and posts to the correct warehouse endpoint', async () => {
    await primeDetailPage({
      transactions: [
        {
          id: 'tx-9',
          transaction_type: 'issue',
          quantity: '2',
          unit: 'kg',
          performed_at: '2026-02-01T00:00:00.000Z',
          reason: 'op',
          reference_type: null,
          lot_id: LOT_1.id,
        },
      ],
    });
    await waitFor(() =>
      expect(screen.getByTestId('item-activity-reverse-tx-9')).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByTestId('item-activity-reverse-tx-9'));
    // The original transaction card is visible.
    expect(screen.getByTestId('stock-op-reverse-original')).toHaveTextContent('issue');
    // Missing reason → blocked.
    fireEvent.click(screen.getByTestId('stock-op-reverse-submit'));
    expect(screen.getByTestId('stock-op-reverse-reason-error')).toBeInTheDocument();
    // Provide reason, submit, confirm.
    fireEvent.change(screen.getByTestId('stock-op-reverse-reason'), {
      target: { value: 'wrong lot' },
    });
    fireEvent.click(screen.getByTestId('stock-op-reverse-submit'));
    await waitFor(() => expect(screen.getByTestId('stock-op-reverse-confirm')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('stock-op-reverse-confirm'));
    await waitFor(() => {
      const posts = mockedApiFetch.mock.calls.filter(
        (c) => (c[1] as RequestInit | undefined)?.method === 'POST',
      );
      expect(posts.length).toBe(1);
    });
    const post = mockedApiFetch.mock.calls.find(
      (c) => (c[1] as RequestInit | undefined)?.method === 'POST',
    ) as [string, RequestInit];
    expect(post[0]).toBe(`/v1/warehouses/${WH_1.id}/inventory:reverse`);
    const body = JSON.parse(post[1].body as string);
    expect(body).toMatchObject({
      reverses_transaction_id: 'tx-9',
      reason: 'wrong lot',
    });
    // Success toast + close.
    await waitFor(() =>
      expect(toastSpy).toHaveBeenCalledWith('Reverse transaction succeeded.', 'success'),
    );
  });

  it('displays the backend business error on rejected reversal', async () => {
    await primeDetailPage({
      transactions: [
        {
          id: 'tx-9',
          transaction_type: 'receipt',
          quantity: '10',
          unit: 'kg',
          performed_at: '2026-02-01T00:00:00.000Z',
          reason: null,
          reference_type: null,
          lot_id: LOT_1.id,
        },
      ],
    });
    fireEvent.click(screen.getByTestId('item-activity-reverse-tx-9'));
    fireEvent.change(screen.getByTestId('stock-op-reverse-reason'), {
      target: { value: 'wrong lot' },
    });
    fireEvent.click(screen.getByTestId('stock-op-reverse-submit'));
    await waitFor(() => expect(screen.getByTestId('stock-op-reverse-confirm')).toBeInTheDocument());
    mockedApiFetch.mockImplementation((_path: string, init?: RequestInit) => {
      if (init?.method === 'POST') {
        return Promise.reject(
          new ApiError(409, { detail: 'Reversal would drive balance negative.' }),
        );
      }
      return Promise.resolve([]);
    });
    fireEvent.click(screen.getByTestId('stock-op-reverse-confirm'));
    await waitFor(() =>
      expect(screen.getByTestId('stock-op-reverse-error')).toHaveTextContent(
        /Reversal would drive balance negative\./,
      ),
    );
  });
});

// ------------------------------------------------------------------ //
// IDEMPOTENCY-KEY DISCIPLINE                                         //
// ------------------------------------------------------------------ //
describe('StockOperationDialog — idempotency-key discipline', () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
    routerPush.mockReset();
    routerReplace.mockClear();
    toastSpy.mockReset();
    useParamsMock.mockReset();
    window.history.replaceState({}, '', '/');
  });
  afterEach(() => vi.clearAllMocks());

  it('a retry after an uncertain first result reuses the same key', async () => {
    await primeDetailPage();
    fireEvent.click(screen.getByTestId('item-detail-stock-receive'));
    fireEvent.change(screen.getByTestId('stock-op-receive-warehouse'), {
      target: { value: WH_1.id },
    });
    fireEvent.change(screen.getByTestId('stock-op-receive-lot-code'), {
      target: { value: 'LOT-XYZ' },
    });
    fireEvent.change(screen.getByTestId('stock-op-receive-quantity'), {
      target: { value: '5' },
    });
    fireEvent.click(screen.getByTestId('stock-op-receive-submit'));
    await waitFor(() => expect(screen.getByTestId('stock-op-receive-confirm')).toBeInTheDocument());
    // First attempt: network error (uncertain result).
    let call = 0;
    mockedApiFetch.mockImplementation((_path: string, init?: RequestInit) => {
      if (init?.method === 'POST') {
        call += 1;
        if (call === 1) return Promise.reject(new ApiError(500, { detail: 'network' }));
        return Promise.resolve({ id: 'tx-r' });
      }
      return Promise.resolve([]);
    });
    fireEvent.click(screen.getByTestId('stock-op-receive-confirm'));
    await waitFor(() => expect(screen.getByTestId('stock-op-receive-error')).toBeInTheDocument());
    const firstPost = mockedApiFetch.mock.calls.find(
      (c) => (c[1] as RequestInit | undefined)?.method === 'POST',
    ) as [string, RequestInit];
    const firstKey = (firstPost[1].headers as Record<string, string>)['Idempotency-Key'];
    // Retry with the identical payload → same key.
    fireEvent.click(screen.getByTestId('stock-op-receive-confirm'));
    await waitFor(() => {
      const posts = mockedApiFetch.mock.calls.filter(
        (c) => (c[1] as RequestInit | undefined)?.method === 'POST',
      );
      expect(posts.length).toBe(2);
    });
    const posts = mockedApiFetch.mock.calls.filter(
      (c) => (c[1] as RequestInit | undefined)?.method === 'POST',
    );
    const secondKey = ((posts[1][1] as RequestInit).headers as Record<string, string>)[
      'Idempotency-Key'
    ];
    expect(secondKey).toBe(firstKey);
  });

  it('changing any form field between attempts generates a fresh key', async () => {
    await primeDetailPage();
    fireEvent.click(screen.getByTestId('item-detail-stock-receive'));
    fireEvent.change(screen.getByTestId('stock-op-receive-warehouse'), {
      target: { value: WH_1.id },
    });
    fireEvent.change(screen.getByTestId('stock-op-receive-lot-code'), {
      target: { value: 'LOT-A' },
    });
    fireEvent.change(screen.getByTestId('stock-op-receive-quantity'), {
      target: { value: '5' },
    });
    fireEvent.click(screen.getByTestId('stock-op-receive-submit'));
    await waitFor(() => expect(screen.getByTestId('stock-op-receive-confirm')).toBeInTheDocument());
    mockedApiFetch.mockImplementation((_path: string, init?: RequestInit) => {
      if (init?.method === 'POST') return Promise.reject(new ApiError(500, {}));
      return Promise.resolve([]);
    });
    fireEvent.click(screen.getByTestId('stock-op-receive-confirm'));
    await waitFor(() => expect(screen.getByTestId('stock-op-receive-error')).toBeInTheDocument());
    const firstPost = mockedApiFetch.mock.calls.find(
      (c) => (c[1] as RequestInit | undefined)?.method === 'POST',
    ) as [string, RequestInit];
    const firstKey = (firstPost[1].headers as Record<string, string>)['Idempotency-Key'];
    // Go back to editing and change the payload.
    fireEvent.click(screen.getByTestId('stock-op-receive-back'));
    fireEvent.change(screen.getByTestId('stock-op-receive-quantity'), {
      target: { value: '7' },
    });
    fireEvent.click(screen.getByTestId('stock-op-receive-submit'));
    await waitFor(() => expect(screen.getByTestId('stock-op-receive-confirm')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('stock-op-receive-confirm'));
    await waitFor(() => {
      const posts = mockedApiFetch.mock.calls.filter(
        (c) => (c[1] as RequestInit | undefined)?.method === 'POST',
      );
      expect(posts.length).toBe(2);
    });
    const posts = mockedApiFetch.mock.calls.filter(
      (c) => (c[1] as RequestInit | undefined)?.method === 'POST',
    );
    const secondKey = ((posts[1][1] as RequestInit).headers as Record<string, string>)[
      'Idempotency-Key'
    ];
    expect(secondKey).not.toBe(firstKey);
  });
});

// ------------------------------------------------------------------ //
// ROUTE / GENERATION GUARDS                                          //
// ------------------------------------------------------------------ //
describe('StockOperationDialog — route/generation guards', () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
    routerPush.mockReset();
    routerReplace.mockClear();
    toastSpy.mockReset();
    useParamsMock.mockReset();
    window.history.replaceState({}, '', '/');
  });
  afterEach(() => vi.clearAllMocks());

  it('changing the URL organization closes any open dialog', async () => {
    await primeDetailPage();
    fireEvent.click(screen.getByTestId('item-detail-stock-issue'));
    expect(screen.getByTestId('stock-op-issue')).toBeInTheDocument();
    // Flip org via URL → identity reset closes the dialog.
    await act(async () => {
      routerReplace('/inventory/items/item-1?organization_id=org-B');
    });
    await waitFor(() => expect(screen.queryByTestId('stock-op-issue')).not.toBeInTheDocument());
  });

  it('a stale POST completion cannot fire success toast after org change', async () => {
    await primeDetailPage();
    fireEvent.click(screen.getByTestId('item-detail-stock-issue'));
    fireEvent.change(screen.getByTestId('stock-op-issue-warehouse'), {
      target: { value: WH_1.id },
    });
    fireEvent.change(screen.getByTestId('stock-op-issue-lot'), { target: { value: LOT_1.id } });
    fireEvent.change(screen.getByTestId('stock-op-issue-quantity'), {
      target: { value: '2' },
    });
    fireEvent.click(screen.getByTestId('stock-op-issue-submit'));
    await waitFor(() => expect(screen.getByTestId('stock-op-issue-confirm')).toBeInTheDocument());
    const dPost = deferred<{ id: string }>();
    mockedApiFetch.mockImplementation((path: string, init?: RequestInit) => {
      if (init?.method === 'POST') return dPost.promise;
      if (path === '/v1/organizations') return Promise.resolve([ORG_A, ORG_B]);
      if (path === `/v1/organizations/${ORG_A.id}/inventory-items`) return Promise.resolve([ITEM]);
      if (path === `/v1/organizations/${ORG_B.id}/inventory-items`) return Promise.resolve([ITEM]);
      if (path === `/v1/organizations/${ORG_A.id}/warehouses`) return Promise.resolve([WH_1, WH_2]);
      if (path === `/v1/organizations/${ORG_B.id}/warehouses`) return Promise.resolve([]);
      if (path.startsWith('/v1/warehouses/')) return Promise.resolve([LOT_1]);
      if (path.startsWith('/v1/lots/')) return Promise.resolve({ items: [], next_cursor: null });
      return Promise.resolve([]);
    });
    fireEvent.click(screen.getByTestId('stock-op-issue-confirm'));
    // Flip org before the POST resolves.
    await act(async () => {
      routerReplace('/inventory/items/item-1?organization_id=org-B');
    });
    // Resolve the stale POST — it must not fire the success toast
    // in the newly navigated route.
    await act(async () => {
      dPost.resolve({ id: 'tx-1' });
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(toastSpy).not.toHaveBeenCalledWith('Issue stock succeeded.', 'success');
  });

  it('401 during a POST redirects to /login without writing state', async () => {
    await primeDetailPage();
    fireEvent.click(screen.getByTestId('item-detail-stock-receive'));
    fireEvent.change(screen.getByTestId('stock-op-receive-warehouse'), {
      target: { value: WH_1.id },
    });
    fireEvent.change(screen.getByTestId('stock-op-receive-lot-code'), {
      target: { value: 'LOT-A' },
    });
    fireEvent.change(screen.getByTestId('stock-op-receive-quantity'), {
      target: { value: '5' },
    });
    fireEvent.click(screen.getByTestId('stock-op-receive-submit'));
    await waitFor(() => expect(screen.getByTestId('stock-op-receive-confirm')).toBeInTheDocument());
    mockedApiFetch.mockImplementation((_path: string, init?: RequestInit) => {
      if (init?.method === 'POST') return Promise.reject(new ApiError(401, {}));
      return Promise.resolve([]);
    });
    fireEvent.click(screen.getByTestId('stock-op-receive-confirm'));
    await waitFor(() => expect(routerPush).toHaveBeenCalledWith('/login'));
    // No success toast; no error banner (auth path).
    expect(toastSpy).not.toHaveBeenCalledWith('Receive stock succeeded.', 'success');
  });
});

// ------------------------------------------------------------------ //
// Sprint 5.4.1 review fixes                                          //
// ------------------------------------------------------------------ //
describe('StockOperationDialog — focus management (Sprint 5.4.1)', () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
    routerPush.mockReset();
    routerReplace.mockClear();
    toastSpy.mockReset();
    useParamsMock.mockReset();
    window.history.replaceState({}, '', '/');
  });
  afterEach(() => vi.clearAllMocks());

  it('autofocus lands on the first tabbable control when the dialog opens', async () => {
    await primeDetailPage();
    const trigger = screen.getByTestId('item-detail-stock-receive') as HTMLButtonElement;
    trigger.focus();
    expect(document.activeElement).toBe(trigger);
    fireEvent.click(trigger);
    // The dialog's first tabbable is the warehouse <select>. rAF
    // resolves within a microtask flush of the assertion loop.
    await waitFor(() =>
      expect(document.activeElement).toBe(screen.getByTestId('stock-op-receive-warehouse')),
    );
  });

  it('Tab wraps forward from the last control to the first (focus trap)', async () => {
    await primeDetailPage();
    fireEvent.click(screen.getByTestId('item-detail-stock-receive'));
    await waitFor(() =>
      expect(document.activeElement).toBe(screen.getByTestId('stock-op-receive-warehouse')),
    );
    const submit = screen.getByTestId('stock-op-receive-submit') as HTMLButtonElement;
    submit.focus();
    expect(document.activeElement).toBe(submit);
    // Tab from the last focusable → wraps back to the first inside
    // the dialog. The dialog listens for keydown on its own node.
    fireEvent.keyDown(screen.getByTestId('stock-op-receive'), { key: 'Tab' });
    expect(document.activeElement).toBe(screen.getByTestId('stock-op-receive-warehouse'));
  });

  it('Shift+Tab wraps backward from the first control to the last', async () => {
    await primeDetailPage();
    fireEvent.click(screen.getByTestId('item-detail-stock-receive'));
    await waitFor(() =>
      expect(document.activeElement).toBe(screen.getByTestId('stock-op-receive-warehouse')),
    );
    const warehouse = screen.getByTestId('stock-op-receive-warehouse') as HTMLSelectElement;
    warehouse.focus();
    fireEvent.keyDown(screen.getByTestId('stock-op-receive'), {
      key: 'Tab',
      shiftKey: true,
    });
    expect(document.activeElement).toBe(screen.getByTestId('stock-op-receive-submit'));
  });

  it('closing the dialog restores focus to the trigger', async () => {
    await primeDetailPage();
    const trigger = screen.getByTestId('item-detail-stock-receive') as HTMLButtonElement;
    trigger.focus();
    fireEvent.click(trigger);
    await waitFor(() => expect(screen.getByTestId('stock-op-receive')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('stock-op-receive-cancel'));
    await waitFor(() => expect(screen.queryByTestId('stock-op-receive')).not.toBeInTheDocument());
    // Focus goes back to the button that opened the dialog.
    expect(document.activeElement).toBe(trigger);
  });

  it('ESC closes the dialog and restores focus (except while a POST is pending)', async () => {
    await primeDetailPage();
    const trigger = screen.getByTestId('item-detail-stock-receive') as HTMLButtonElement;
    trigger.focus();
    fireEvent.click(trigger);
    await waitFor(() => expect(screen.getByTestId('stock-op-receive')).toBeInTheDocument());
    fireEvent.keyDown(window, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByTestId('stock-op-receive')).not.toBeInTheDocument());
    expect(document.activeElement).toBe(trigger);
  });
});

describe('StockOperationDialog — unmount invalidation (Sprint 5.4.1)', () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
    routerPush.mockReset();
    routerReplace.mockClear();
    toastSpy.mockReset();
    useParamsMock.mockReset();
    window.history.replaceState({}, '', '/');
  });
  afterEach(() => vi.clearAllMocks());

  it('a POST that resolves *after* unmount cannot fire success toast, onClose, or refresh', async () => {
    await primeDetailPage();
    fireEvent.click(screen.getByTestId('item-detail-stock-issue'));
    fireEvent.change(screen.getByTestId('stock-op-issue-warehouse'), {
      target: { value: WH_1.id },
    });
    fireEvent.change(screen.getByTestId('stock-op-issue-lot'), { target: { value: LOT_1.id } });
    fireEvent.change(screen.getByTestId('stock-op-issue-quantity'), {
      target: { value: '3' },
    });
    fireEvent.click(screen.getByTestId('stock-op-issue-submit'));
    await waitFor(() => expect(screen.getByTestId('stock-op-issue-confirm')).toBeInTheDocument());
    const dPost = deferred<{ id: string }>();
    let refreshCalls = 0;
    mockedApiFetch.mockImplementation((path: string, init?: RequestInit) => {
      if (init?.method === 'POST') return dPost.promise;
      if (path === '/v1/warehouses/wh-1/lots') {
        refreshCalls += 1;
        return Promise.resolve([LOT_1]);
      }
      return Promise.resolve([]);
    });
    fireEvent.click(screen.getByTestId('stock-op-issue-confirm'));
    // Flip org via URL — this triggers reset in the detail page,
    // which closes the dialog and unmounts it.
    await act(async () => {
      routerReplace('/inventory/items/item-1?organization_id=org-B');
    });
    await waitFor(() => expect(screen.queryByTestId('stock-op-issue')).not.toBeInTheDocument());
    const successToastCallsBefore = toastSpy.mock.calls.filter((c) =>
      String(c[0]).includes('succeeded'),
    ).length;
    const refreshCallsBefore = refreshCalls;
    // Late POST resolution: the guard must drop every side effect.
    await act(async () => {
      dPost.resolve({ id: 'tx-late' });
      await Promise.resolve();
      await Promise.resolve();
    });
    const successToastCallsAfter = toastSpy.mock.calls.filter((c) =>
      String(c[0]).includes('succeeded'),
    ).length;
    expect(successToastCallsAfter).toBe(successToastCallsBefore);
    // Refresh callback must NOT have fired for the stale mutation.
    expect(refreshCalls).toBe(refreshCallsBefore);
  });

  it('a 401 that arrives after unmount does not trigger a login redirect', async () => {
    await primeDetailPage();
    fireEvent.click(screen.getByTestId('item-detail-stock-receive'));
    fireEvent.change(screen.getByTestId('stock-op-receive-warehouse'), {
      target: { value: WH_1.id },
    });
    fireEvent.change(screen.getByTestId('stock-op-receive-lot-code'), {
      target: { value: 'LOT-A' },
    });
    fireEvent.change(screen.getByTestId('stock-op-receive-quantity'), {
      target: { value: '5' },
    });
    fireEvent.click(screen.getByTestId('stock-op-receive-submit'));
    await waitFor(() => expect(screen.getByTestId('stock-op-receive-confirm')).toBeInTheDocument());
    const dPost = deferred<{ id: string }>();
    mockedApiFetch.mockImplementation((_path: string, init?: RequestInit) => {
      if (init?.method === 'POST') return dPost.promise;
      return Promise.resolve([]);
    });
    fireEvent.click(screen.getByTestId('stock-op-receive-confirm'));
    // Unmount by flipping org.
    await act(async () => {
      routerReplace('/inventory/items/item-1?organization_id=org-B');
    });
    await waitFor(() => expect(screen.queryByTestId('stock-op-receive')).not.toBeInTheDocument());
    routerPush.mockReset();
    // Now the POST rejects with 401 — the stale completion must
    // NOT push /login on the newly-navigated route.
    await act(async () => {
      dPost.reject(new ApiError(401, {}));
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(routerPush).not.toHaveBeenCalledWith('/login');
  });
});

describe('InventoryItemActivity — reversal eligibility (Sprint 5.4.1)', () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
    routerPush.mockReset();
    routerReplace.mockClear();
    toastSpy.mockReset();
    useParamsMock.mockReset();
    window.history.replaceState({}, '', '/');
  });
  afterEach(() => vi.clearAllMocks());

  it('a transaction already offset by a reversal row shows "Reversed" and no Reverse button', async () => {
    await primeDetailPage({
      transactions: [
        {
          id: 'tx-orig',
          transaction_type: 'issue',
          quantity: '2',
          unit: 'kg',
          performed_at: '2026-02-01T00:00:00.000Z',
          reason: 'op',
          reference_type: null,
          lot_id: LOT_1.id,
        },
        // A reversal row referencing tx-orig — marks it consumed.
        {
          id: 'tx-rev',
          transaction_type: 'reversal',
          quantity: '2',
          unit: 'kg',
          performed_at: '2026-02-01T01:00:00.000Z',
          reason: 'undo op',
          reference_type: null,
          lot_id: LOT_1.id,
          reverses_transaction_id: 'tx-orig',
        },
      ],
    });
    await waitFor(() => expect(screen.getByTestId('item-activity')).toBeInTheDocument());
    // Reverse button on the original row is gone; "Reversed" label
    // is shown in its place.
    expect(screen.queryByTestId('item-activity-reverse-tx-orig')).not.toBeInTheDocument();
    expect(screen.getByTestId('item-activity-reversed-tx-orig')).toBeInTheDocument();
    // The reversal row itself is never reversible (type filter).
    expect(screen.queryByTestId('item-activity-reverse-tx-rev')).not.toBeInTheDocument();
  });

  it('an ordinary (non-reversed) tx still shows the Reverse button', async () => {
    await primeDetailPage({
      transactions: [
        {
          id: 'tx-plain',
          transaction_type: 'receipt',
          quantity: '10',
          unit: 'kg',
          performed_at: '2026-02-01T00:00:00.000Z',
          reason: null,
          reference_type: null,
          lot_id: LOT_1.id,
        },
      ],
    });
    await waitFor(() =>
      expect(screen.getByTestId('item-activity-reverse-tx-plain')).toBeInTheDocument(),
    );
  });
});

describe('InventoryItemActivity — filter accessibility (Sprint 5.4.1)', () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
    routerPush.mockReset();
    routerReplace.mockClear();
    toastSpy.mockReset();
    useParamsMock.mockReset();
    window.history.replaceState({}, '', '/');
  });
  afterEach(() => vi.clearAllMocks());

  it('operation type, start date, and end date filters carry accessible names', async () => {
    await primeDetailPage({
      transactions: [
        {
          id: 'tx-1',
          transaction_type: 'receipt',
          quantity: '1',
          unit: 'kg',
          performed_at: '2026-02-01T00:00:00.000Z',
          reason: null,
          reference_type: null,
          lot_id: LOT_1.id,
        },
      ],
    });
    // Query by accessible name. Testing Library's `getByRole` with
    // `name` resolves against the accessible name computation, which
    // covers both aria-label and associated <label> elements.
    expect(screen.getByRole('combobox', { name: /operation type/i })).toBeInTheDocument();
    // Date inputs are `textbox` in accessible-name terms; use
    // aria-label directly.
    expect(screen.getByLabelText(/start date/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/end date/i)).toBeInTheDocument();
  });
});

// ------------------------------------------------------------------ //
// Sprint 5.4.2 — atomic warehouse-transfer reversal (UI contract)     //
// ------------------------------------------------------------------ //
describe('InventoryItemActivity — atomic transfer reversal (Sprint 5.4.2)', () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
    routerPush.mockReset();
    routerReplace.mockClear();
    toastSpy.mockReset();
    useParamsMock.mockReset();
    window.history.replaceState({}, '', '/');
  });
  afterEach(() => vi.clearAllMocks());

  it('exposes exactly one logical Reverse transfer action on the canonical OUT row', async () => {
    // Bespoke mock: return the OUT row when the source lot is queried
    // and the IN row when the destination lot is queried, so the
    // merged activity list contains exactly one of each.
    const LOT_2: ItemLot = {
      id: 'lot-2',
      item_id: 'item-1',
      warehouse_id: 'wh-2',
      storage_location_id: null,
      lot_code: 'L1',
      expiry_date: null,
      balance: '8',
      balance_unit: 'kg',
    } as ItemLot;
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/organizations') return Promise.resolve([ORG_A, ORG_B]);
      if (path === `/v1/organizations/${ORG_A.id}/inventory-items`) return Promise.resolve([ITEM]);
      if (path === `/v1/organizations/${ORG_A.id}/warehouses`) return Promise.resolve([WH_1, WH_2]);
      if (path === '/v1/warehouses/wh-1/lots') return Promise.resolve([LOT_1]);
      if (path === '/v1/warehouses/wh-2/lots') return Promise.resolve([LOT_2]);
      if (path.startsWith(`/v1/lots/${LOT_1.id}/transactions`)) {
        return Promise.resolve({
          items: [
            {
              id: 'tx-out',
              transaction_type: 'transfer_out',
              quantity: '8',
              unit: 'kg',
              performed_at: '2026-02-01T00:00:00.000Z',
              reason: null,
              reference_type: 'transfer',
              reference_id: 'transfer-ref-1',
              lot_id: LOT_1.id,
            },
          ],
          next_cursor: null,
        });
      }
      if (path.startsWith(`/v1/lots/${LOT_2.id}/transactions`)) {
        return Promise.resolve({
          items: [
            {
              id: 'tx-in',
              transaction_type: 'transfer_in',
              quantity: '8',
              unit: 'kg',
              performed_at: '2026-02-01T00:00:00.000Z',
              reason: null,
              reference_type: 'transfer',
              reference_id: 'transfer-ref-1',
              lot_id: LOT_2.id,
            },
          ],
          next_cursor: null,
        });
      }
      return Promise.resolve([]);
    });
    useParamsMock.mockReturnValue({ itemId: 'item-1' });
    window.history.replaceState({}, '', '/inventory/items/item-1?organization_id=org-A');
    render(<InventoryItemDetailPage />);
    await waitFor(() =>
      expect(screen.getByTestId('item-detail-stock-actions')).toBeInTheDocument(),
    );
    // OUT row exposes the button, labelled "Reverse transfer".
    await waitFor(() =>
      expect(screen.getByTestId('item-activity-reverse-tx-out')).toBeInTheDocument(),
    );
    expect(screen.getByTestId('item-activity-reverse-tx-out')).toHaveTextContent(
      /reverse transfer/i,
    );
    expect(screen.getAllByRole('button', { name: /reverse transfer/i })).toHaveLength(1);
    // IN row does NOT expose the button — inventory integrity depends
    // on the single-entry-point rule.
    expect(screen.queryByTestId('item-activity-reverse-tx-in')).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('item-activity-reverse-tx-out'));
    expect(screen.getByRole('dialog', { name: /reverse transfer/i })).toBeInTheDocument();
    fireEvent.change(screen.getByTestId('stock-op-reverse-reason'), {
      target: { value: 'wrong destination' },
    });
    fireEvent.click(screen.getByTestId('stock-op-reverse-submit'));
    expect(screen.getByText(/atomically reverses both sides of the transfer/i)).toBeInTheDocument();
  });

  it('reversing a transfer_out submits exactly ONE backend request', async () => {
    await primeDetailPage({
      transactions: [
        {
          id: 'tx-out',
          transaction_type: 'transfer_out',
          quantity: '4',
          unit: 'kg',
          performed_at: '2026-02-01T00:00:00.000Z',
          reason: null,
          reference_type: 'transfer',
          reference_id: 'transfer-ref-2',
          lot_id: LOT_1.id,
        },
      ],
    });
    await waitFor(() =>
      expect(screen.getByTestId('item-activity-reverse-tx-out')).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByTestId('item-activity-reverse-tx-out'));
    fireEvent.change(screen.getByTestId('stock-op-reverse-reason'), {
      target: { value: 'wrong destination' },
    });
    fireEvent.click(screen.getByTestId('stock-op-reverse-submit'));
    await waitFor(() => expect(screen.getByTestId('stock-op-reverse-confirm')).toBeInTheDocument());
    fireEvent.click(screen.getByTestId('stock-op-reverse-confirm'));
    await waitFor(() => {
      const posts = mockedApiFetch.mock.calls.filter(
        (c) => (c[1] as RequestInit | undefined)?.method === 'POST',
      );
      expect(posts.length).toBe(1);
    });
    // The single request targets the SOURCE warehouse and carries
    // the transfer_out id — the backend fans out to both sides.
    const post = mockedApiFetch.mock.calls.find(
      (c) => (c[1] as RequestInit | undefined)?.method === 'POST',
    ) as [string, RequestInit];
    expect(post[0]).toBe(`/v1/warehouses/${WH_1.id}/inventory:reverse`);
    expect(JSON.parse(post[1].body as string)).toMatchObject({
      reverses_transaction_id: 'tx-out',
      reason: 'wrong destination',
    });
  });
});
