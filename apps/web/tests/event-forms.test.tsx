/**
 * Vitest guard for the deliberate operational event forms.
 *
 * These are minimal render + form-validation checks — the full E2E
 * happens against a live FastAPI in the pytest suite. Here we
 * verify the client-side gates that stop empty / unconfirmed
 * submissions ever leaving the browser.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { ApiError, apiFetch, apiFetchResult } from '@/lib/api';
import { FeedingForm, StockingForm, MortalityForm } from '@/components/event-forms';

vi.mock('@/lib/api', async (importOriginal) => {
  const original = await importOriginal<typeof import('@/lib/api')>();
  return { ...original, apiFetch: vi.fn(), apiFetchResult: vi.fn() };
});

const FEED_LOT_ID = '11111111-1111-4111-8111-111111111111';
const FEED_ITEM_ID = '22222222-2222-4222-8222-222222222222';

function mockEligibleLot() {
  vi.mocked(apiFetch).mockImplementation(async (path) => {
    if (path.endsWith('/warehouses?operational_only=true'))
      return [
        {
          id: 'warehouse-1',
          organization_id: 'org-1',
          farm_id: 'farm-1',
          code: 'FEED',
          name: 'Feed Store',
          status: 'active',
        },
      ] as never;
    if (path.endsWith('/inventory-items?operational_only=true'))
      return [
        {
          id: FEED_ITEM_ID,
          code: 'GROWER',
          name: 'Grower crumble',
          category: 'feed',
          canonical_unit: 'kg',
          is_active: true,
        },
      ] as never;
    if (path === '/v1/warehouses/warehouse-1/lots')
      return [
        {
          id: FEED_LOT_ID,
          item_id: FEED_ITEM_ID,
          warehouse_id: 'warehouse-1',
          storage_location_id: null,
          lot_code: 'UAT-FEED-01',
          expiry_date: null,
          balance: '50',
          balance_unit: 'kg',
        },
      ] as never;
    throw new Error(`Unexpected request: ${path}`);
  });
}

function renderFeeding() {
  return render(
    <FeedingForm
      batchId="batch-1"
      organizationId="org-1"
      farmId="farm-1"
      onCreated={() => {}}
      onCancel={() => {}}
    />,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(apiFetchResult).mockResolvedValue({
    data: { id: 'event-1' },
    response: new Response(null, { status: 201 }),
  } as never);
});

describe('FeedingForm', () => {
  it('maps a readable selected lot label to its UUID in the request', async () => {
    mockEligibleLot();
    renderFeeding();
    const select = await screen.findByTestId('feeding-lot-id');
    await waitFor(() => expect(select).not.toBeDisabled());
    expect(screen.getByRole('option', { name: /Grower crumble.*UAT-FEED-01/ })).toHaveValue(
      FEED_LOT_ID,
    );
    fireEvent.change(select, { target: { value: FEED_LOT_ID } });
    fireEvent.click(screen.getByTestId('feeding-submit'));
    await waitFor(() => expect(apiFetchResult).toHaveBeenCalled());
    const [, init] = vi.mocked(apiFetchResult).mock.calls[0];
    expect(JSON.parse(String(init?.body))).toMatchObject({
      event_type: 'FEEDING',
      data: { inventory_lot_id: FEED_LOT_ID },
    });
    expect(apiFetch).toHaveBeenCalledWith(
      '/v1/organizations/org-1/warehouses?operational_only=true',
    );
    expect(apiFetch).toHaveBeenCalledWith(
      '/v1/organizations/org-1/inventory-items?operational_only=true',
    );
  });

  it('cannot submit arbitrary text as inventory_lot_id', async () => {
    mockEligibleLot();
    renderFeeding();
    const select = await screen.findByTestId('feeding-lot-id');
    await waitFor(() => expect(select).not.toBeDisabled());
    fireEvent.change(select, { target: { value: 'UAT-BATch Feed lot 1' } });
    fireEvent.click(screen.getByTestId('feeding-submit'));
    await waitFor(() => expect(apiFetchResult).toHaveBeenCalled());
    const [, init] = vi.mocked(apiFetchResult).mock.calls[0];
    const body = JSON.parse(String(init?.body));
    expect(body.data).not.toHaveProperty('inventory_lot_id');
    expect(body.data.feed_description).toBe('Grower crumble 35%');
  });

  it('omits an empty optional lot value', async () => {
    mockEligibleLot();
    renderFeeding();
    await waitFor(() => expect(screen.getByTestId('feeding-lot-id')).not.toBeDisabled());
    fireEvent.click(screen.getByTestId('feeding-submit'));
    await waitFor(() => expect(apiFetchResult).toHaveBeenCalled());
    const [, init] = vi.mocked(apiFetchResult).mock.calls[0];
    expect(JSON.parse(String(init?.body)).data).not.toHaveProperty('inventory_lot_id');
  });

  it('shows a clear state when no eligible lots exist', async () => {
    vi.mocked(apiFetch).mockImplementation(async (path) => {
      if (
        path.endsWith('/warehouses?operational_only=true') ||
        path.endsWith('/inventory-items?operational_only=true')
      )
        return [] as never;
      throw new Error(`Unexpected request: ${path}`);
    });
    renderFeeding();
    expect(await screen.findByTestId('feeding-lots-empty')).toHaveTextContent(
      /No eligible feed lots.*ad-hoc feeding/i,
    );
  });

  it('keeps structured 422 errors visible', async () => {
    mockEligibleLot();
    vi.mocked(apiFetchResult).mockRejectedValue(
      new ApiError(422, {
        detail: {
          event_type: 'FEEDING',
          errors: [
            {
              field: 'inventory_lot_id',
              type: 'uuid_parsing',
              message: 'Input should be a valid UUID',
            },
          ],
        },
      } as never),
    );
    renderFeeding();
    await waitFor(() => expect(screen.getByTestId('feeding-lot-id')).not.toBeDisabled());
    fireEvent.change(screen.getByTestId('feeding-lot-id'), { target: { value: FEED_LOT_ID } });
    fireEvent.click(screen.getByTestId('feeding-submit'));
    expect(await screen.findByTestId('feeding-error')).toHaveTextContent(
      'Input should be a valid UUID',
    );
  });
});

describe('StockingForm', () => {
  it('renders required inputs and confirmation checkbox', () => {
    render(<StockingForm batchId="b1" onCreated={() => {}} onCancel={() => {}} />);
    expect(screen.getByTestId('stocking-species')).toBeInTheDocument();
    expect(screen.getByTestId('stocking-quantity')).toBeInTheDocument();
    expect(screen.getByTestId('stocking-avg-weight')).toBeInTheDocument();
    expect(screen.getByTestId('stocking-confirm')).toBeInTheDocument();
    expect(screen.getByTestId('stocking-submit')).toBeInTheDocument();
  });

  it('blocks submission until the confirmation checkbox is checked', async () => {
    const onCreated = vi.fn();
    render(<StockingForm batchId="b1" onCreated={onCreated} onCancel={() => {}} />);
    fireEvent.click(screen.getByTestId('stocking-submit'));
    await waitFor(() => {
      expect(screen.getByTestId('stocking-error')).toHaveTextContent(/confirm/i);
    });
    expect(onCreated).not.toHaveBeenCalled();
  });
});

describe('MortalityForm', () => {
  it('renders the population-guard warning and the confirm checkbox', () => {
    render(<MortalityForm batchId="b1" onCreated={() => {}} onCancel={() => {}} />);
    expect(screen.getByTestId('mortality-count')).toBeInTheDocument();
    expect(screen.getByTestId('mortality-confirm')).toBeInTheDocument();
    expect(screen.getByText(/exceeds population/i)).toBeInTheDocument();
  });

  it('refuses submit until confirmed', async () => {
    render(<MortalityForm batchId="b1" onCreated={() => {}} onCancel={() => {}} />);
    fireEvent.click(screen.getByTestId('mortality-submit'));
    await waitFor(() => {
      expect(screen.getByTestId('mortality-error')).toBeInTheDocument();
    });
  });
});
