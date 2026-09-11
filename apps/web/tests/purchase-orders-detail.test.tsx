import { beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';

const { routerPush, stableRouter, useParamsMock, renderedDetailIds, renderedTransitionPoIds } =
  vi.hoisted(() => {
    const push = vi.fn();
    return {
      routerPush: push,
      stableRouter: { push, replace: vi.fn(), back: vi.fn() },
      useParamsMock: vi.fn(() => ({ purchaseOrderId: 'po-1' })),
      renderedDetailIds: [] as string[],
      renderedTransitionPoIds: [] as string[][],
    };
  });

vi.mock('next/navigation', () => ({
  useRouter: () => stableRouter,
  usePathname: () => `/purchase-orders/${useParamsMock().purchaseOrderId}`,
  useParams: () => useParamsMock(),
}));

vi.mock('next/link', () => ({
  default: ({ href, children, ...rest }: any) => (
    <a href={typeof href === 'string' ? href : '#'} {...rest}>
      {children}
    </a>
  ),
}));

vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api');
  return { ...actual, apiFetch: vi.fn() };
});

vi.mock('@/components/purchase-orders/PurchaseOrderDetail', async () => {
  const actual = await vi.importActual<
    typeof import('@/components/purchase-orders/PurchaseOrderDetail')
  >('@/components/purchase-orders/PurchaseOrderDetail');
  return {
    PurchaseOrderDetail: (props: Parameters<typeof actual.PurchaseOrderDetail>[0]) => {
      renderedDetailIds.push(props.purchaseOrder.id);
      return actual.PurchaseOrderDetail(props);
    },
  };
});

vi.mock('@/components/purchase-orders/PurchaseOrderTransitionHistory', async () => {
  const actual = await vi.importActual<
    typeof import('@/components/purchase-orders/PurchaseOrderTransitionHistory')
  >('@/components/purchase-orders/PurchaseOrderTransitionHistory');
  return {
    PurchaseOrderTransitionHistory: (
      props: Parameters<typeof actual.PurchaseOrderTransitionHistory>[0],
    ) => {
      renderedTransitionPoIds.push(props.transitions.map((item) => item.purchase_order_id));
      return actual.PurchaseOrderTransitionHistory(props);
    },
  };
});

vi.mock('@/components/purchase-orders/PurchaseReceiptForm', () => ({
  PurchaseReceiptForm: ({
    open,
    onClose,
    onCompleted,
  }: {
    open: boolean;
    onClose: () => void;
    onCompleted: (replayed: boolean) => void;
  }) =>
    open ? (
      <div role="dialog" aria-label="Mock receive dialog">
        <button onClick={onClose}>Cancel receipt</button>
        <button onClick={() => onCompleted(false)}>Complete created receipt</button>
        <button onClick={() => onCompleted(true)}>Complete replayed receipt</button>
      </div>
    ) : null,
}));

import { ApiError, apiFetch } from '@/lib/api';
import PurchaseOrderDetailPage from '@/app/purchase-orders/[purchaseOrderId]/page';
import type { PurchaseOrder, PurchaseOrderTransition } from '@/lib/purchase-orders';

const mockedApiFetch = vi.mocked(apiFetch);
const USER = {
  id: 'user-1',
  email: 'reader@example.test',
  full_name: 'Reader',
  is_active: true,
  is_verified: true,
  is_superuser: false,
  permissions: [],
  permission_scopes: [
    { organization_id: 'org-1', farm_id: null, permissions: ['purchase_order.read'] },
  ],
};

function makePO(overrides: Partial<PurchaseOrder> = {}): PurchaseOrder {
  return {
    id: 'po-1',
    organization_id: 'org-1',
    farm_id: 'farm-1',
    business_partner_id: 'bp-1',
    po_number: 'PO-2026-000001',
    supplier_reference: 'REF-77',
    status: 'APPROVED',
    currency_code: 'USD',
    order_date: '2026-08-01',
    expected_delivery_date: '2026-08-10',
    delivery_address: {
      line1: '1 Farm Road',
      line2: null,
      city: 'Accra',
      region: null,
      postal_code: null,
      country_code: 'GH',
    },
    notes: 'Frozen PO notes',
    supplier_code: 'ACME',
    supplier_legal_name: 'Acme Legal Snapshot',
    supplier_trading_name: 'Acme Snapshot',
    version: 4,
    created_by_id: USER.id,
    submitted_by_id: 'user-2',
    submitted_at: '2026-08-01T11:00:00Z',
    approved_by_id: USER.id,
    approved_at: '2026-08-02T10:00:00Z',
    rejected_by_id: null,
    rejected_at: null,
    cancelled_by_id: null,
    cancelled_at: null,
    created_at: '2026-08-01T10:00:00Z',
    updated_at: '2026-08-02T10:00:00Z',
    subtotal: '99999999999999999899000000.000000',
    lines: [
      {
        id: 'line-1',
        line_number: 1,
        inventory_item_id: 'item-1',
        item_code: 'ITEM-SNAPSHOT',
        item_name: 'Frozen Item Snapshot',
        item_sku: 'SKU-SNAPSHOT',
        description: 'Frozen description',
        line_note: null,
        ordered_quantity: '999999999999.999999',
        ordered_unit: 'kg',
        canonical_unit: 'kg',
        ordered_quantity_canonical: '999999999999.999999',
        received_quantity: '0.000000',
        received_quantity_canonical: '0.000000',
        unit_price: '99999999999999.999999',
        extended_amount: '99999999999999999899000000.000000',
        created_at: '2026-08-01T10:00:00Z',
        updated_at: '2026-08-01T10:00:00Z',
      },
    ],
    ...overrides,
  };
}

