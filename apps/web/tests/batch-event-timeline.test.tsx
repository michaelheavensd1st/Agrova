import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';

vi.mock('next/navigation', () => ({
  useParams: () => ({ batchId: 'batch-1' }),
  useRouter: () => ({ push: vi.fn() }),
}));
vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api');
  return { ...actual, apiFetch: vi.fn() };
});
vi.mock('@/components/event-forms', () => ({
  CatalogEventForm: () => <div data-testid="catalog-event-form-mock" />,
  FeedingForm: () => null,
  MortalityForm: () => null,
  StockingForm: () => null,
  TransferEventForm: ({ sourceUnit }: { sourceUnit: { code: string } }) => (
    <div data-testid="transfer-event-form-mock">{sourceUnit.code}</div>
  ),
  useEventCatalog: () => [
    {
      code: 'TRANSFER',
      display_name: 'Transfer',
      category: 'operations',
      version: 2,
      triggers_transition_to: null,
      schema: {},
      metadata: {},
      openapi_example: null,
    },
  ],
}));

import BatchDetailPage from '@/app/batches/[batchId]/page';
import { apiFetch } from '@/lib/api';

const mockedApiFetch = apiFetch as unknown as ReturnType<typeof vi.fn>;
const DESTINATION_UUID = '12345678-1234-4abc-8def-1234567890ab';

describe('Batch event timeline', () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
  });

  it('renders a safe TRANSFER summary without exposing the destination UUID', async () => {
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/batches/batch-1') {
        return Promise.resolve({
          id: 'batch-1',
          unit_id: 'unit-1',
          code: 'BATCH-001',
          state: 'active',
          species: 'L. vannamei',
          planned_at: null,
          stocked_at: null,
          harvested_at: null,
          closed_at: null,
          expected_quantity: 1000,
          notes: null,
        });
      }
      if (path === '/v1/units/unit-1') {
        return Promise.resolve({
          id: 'unit-1',
          site_id: 'site-1',
          unit_type_id: 'type-1',
          name: 'Source Tank',
          code: 'SRC-01',
          status: 'active',
          capacity: 2000,
        });
      }
      if (path === '/v1/sites/site-1') {
        return Promise.resolve({
          id: 'site-1',
          farm_id: 'farm-1',
          name: 'Main Site',
          code: 'MAIN',
          status: 'active',
        });
      }
      if (path === '/v1/farms/farm-1') {
        return Promise.resolve({
          id: 'farm-1',
          organization_id: 'org-1',
          name: 'Test Farm',
          code: 'FARM',
          deleted_at: null,
        });
      }
      if (path === '/v1/production-unit-types') return Promise.resolve([]);
      if (path === '/v1/batches/batch-1/events?limit=50') {
        return Promise.resolve({
          items: [
            {
              id: 'event-1',
              batch_id: 'batch-1',
              event_type: 'TRANSFER',
              event_type_version: 1,
              performed_by_id: 'user-1',
              performed_at: '2026-08-21T12:00:00.000Z',
              transfer_id: 'transfer-1',
              transfer_role: 'out',
              data: { destination_unit_id: DESTINATION_UUID, quantity: 125 },
              is_final: false,
              notes: null,
            },
            {
              id: 'event-2',
              batch_id: 'batch-1',
              event_type: 'TRANSFER',
              event_type_version: 2,
              transfer_id: 'transfer-2',
              transfer_role: 'in',
              performed_by_id: 'user-1',
              performed_at: '2026-08-21T11:00:00.000Z',
              data: { source_unit_id: DESTINATION_UUID, quantity: 80 },
              is_final: false,
              notes: null,
            },
          ],
          next_cursor: null,
          limit: 50,
        });
      }
      if (path === '/v1/batches/batch-1/projections') {
        return Promise.resolve({
          batch_id: 'batch-1',
          initial_stocked_quantity: 1000,
          cumulative_mortality: 0,
          cumulative_harvest: 0,
          cumulative_transfer_out: 125,
          estimated_remaining_population: 875,
          latest_average_weight: null,
          weight_unit: null,
          estimated_biomass_kg: null,
          total_feed_kg: 0,
          survival_rate: 1,
          batch_age_days: 1,
          latest_water_quality: null,
          latest_sampling_at: null,
          computed_at: '2026-08-21T12:00:00.000Z',
        });
      }
      return Promise.reject(new Error(`Unexpected request: ${path}`));
    });

    render(<BatchDetailPage />);

    const row = await screen.findByTestId('event-row-event-1');
    await waitFor(() => expect(row).toHaveTextContent('→ 125 ind. sent'));
    expect(row).not.toHaveTextContent(DESTINATION_UUID);
    expect(row).not.toHaveTextContent(DESTINATION_UUID.slice(0, 8));
    const receipt = screen.getByTestId('event-row-event-2');
    expect(receipt).toHaveTextContent('← 80 ind. received');
    expect(receipt).not.toHaveTextContent(DESTINATION_UUID);

    fireEvent.click(screen.getByTestId('record-TRANSFER'));
    expect(screen.getByTestId('transfer-event-form-mock')).toHaveTextContent('SRC-01');
    expect(screen.queryByTestId('catalog-event-form-mock')).not.toBeInTheDocument();
  });
});