function transition(overrides: Partial<PurchaseOrderTransition> = {}): PurchaseOrderTransition {
  return {
    id: 'transition-1',
    purchase_order_id: 'po-1',
    actor_id: USER.id,
    from_status: 'SUBMITTED',
    to_status: 'APPROVED',
    operation: 'approve',
    reason: 'Approved for delivery',
    occurred_at: '2026-08-02T10:00:00Z',
    ...overrides,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe('PurchaseOrderDetailPage', () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
    routerPush.mockClear();
    useParamsMock.mockReturnValue({ purchaseOrderId: 'po-1' });
    renderedDetailIds.length = 0;
    renderedTransitionPoIds.length = 0;
  });

  it('renders loading then the canonical snapshot and exact Decimal values', async () => {
    const poRequest = deferred<PurchaseOrder>();
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/auth/me') return Promise.resolve(USER as never);
      if (path === '/v1/purchase-orders/po-1') return poRequest.promise as never;
      if (path.includes('/transitions'))
        return Promise.resolve({ items: [], next_cursor: null } as never);
      return Promise.resolve(null as never);
    });
    render(<PurchaseOrderDetailPage />);
    expect(screen.getByTestId('po-detail-loading')).toBeInTheDocument();
    await act(async () => {
      poRequest.resolve(makePO());
      await poRequest.promise;
    });
    expect(await screen.findByTestId('po-detail')).toBeInTheDocument();
    expect(screen.getByText('Acme Snapshot · ACME')).toBeInTheDocument();
    expect(screen.getByText('Frozen Item Snapshot')).toBeInTheDocument();
    expect(screen.getByText('ITEM-SNAPSHOT · SKU-SNAPSHOT')).toBeInTheDocument();
    expect(screen.getByText('999,999,999,999.999999')).toBeInTheDocument();
    expect(screen.getAllByText('USD 99,999,999,999,999,999,899,000,000.00').length).toBeGreaterThan(
      0,
    );
    expect(screen.getAllByText('You').length).toBeGreaterThan(0);
    expect(await screen.findByTestId('po-transitions-empty')).toBeInTheDocument();
  });

  it('renders canonical forbidden and tenant-hidden unavailable states', async () => {
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/auth/me') return Promise.resolve(USER as never);
      return Promise.reject(new ApiError(403, { detail: 'denied' }));
    });
    const first = render(<PurchaseOrderDetailPage />);
    expect(await screen.findByTestId('ape-forbidden')).toBeInTheDocument();
    first.unmount();
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/auth/me') return Promise.resolve(USER as never);
      return Promise.reject(new ApiError(404, { detail: 'hidden' }));
    });
    render(<PurchaseOrderDetailPage />);
    expect(await screen.findByText('Purchase order unavailable')).toBeInTheDocument();
    expect(screen.queryByText('hidden')).not.toBeInTheDocument();
  });

  it('loads transitions independently and keeps detail visible on transition failure', async () => {
    const historyRequest = deferred<never>();
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/auth/me') return Promise.resolve(USER as never);
      if (path === '/v1/purchase-orders/po-1') return Promise.resolve(makePO() as never);
      if (path.includes('/transitions')) return historyRequest.promise;
      return Promise.resolve(null as never);
    });
    render(<PurchaseOrderDetailPage />);
    expect(await screen.findByTestId('po-detail')).toBeInTheDocument();
    expect(screen.getByTestId('po-transitions-loading')).toBeInTheDocument();
    const observedRejection = historyRequest.promise.catch(() => undefined);
    await act(async () => {
      historyRequest.reject(new Error('history unavailable'));
      await observedRejection;
    });
    expect(await screen.findByTestId('po-transitions-error')).toBeInTheDocument();
    expect(screen.getByTestId('po-detail')).toBeInTheDocument();
  });

  it('renders transition actor/reason and paginates using opaque cursors', async () => {
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/auth/me') return Promise.resolve(USER as never);
      if (path === '/v1/purchase-orders/po-1') return Promise.resolve(makePO() as never);
      if (path.includes('cursor=next-history'))
        return Promise.resolve({
          items: [transition({ id: 'transition-2', operation: 'cancel', to_status: 'CANCELLED' })],
          next_cursor: null,
        } as never);
      if (path.includes('/transitions'))
        return Promise.resolve({ items: [transition()], next_cursor: 'next-history' } as never);
      return Promise.resolve(null as never);
    });
    render(<PurchaseOrderDetailPage />);
    expect(await screen.findByTestId('po-transition-transition-1')).toHaveTextContent(
      'Approved for delivery',
    );
    expect(screen.getByTestId('po-transition-transition-1')).toHaveTextContent('Actor: You');
    fireEvent.click(screen.getByTestId('po-transitions-next'));
    expect(await screen.findByTestId('po-transition-transition-2')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('po-transitions-previous'));
    expect(await screen.findByTestId('po-transition-transition-1')).toBeInTheDocument();
    expect(
      mockedApiFetch.mock.calls.some(([path]) => String(path).includes('cursor=next-history')),
    ).toBe(true);
  });

  it('locks rapid transition Next and Previous activation and preserves page boundaries', async () => {
    const pageTwo = deferred<{ items: PurchaseOrderTransition[]; next_cursor: null }>();
    const returnToFirst = deferred<{ items: PurchaseOrderTransition[]; next_cursor: string }>();
    let firstPageLoads = 0;
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/auth/me') return Promise.resolve(USER as never);
      if (path === '/v1/purchase-orders/po-1') return Promise.resolve(makePO() as never);
      if (path.includes('cursor=opaque-history-two')) return pageTwo.promise as never;
      if (path.includes('/transitions')) {
        firstPageLoads += 1;
        if (firstPageLoads === 1)
          return Promise.resolve({
            items: [transition()],
            next_cursor: 'opaque-history-two',
          } as never);
        return returnToFirst.promise as never;
      }
      return Promise.resolve(null as never);
    });
    render(<PurchaseOrderDetailPage />);
    await screen.findByTestId('po-transition-transition-1');
    const next = screen.getByTestId('po-transitions-next');
    fireEvent.click(next);
    fireEvent.click(next);
    expect(screen.getByTestId('po-transitions-loading')).toBeInTheDocument();
    expect(
      mockedApiFetch.mock.calls.filter(([path]) =>
        String(path).includes('cursor=opaque-history-two'),
      ),
    ).toHaveLength(1);

    await act(() =>
      pageTwo.resolve({
        items: [transition({ id: 'transition-2', operation: 'cancel', to_status: 'CANCELLED' })],
        next_cursor: null,
      }),
    );
    await screen.findByTestId('po-transition-transition-2');
    const previous = screen.getByTestId('po-transitions-previous');
    fireEvent.click(previous);
    fireEvent.click(previous);
    expect(screen.getByTestId('po-transitions-loading')).toBeInTheDocument();
    expect(firstPageLoads).toBe(2);

    await act(() =>
      returnToFirst.resolve({ items: [transition()], next_cursor: 'opaque-history-two' }),
    );
    expect(await screen.findByTestId('po-transition-transition-1')).toBeInTheDocument();
    expect(screen.getByTestId('po-transitions-previous')).toBeDisabled();
  });

  it('rejects a stale detail response after a route change', async () => {
    const oldPO = deferred<PurchaseOrder>();
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/auth/me') return Promise.resolve(USER as never);
      if (path === '/v1/purchase-orders/po-1') return oldPO.promise as never;
      if (path === '/v1/purchase-orders/po-2')
        return Promise.resolve(makePO({ id: 'po-2', po_number: 'PO-NEW' }) as never);
      if (path.includes('/po-2/transitions'))
        return Promise.resolve({
          items: [
            transition({ id: 'transition-new', purchase_order_id: 'po-2', reason: 'New route' }),
          ],
          next_cursor: null,
        } as never);
      return Promise.resolve(null as never);
    });
    const view = render(<PurchaseOrderDetailPage />);
    useParamsMock.mockReturnValue({ purchaseOrderId: 'po-2' });
    view.rerender(<PurchaseOrderDetailPage />);
    expect(await screen.findByText('PO-NEW')).toBeInTheDocument();
    expect(await screen.findByTestId('po-transition-transition-new')).toBeInTheDocument();
    await act(() => oldPO.resolve(makePO({ po_number: 'PO-STALE' })));
    expect(screen.queryByText('PO-STALE')).not.toBeInTheDocument();
  });

  it('never renders an already-loaded PO under a new route identity', async () => {
    const newPO = deferred<PurchaseOrder>();
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/auth/me') return Promise.resolve(USER as never);
      if (path === '/v1/purchase-orders/po-1') return Promise.resolve(makePO() as never);
      if (path === '/v1/purchase-orders/po-2') return newPO.promise as never;
      if (path.includes('/transitions'))
        return Promise.resolve({ items: [], next_cursor: null } as never);
      return Promise.resolve(null as never);
    });
    const view = render(<PurchaseOrderDetailPage />);
    expect(await screen.findByText('PO-2026-000001')).toBeInTheDocument();
    renderedDetailIds.length = 0;

    useParamsMock.mockReturnValue({ purchaseOrderId: 'po-2' });
    view.rerender(<PurchaseOrderDetailPage />);
    expect(screen.queryByText('PO-2026-000001')).not.toBeInTheDocument();
    expect(renderedDetailIds).not.toContain('po-1');

    await act(() => newPO.resolve(makePO({ id: 'po-2', po_number: 'PO-NEW' })));
    expect(await screen.findByText('PO-NEW')).toBeInTheDocument();
    expect(renderedDetailIds[renderedDetailIds.length - 1]).toBe('po-2');
  });

  it('never renders already-loaded transitions under a new PO route', async () => {
    const newPO = deferred<PurchaseOrder>();
    const newHistory = deferred<{ items: PurchaseOrderTransition[]; next_cursor: null }>();
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/auth/me') return Promise.resolve(USER as never);
      if (path === '/v1/purchase-orders/po-1') return Promise.resolve(makePO() as never);
      if (path.includes('/po-1/transitions'))
        return Promise.resolve({ items: [transition()], next_cursor: null } as never);
      if (path === '/v1/purchase-orders/po-2') return newPO.promise as never;
      if (path.includes('/po-2/transitions')) return newHistory.promise as never;
      return Promise.resolve(null as never);
    });
    const view = render(<PurchaseOrderDetailPage />);
    expect(await screen.findByTestId('po-transition-transition-1')).toBeInTheDocument();
    renderedTransitionPoIds.length = 0;

    useParamsMock.mockReturnValue({ purchaseOrderId: 'po-2' });
    view.rerender(<PurchaseOrderDetailPage />);
    expect(screen.queryByTestId('po-transition-transition-1')).not.toBeInTheDocument();
    expect(renderedTransitionPoIds.flat()).not.toContain('po-1');

    await act(() => newPO.resolve(makePO({ id: 'po-2', po_number: 'PO-NEW' })));
    await act(() =>
      newHistory.resolve({
        items: [transition({ id: 'transition-new', purchase_order_id: 'po-2' })],
        next_cursor: null,
      }),
    );
    expect(await screen.findByTestId('po-transition-transition-new')).toBeInTheDocument();
    expect(renderedTransitionPoIds[renderedTransitionPoIds.length - 1]).toEqual(['po-2']);
  });

  it('rejects a stale transition response after a route change', async () => {
    const oldHistory = deferred<{ items: PurchaseOrderTransition[]; next_cursor: null }>();
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/auth/me') return Promise.resolve(USER as never);
      if (path === '/v1/purchase-orders/po-1') return Promise.resolve(makePO() as never);
      if (path === '/v1/purchase-orders/po-2')
        return Promise.resolve(makePO({ id: 'po-2', po_number: 'PO-NEW' }) as never);
      if (path.includes('/po-1/transitions')) return oldHistory.promise as never;
      if (path.includes('/po-2/transitions'))
        return Promise.resolve({
          items: [
            transition({
              id: 'transition-new',
              purchase_order_id: 'po-2',
              reason: 'New route',
            }),
          ],
          next_cursor: null,
        } as never);
      return Promise.resolve(null as never);
    });
    const view = render(<PurchaseOrderDetailPage />);
    expect(await screen.findByTestId('po-transitions-loading')).toBeInTheDocument();
    useParamsMock.mockReturnValue({ purchaseOrderId: 'po-2' });
    view.rerender(<PurchaseOrderDetailPage />);
    expect(await screen.findByTestId('po-transition-transition-new')).toBeInTheDocument();
    await act(() =>
      oldHistory.resolve({ items: [transition({ id: 'transition-stale' })], next_cursor: null }),
    );
    expect(screen.queryByTestId('po-transition-transition-stale')).not.toBeInTheDocument();
  });

  it('keeps the newest PO A detail and history across an overlapping A to B to A route sequence', async () => {
    const firstA = deferred<PurchaseOrder>();
    const poB = deferred<PurchaseOrder>();
    const secondA = deferred<PurchaseOrder>();
    const historyA = deferred<{ items: PurchaseOrderTransition[]; next_cursor: null }>();
    let aLoads = 0;
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/auth/me') return Promise.resolve(USER as never);
      if (path === '/v1/purchase-orders/po-1') {
        aLoads += 1;
        return (aLoads === 1 ? firstA.promise : secondA.promise) as never;
      }
      if (path === '/v1/purchase-orders/po-2') return poB.promise as never;
      if (path.includes('/po-1/transitions')) return historyA.promise as never;
      if (path.includes('/po-2/transitions'))
        return Promise.resolve({ items: [], next_cursor: null } as never);
      return Promise.resolve(null as never);
    });
    const view = render(<PurchaseOrderDetailPage />);
    await waitFor(() => expect(aLoads).toBe(1));
    act(() => {
      useParamsMock.mockReturnValue({ purchaseOrderId: 'po-2' });
      view.rerender(<PurchaseOrderDetailPage />);
    });
    await waitFor(() =>
      expect(mockedApiFetch.mock.calls.some(([path]) => path === '/v1/purchase-orders/po-2')).toBe(
        true,
      ),
    );
    act(() => {
      useParamsMock.mockReturnValue({ purchaseOrderId: 'po-1' });
      view.rerender(<PurchaseOrderDetailPage />);
    });
    await waitFor(() => expect(aLoads).toBe(2));

    await act(() => secondA.resolve(makePO({ po_number: 'PO-A-CURRENT' })));
    await act(() =>
      historyA.resolve({
        items: [transition({ id: 'transition-a-current', reason: 'Current A' })],
        next_cursor: null,
      }),
    );
    expect(await screen.findByText('PO-A-CURRENT')).toBeInTheDocument();
    expect(await screen.findByTestId('po-transition-transition-a-current')).toBeInTheDocument();

    await act(() => firstA.resolve(makePO({ po_number: 'PO-A-STALE' })));
    await act(() => poB.resolve(makePO({ id: 'po-2', po_number: 'PO-B-STALE' })));
    expect(screen.queryByText('PO-A-STALE')).not.toBeInTheDocument();
    expect(screen.queryByText('PO-B-STALE')).not.toBeInTheDocument();
    expect(screen.getByText('PO-A-CURRENT')).toBeInTheDocument();
  });

  it('renders only A2 transition history after overlapping A to B to A history requests', async () => {
    const historyA1 = deferred<{ items: PurchaseOrderTransition[]; next_cursor: null }>();
    const historyB = deferred<{ items: PurchaseOrderTransition[]; next_cursor: null }>();
    const historyA2 = deferred<{ items: PurchaseOrderTransition[]; next_cursor: null }>();
    let aHistoryLoads = 0;
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/auth/me') return Promise.resolve(USER as never);
      if (path === '/v1/purchase-orders/po-1') return Promise.resolve(makePO() as never);
      if (path === '/v1/purchase-orders/po-2')
        return Promise.resolve(makePO({ id: 'po-2', po_number: 'PO-B' }) as never);
      if (path.includes('/po-1/transitions')) {
        aHistoryLoads += 1;
        return (aHistoryLoads === 1 ? historyA1.promise : historyA2.promise) as never;
      }
      if (path.includes('/po-2/transitions')) return historyB.promise as never;
      return Promise.resolve(null as never);
    });
    const view = render(<PurchaseOrderDetailPage />);
    await waitFor(() => expect(aHistoryLoads).toBe(1));
    act(() => {
      useParamsMock.mockReturnValue({ purchaseOrderId: 'po-2' });
      view.rerender(<PurchaseOrderDetailPage />);
    });
    await waitFor(() =>
      expect(
        mockedApiFetch.mock.calls.some(([path]) => String(path).includes('/po-2/transitions')),
      ).toBe(true),
    );
    act(() => {
      useParamsMock.mockReturnValue({ purchaseOrderId: 'po-1' });
      view.rerender(<PurchaseOrderDetailPage />);
    });
    await waitFor(() => expect(aHistoryLoads).toBe(2));

    await act(() =>
      historyA2.resolve({
        items: [transition({ id: 'transition-a2', reason: 'A2 current history' })],
        next_cursor: null,
      }),
    );
    expect(await screen.findByTestId('po-transition-transition-a2')).toBeInTheDocument();

    await act(() =>
      historyA1.resolve({
        items: [transition({ id: 'transition-a1', reason: 'A1 stale history' })],
        next_cursor: null,
      }),
    );
    await act(() =>
      historyB.resolve({
        items: [
          transition({ id: 'transition-b', purchase_order_id: 'po-2', reason: 'B stale history' }),
        ],
        next_cursor: null,
      }),
    );
    expect(screen.getByTestId('po-transition-transition-a2')).toBeInTheDocument();
    expect(screen.queryByTestId('po-transition-transition-a1')).not.toBeInTheDocument();
    expect(screen.queryByTestId('po-transition-transition-b')).not.toBeInTheDocument();
  });

  it.each([
    { label: 'read-only', permissions: ['purchase_receipt.read'], history: true, create: false },
    {
      label: 'create-only',
      permissions: ['purchase_receipt.create'],
      history: false,
      create: true,
    },
    { label: 'neither', permissions: [], history: false, create: false },
  ])(
    'applies $label receipt presentation without unauthorized history requests',
    async ({ permissions, history, create }) => {
      const scopedUser = {
        ...USER,
        permission_scopes: [
          {
            organization_id: 'org-1',
            farm_id: 'farm-1',
            permissions: ['purchase_order.read', ...permissions],
          },
        ],
      };
      mockedApiFetch.mockImplementation((path: string) => {
        if (path === '/v1/auth/me') return Promise.resolve(scopedUser as never);
        if (path === '/v1/purchase-orders/po-1') return Promise.resolve(makePO() as never);
        if (path.includes('/transitions'))
          return Promise.resolve({ items: [], next_cursor: null } as never);
        if (path.includes('/receipts?'))
          return Promise.resolve({ items: [], next_cursor: null } as never);
        if (path.includes('/receipt-warehouses')) return Promise.resolve([] as never);
        return Promise.resolve(null as never);
      });
      render(<PurchaseOrderDetailPage />);
      await screen.findByTestId('po-detail');
      if (history) expect(await screen.findByTestId('receipt-history')).toBeInTheDocument();
      else expect(screen.queryByTestId('receipt-history')).not.toBeInTheDocument();
      expect(Boolean(screen.queryByTestId('receive-po-action'))).toBe(create);
      expect(mockedApiFetch.mock.calls.some(([path]) => String(path).includes('/receipts?'))).toBe(
        history,
      );
    },
  );

  it('restores focus to Receive and does not eagerly request every warehouse location', async () => {
    const scopedUser = {
      ...USER,
      permission_scopes: [
        {
          organization_id: 'org-1',
          farm_id: 'farm-1',
          permissions: ['purchase_order.read', 'purchase_receipt.create', 'purchase_receipt.read'],
        },
      ],
    };
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/auth/me') return Promise.resolve(scopedUser as never);
      if (path === '/v1/purchase-orders/po-1') return Promise.resolve(makePO() as never);
      if (path.includes('/transitions') || path.includes('/receipts?'))
        return Promise.resolve({ items: [], next_cursor: null } as never);
      if (path.includes('/receipt-warehouses'))
        return Promise.resolve([{ id: 'wh-1', name: 'Main', code: 'MAIN' }] as never);
      return Promise.resolve(null as never);
    });
    render(<PurchaseOrderDetailPage />);
    const receive = await screen.findByTestId('receive-po-action');
    expect(
      mockedApiFetch.mock.calls.some(([path]) => String(path).includes('/storage-locations')),
    ).toBe(false);
    fireEvent.click(receive);
    fireEvent.click(screen.getByRole('button', { name: 'Cancel receipt' }));
    await waitFor(() => expect(receive).toHaveFocus());
  });

  it.each([
    ['Complete created receipt', false],
    ['Complete replayed receipt', true],
  ] as const)(
    'performs one bounded authoritative refresh after %s',
    async (buttonName, _replayed) => {
      const scopedUser = {
        ...USER,
        permission_scopes: [
          {
            organization_id: 'org-1',
            farm_id: 'farm-1',
            permissions: [
              'purchase_order.read',
              'purchase_receipt.create',
              'purchase_receipt.read',
            ],
          },
        ],
      };
      let poLoads = 0;
      let transitionLoads = 0;
      let receiptLoads = 0;
      let warehouseLoads = 0;
      mockedApiFetch.mockImplementation((path: string) => {
        if (path === '/v1/auth/me') return Promise.resolve(scopedUser as never);
        if (path === '/v1/purchase-orders/po-1') {
          poLoads += 1;
          return Promise.resolve(makePO({ version: poLoads }) as never);
        }
        if (path.includes('/transitions')) {
          transitionLoads += 1;
          return Promise.resolve({ items: [], next_cursor: null } as never);
        }
        if (path.includes('/receipts?')) {
          receiptLoads += 1;
          return Promise.resolve({ items: [], next_cursor: null } as never);
        }
        if (path.includes('/receipt-warehouses')) {
          warehouseLoads += 1;
          return Promise.resolve([] as never);
        }
        return Promise.resolve(null as never);
      });
      render(<PurchaseOrderDetailPage />);
      fireEvent.click(await screen.findByTestId('receive-po-action'));
      fireEvent.click(screen.getByRole('button', { name: buttonName }));
      await waitFor(() => expect(poLoads).toBe(2));
      await waitFor(() =>
        expect([transitionLoads, receiptLoads, warehouseLoads]).toEqual([2, 2, 2]),
      );

      const requestedPaths = mockedApiFetch.mock.calls.map(([path]) => path);
      expect(
        requestedPaths.filter((path) => path === '/v1/purchase-orders/po-1/receipt-warehouses'),
      ).toHaveLength(2);
      expect(requestedPaths).not.toContain('/v1/purchase-orders/org-1/receipt-warehouses');

      await waitFor(() => expect(screen.getByTestId('receive-po-action')).toHaveFocus());
    },
  );

  it('focuses the Receiving heading after a terminal receipt removes the Receive control', async () => {
    const scopedUser = {
      ...USER,
      permission_scopes: [
        {
          organization_id: 'org-1',
          farm_id: 'farm-1',
          permissions: ['purchase_order.read', 'purchase_receipt.create', 'purchase_receipt.read'],
        },
      ],
    };
    let poLoads = 0;
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/auth/me') return Promise.resolve(scopedUser as never);
      if (path === '/v1/purchase-orders/po-1') {
        poLoads += 1;
        return Promise.resolve(makePO(poLoads === 1 ? {} : { status: 'RECEIVED' }) as never);
      }
      if (path.includes('/transitions') || path.includes('/receipts?'))
        return Promise.resolve({ items: [], next_cursor: null } as never);
      if (path.includes('/receipt-warehouses')) return Promise.resolve([] as never);
      return Promise.resolve(null as never);
    });
    render(<PurchaseOrderDetailPage />);
    fireEvent.click(await screen.findByTestId('receive-po-action'));
    fireEvent.click(screen.getByRole('button', { name: 'Complete created receipt' }));
    await waitFor(() => expect(screen.queryByTestId('receive-po-action')).not.toBeInTheDocument());
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Receiving' })).toHaveFocus());
  });

  it('does not move focus in a new PO context when an old receipt refresh finishes', async () => {
    const scopedUser = {
      ...USER,
      permission_scopes: [
        {
          organization_id: 'org-1',
          farm_id: 'farm-1',
          permissions: ['purchase_order.read', 'purchase_receipt.create', 'purchase_receipt.read'],
        },
      ],
    };
    const oldRefresh = deferred<PurchaseOrder>();
    let poOneLoads = 0;
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/auth/me') return Promise.resolve(scopedUser as never);
      if (path === '/v1/purchase-orders/po-1') {
        poOneLoads += 1;
        return poOneLoads === 1
          ? Promise.resolve(makePO() as never)
          : (oldRefresh.promise as never);
      }
      if (path === '/v1/purchase-orders/po-2')
        return Promise.resolve(makePO({ id: 'po-2', po_number: 'PO-TWO' }) as never);
      if (path.includes('/transitions') || path.includes('/receipts?'))
        return Promise.resolve({ items: [], next_cursor: null } as never);
      if (path.includes('/receipt-warehouses')) return Promise.resolve([] as never);
      return Promise.resolve(null as never);
    });
    const view = render(
      <>
        <button>New context target</button>
        <PurchaseOrderDetailPage />
      </>,
    );
    fireEvent.click(await screen.findByTestId('receive-po-action'));
    fireEvent.click(screen.getByRole('button', { name: 'Complete created receipt' }));
    useParamsMock.mockReturnValue({ purchaseOrderId: 'po-2' });
    view.rerender(
      <>
        <button>New context target</button>
        <PurchaseOrderDetailPage />
      </>,
    );
    await screen.findByText('PO-TWO');
    screen.getByRole('button', { name: 'New context target' }).focus();
    await act(() => oldRefresh.resolve(makePO({ version: 5 })));
    expect(screen.getByRole('button', { name: 'New context target' })).toHaveFocus();
  });

  it('traverses forward and backward using an opaque receipt cursor', async () => {
    const scopedUser = {
      ...USER,
      permission_scopes: [
        {
          organization_id: 'org-1',
          farm_id: 'farm-1',
          permissions: ['purchase_order.read', 'purchase_receipt.read'],
        },
      ],
    };
    const receipt = (id: string, grn: string) => ({
      id,
      organization_id: 'org-1',
      purchase_order_id: 'po-1',
      farm_id: 'farm-1',
      warehouse_id: 'wh-1',
      grn,
      supplier_delivery_reference: null,
      received_at: '2026-08-11T10:00:00Z',
      received_by_id: 'user-1',
      notes: null,
      created_at: '2026-08-11T10:00:00Z',
      lines: [],
    });
    let firstLoads = 0;
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/auth/me') return Promise.resolve(scopedUser as never);
      if (path === '/v1/purchase-orders/po-1') return Promise.resolve(makePO() as never);
      if (path.includes('/transitions'))
        return Promise.resolve({ items: [], next_cursor: null } as never);
      if (path.includes('cursor=opaque-receipt'))
        return Promise.resolve({
          items: [receipt('r-2', 'GRN-SECOND')],
          next_cursor: null,
        } as never);
      if (path.includes('/receipts?')) {
        firstLoads += 1;
        return Promise.resolve({
          items: [receipt('r-1', 'GRN-FIRST')],
          next_cursor: 'opaque-receipt',
        } as never);
      }
      if (path.includes('/receipt-warehouses'))
        return Promise.resolve([{ id: 'wh-1', name: 'Main', code: 'MAIN' }] as never);
      return Promise.resolve(null as never);
    });
    render(<PurchaseOrderDetailPage />);
    await screen.findByText('GRN-FIRST');
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    expect(await screen.findByText('GRN-SECOND')).toBeInTheDocument();
    expect(screen.queryByText('GRN-FIRST')).not.toBeInTheDocument();
    expect(
      mockedApiFetch.mock.calls.some(([path]) => String(path).includes('cursor=opaque-receipt')),
    ).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: 'Previous' }));
    await waitFor(() => expect(firstLoads).toBe(2));
    expect(await screen.findByText('GRN-FIRST')).toBeInTheDocument();
    expect(screen.queryByText('GRN-SECOND')).not.toBeInTheDocument();
  });

  it('safely recovers an invalid opaque receipt cursor', async () => {
    const scopedUser = {
      ...USER,
      permission_scopes: [
        {
          organization_id: 'org-1',
          farm_id: 'farm-1',
          permissions: ['purchase_order.read', 'purchase_receipt.read'],
        },
      ],
    };
    const receipt = {
      id: 'r-1',
      organization_id: 'org-1',
      purchase_order_id: 'po-1',
      farm_id: 'farm-1',
      warehouse_id: 'wh-1',
      grn: 'GRN-FIRST',
      supplier_delivery_reference: null,
      received_at: '2026-08-11T10:00:00Z',
      received_by_id: 'user-1',
      notes: null,
      created_at: '2026-08-11T10:00:00Z',
      lines: [],
    };
    let firstLoads = 0;
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/auth/me') return Promise.resolve(scopedUser as never);
      if (path === '/v1/purchase-orders/po-1') return Promise.resolve(makePO() as never);
      if (path.includes('/transitions'))
        return Promise.resolve({ items: [], next_cursor: null } as never);
      if (path.includes('cursor=invalid-opaque'))
        return Promise.reject(new ApiError(422, { detail: { code: 'invalid_cursor' } } as never));
      if (path.includes('/receipts?')) {
        firstLoads += 1;
        return Promise.resolve({
          items: [receipt],
          next_cursor: firstLoads === 1 ? 'invalid-opaque' : null,
        } as never);
      }
      if (path.includes('/receipt-warehouses')) return Promise.resolve([] as never);
      return Promise.resolve(null as never);
    });
    render(<PurchaseOrderDetailPage />);
    await screen.findByText('GRN-FIRST');
    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    await waitFor(() => expect(firstLoads).toBe(2));
    expect(await screen.findByText('GRN-FIRST')).toBeInTheDocument();
  });

  it('rejects a stale receipt-history response after a PO route change', async () => {
    const scopedUser = {
      ...USER,
      permission_scopes: [
        {
          organization_id: 'org-1',
          farm_id: 'farm-1',
          permissions: ['purchase_order.read', 'purchase_receipt.read'],
        },
      ],
    };
    const row = (id: string, po: string, grn: string) => ({
      id,
      organization_id: 'org-1',
      purchase_order_id: po,
      farm_id: 'farm-1',
      warehouse_id: 'wh-1',
      grn,
      supplier_delivery_reference: null,
      received_at: '2026-08-11T10:00:00Z',
      received_by_id: 'user-1',
      notes: null,
      created_at: '2026-08-11T10:00:00Z',
      lines: [],
    });
    const oldHistory = deferred<{ items: Array<ReturnType<typeof row>>; next_cursor: null }>();
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/auth/me') return Promise.resolve(scopedUser as never);
      if (path === '/v1/purchase-orders/po-1') return Promise.resolve(makePO() as never);
      if (path === '/v1/purchase-orders/po-2')
        return Promise.resolve(makePO({ id: 'po-2', po_number: 'PO-TWO' }) as never);
      if (path.includes('/po-1/receipts?')) return oldHistory.promise as never;
      if (path.includes('/po-2/receipts?'))
        return Promise.resolve({
          items: [row('r-2', 'po-2', 'GRN-CURRENT')],
          next_cursor: null,
        } as never);
      if (path.includes('/transitions'))
        return Promise.resolve({ items: [], next_cursor: null } as never);
      if (path.includes('/receipt-warehouses')) return Promise.resolve([] as never);
      return Promise.resolve(null as never);
    });
    const view = render(<PurchaseOrderDetailPage />);
    await screen.findByTestId('receipt-history');
    useParamsMock.mockReturnValue({ purchaseOrderId: 'po-2' });
    view.rerender(<PurchaseOrderDetailPage />);
    expect(await screen.findByText('GRN-CURRENT')).toBeInTheDocument();
    await act(() =>
      oldHistory.resolve({ items: [row('r-old', 'po-1', 'GRN-STALE')], next_cursor: null }),
    );
    expect(screen.queryByText('GRN-STALE')).not.toBeInTheDocument();
  });
});
