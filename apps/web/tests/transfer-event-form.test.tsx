import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';

vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api');
  return { ...actual, apiFetch: vi.fn(), apiFetchResult: vi.fn() };
});

import { CatalogEventForm, TransferEventForm } from '@/components/event-forms';
import { ApiError, apiFetch, apiFetchResult } from '@/lib/api';
import type { EventCatalogEntry, ProductionUnit } from '@/lib/types';

const mockedApiFetch = vi.mocked(apiFetch);
const mockedApiFetchResult = vi.mocked(apiFetchResult);
const SOURCE_UUID = '11111111-1111-4111-8111-111111111111';
const DESTINATION_UUID = '22222222-2222-4222-8222-222222222222';
const DESTINATION_UNIT_UUID = '33333333-3333-4333-8333-333333333333';
const CATALOG_SOURCE_UUID = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const CATALOG_DESTINATION_UUID = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const ORIGINAL_TZ = process.env.TZ;

const SOURCE_UNIT: ProductionUnit = {
  id: SOURCE_UUID,
  site_id: 'site-1',
  unit_type_id: 'type-1',
  code: 'P1',
  name: 'Nursery Pond',
  status: 'active',
  capacity: 1000,
};

const TRANSFER: EventCatalogEntry = {
  code: 'TRANSFER',
  display_name: 'Transfer',
  category: 'operations',
  version: 2,
  triggers_transition_to: null,
  metadata: {},
  schema: {
    type: 'object',
    required: [
      'source_unit_id',
      'destination_unit_id',
      'destination_batch_id',
      'quantity',
      'transferred_at',
    ],
    $defs: { WeightUnit: { type: 'string', enum: ['g', 'kg'] } },
    properties: {
      source_unit_id: { type: 'string' },
      destination_unit_id: { type: 'string' },
      destination_batch_id: { type: 'string' },
      quantity: { type: 'integer', minimum: 1, description: 'Individuals transferred (net).' },
      average_weight: { anyOf: [{ type: 'number', minimum: 0 }, { type: 'null' }] },
      weight_unit: { $ref: '#/$defs/WeightUnit', default: 'g' },
      transfer_loss: {
        type: 'integer',
        minimum: 0,
        default: 0,
        description: 'Mortalities incurred during transfer.',
      },
      transferred_at: { type: 'string', format: 'date-time' },
      notes: { anyOf: [{ type: 'string' }, { type: 'null' }] },
    },
  },
  openapi_example: {
    source_unit_id: CATALOG_SOURCE_UUID,
    destination_unit_id: CATALOG_DESTINATION_UUID,
    quantity: 125,
    average_weight: 2.8,
    weight_unit: 'g',
    transfer_loss: 0,
    transferred_at: '2026-08-21T09:00:00+00:00',
  },
};

function mockSuccessfulDiscovery() {
  mockedApiFetch.mockImplementation(async (path) => {
    if (path === '/v1/batches/batch-1/transfer-destinations') {
      return [
        {
          id: DESTINATION_UUID,
          unit_id: DESTINATION_UNIT_UUID,
          label: 'P2 — Grow-out Pond · GROW',
        },
      ] as never;
    }
    throw new Error(`Unexpected request: ${path}`);
  });
}

function renderTransfer() {
  return render(
    <TransferEventForm
      batchId="batch-1"
      entry={TRANSFER}
      sourceUnit={SOURCE_UNIT}
      onCreated={() => {}}
      onCancel={() => {}}
    />,
  );
}

describe('TransferEventForm', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedApiFetchResult.mockResolvedValue({
      data: { id: 'event-1' },
      response: new Response(null, { status: 201 }),
    } as never);
  });

  afterEach(() => {
    process.env.TZ = ORIGINAL_TZ;
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it('shows a readable derived source and human labels without editable or visible UUID fields', async () => {
    mockSuccessfulDiscovery();
    renderTransfer();

    expect(screen.getByTestId('transfer-source-unit')).toHaveTextContent('P1 — Nursery Pond');
    expect(screen.getByTestId('transfer-source-unit')).not.toHaveTextContent(SOURCE_UUID);
    expect(screen.queryByTestId('catalog-field-source_unit_id')).not.toBeInTheDocument();
    expect(screen.queryByTestId('catalog-field-destination_unit_id')).not.toBeInTheDocument();
    expect(screen.queryByText('source_unit_id')).not.toBeInTheDocument();
    expect(screen.queryByText('destination_unit_id')).not.toBeInTheDocument();
    expect(screen.queryByText(CATALOG_SOURCE_UUID)).not.toBeInTheDocument();
    expect(screen.queryByText(CATALOG_DESTINATION_UUID)).not.toBeInTheDocument();

    const option = await screen.findByRole('option', { name: 'P2 — Grow-out Pond · GROW' });
    expect(option).toHaveValue(DESTINATION_UUID);
    expect(option).not.toHaveTextContent(DESTINATION_UUID);
    expect(screen.queryByText(DESTINATION_UUID)).not.toBeInTheDocument();
    for (const label of [
      'Source unit',
      'Quantity (individuals)',
      'Average weight',
      'Weight unit',
      'Transfer loss',
      'Transferred at',
      'Notes',
    ]) {
      expect(screen.getByText(label, { exact: false })).toBeInTheDocument();
    }
    expect(screen.getByRole('combobox', { name: /Destination batch/ })).toBeInTheDocument();
  });

  it('uses only the batch-scoped server-authoritative destination endpoint', async () => {
    mockSuccessfulDiscovery();
    renderTransfer();

    const select = await screen.findByTestId('transfer-destination-unit');
    await waitFor(() => expect(select).not.toBeDisabled());
    const optionValues = Array.from((select as HTMLSelectElement).options).map(
      (option) => option.value,
    );
    expect(optionValues).toContain(DESTINATION_UUID);
    expect(optionValues).toHaveLength(2);
    expect(mockedApiFetch).toHaveBeenCalledTimes(1);
    expect(mockedApiFetch).toHaveBeenCalledWith('/v1/batches/batch-1/transfer-destinations');
  });

  it('submits authoritative unit UUIDs and rejects arbitrary destination text', async () => {
    mockSuccessfulDiscovery();
    renderTransfer();
    const select = await screen.findByTestId('transfer-destination-unit');
    await waitFor(() => expect(select).not.toBeDisabled());

    fireEvent.change(select, { target: { value: 'P2' } });
    fireEvent.submit(screen.getByTestId('transfer-form'));
    expect(await screen.findByText('Select an eligible destination batch.')).toBeInTheDocument();
    expect(mockedApiFetchResult).not.toHaveBeenCalled();

    fireEvent.change(select, { target: { value: DESTINATION_UUID } });
    fireEvent.change(screen.getByTestId('transfer-quantity'), { target: { value: '80' } });
    fireEvent.change(screen.getByTestId('transfer-average-weight'), { target: { value: '3.4' } });
    fireEvent.change(screen.getByTestId('transfer-loss'), { target: { value: '2' } });
    fireEvent.change(screen.getByTestId('transfer-transferred-at'), {
      target: { value: '2026-08-21T09:00' },
    });
    fireEvent.change(screen.getByTestId('transfer-notes'), { target: { value: 'Moved safely' } });
    fireEvent.click(screen.getByTestId('transfer-submit'));
    await waitFor(() => expect(mockedApiFetchResult).toHaveBeenCalledTimes(1));
    const [path, init] = mockedApiFetchResult.mock.calls[0];
    expect(path).toBe('/v1/batches/batch-1/events');
    expect(JSON.parse(String(init?.body))).toEqual({
      event_type: 'TRANSFER',
      data: {
        source_unit_id: SOURCE_UUID,
        destination_unit_id: DESTINATION_UNIT_UUID,
        destination_batch_id: DESTINATION_UUID,
        quantity: 80,
        average_weight: 3.4,
        weight_unit: 'g',
        transfer_loss: 2,
        transferred_at: new Date('2026-08-21T09:00').toISOString(),
        notes: 'Moved safely',
      },
    });
  });

  it('guards two synchronous submissions with one request and one idempotency key', async () => {
    mockSuccessfulDiscovery();
    let resolvePost!: (value: unknown) => void;
    mockedApiFetchResult.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolvePost = resolve;
        }) as never,
    );
    const randomUUID = vi.fn(() => 'single-transfer-key');
    vi.stubGlobal('crypto', { randomUUID });
    renderTransfer();
    const select = await screen.findByTestId('transfer-destination-unit');
    await waitFor(() => expect(select).not.toBeDisabled());
    fireEvent.change(select, { target: { value: DESTINATION_UUID } });

    const form = screen.getByTestId('transfer-form');
    fireEvent.submit(form);
    fireEvent.submit(form);

    expect(mockedApiFetchResult).toHaveBeenCalledTimes(1);
    expect(randomUUID).toHaveBeenCalledTimes(1);
    expect(mockedApiFetchResult.mock.calls[0][1]?.headers).toEqual({
      'Idempotency-Key': expect.stringContaining('single-transfer-key'),
    });

    resolvePost({ data: { id: 'event-1' }, response: new Response(null, { status: 201 }) });
    await waitFor(() =>
      expect(screen.getByTestId('transfer-submit')).not.toHaveTextContent('Saving'),
    );
  });

  it('initializes transferred time from the current local wall clock and submits the UTC instant', async () => {
    process.env.TZ = 'America/New_York';
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date('2026-08-21T13:45:00.000Z'));
    mockSuccessfulDiscovery();
    renderTransfer();

    expect(screen.getByTestId('transfer-transferred-at')).toHaveValue('2026-08-21T09:45');
    const select = screen.getByTestId('transfer-destination-unit');
    await waitFor(() => expect(select).not.toBeDisabled());
    fireEvent.change(select, { target: { value: DESTINATION_UUID } });
    fireEvent.click(screen.getByTestId('transfer-submit'));
    await waitFor(() => expect(mockedApiFetchResult).toHaveBeenCalledTimes(1));

    const body = JSON.parse(String(mockedApiFetchResult.mock.calls[0][1]?.body));
    expect(body.data.transferred_at).toBe('2026-08-21T13:45:00.000Z');
  });

  it('disables submission while loading and shows a safe empty state after successful discovery', async () => {
    let resolveDestinations!: (destinations: []) => void;
    mockedApiFetch.mockImplementation((path) => {
      if (path === '/v1/batches/batch-1/transfer-destinations') {
        return new Promise((resolve) => {
          resolveDestinations = resolve;
        }) as never;
      }
      return Promise.reject(new Error(`Unexpected request: ${path}`));
    });
    renderTransfer();
    expect(screen.getByTestId('transfer-destinations-loading')).toBeInTheDocument();
    expect(screen.getByTestId('transfer-submit')).toBeDisabled();

    resolveDestinations([]);
    expect(await screen.findByTestId('transfer-destinations-empty')).toHaveTextContent(
      'No eligible destination batches are available in this farm.',
    );
    expect(screen.getByTestId('transfer-submit')).toBeDisabled();
  });

  it('fails destination discovery closed when the authoritative request fails', async () => {
    mockedApiFetch.mockImplementation(async (path) => {
      if (path === '/v1/batches/batch-1/transfer-destinations') {
        throw new Error('Network unavailable');
      }
      throw new Error(`Unexpected request: ${path}`);
    });
    renderTransfer();

    expect(await screen.findByTestId('transfer-destinations-error')).toHaveTextContent(
      'Network unavailable',
    );
    expect(screen.getByTestId('transfer-destination-unit')).toBeDisabled();
    expect(screen.getByTestId('transfer-submit')).toBeDisabled();
  });

  it('hides internal permission codes when destination discovery is forbidden', async () => {
    mockedApiFetch.mockRejectedValue(
      new ApiError(403, {
        detail: 'Missing required permission: production_event.create',
      } as never),
    );
    renderTransfer();

    const error = await screen.findByTestId('transfer-destinations-error');
    expect(error).toHaveTextContent('You do not have permission to record transfers.');
    expect(error).not.toHaveTextContent('production_event.create');
    expect(error).not.toHaveTextContent('Missing required permission');
    expect(screen.getByTestId('transfer-destination-unit')).toBeDisabled();
    expect(screen.getByTestId('transfer-submit')).toBeDisabled();
  });

  it('preserves entered values and useful structured backend errors without exposing UUIDs', async () => {
    mockSuccessfulDiscovery();
    mockedApiFetchResult.mockRejectedValue(
      new ApiError(409, {
        detail: {
          code: 'transfer_exceeds_population',
          message: `Transfer quantity exceeds population for destination_unit_id ${DESTINATION_UUID}.`,
        },
      } as never),
    );
    renderTransfer();
    const select = await screen.findByTestId('transfer-destination-unit');
    await waitFor(() => expect(select).not.toBeDisabled());
    fireEvent.change(select, { target: { value: DESTINATION_UUID } });
    fireEvent.change(screen.getByTestId('transfer-quantity'), { target: { value: '999' } });
    fireEvent.click(screen.getByTestId('transfer-submit'));

    const error = await screen.findByTestId('transfer-error');
    expect(error).toHaveTextContent('Transfer quantity exceeds population for Destination unit');
    expect(error).not.toHaveTextContent(DESTINATION_UUID);
    expect(screen.getByTestId('transfer-quantity')).toHaveValue(999);
    expect(screen.getByTestId('transfer-destination-unit')).toHaveValue(DESTINATION_UUID);
  });

  it('leaves non-TRANSFER catalog rendering unchanged', () => {
    const sampling = {
      ...TRANSFER,
      code: 'SAMPLING',
      display_name: 'Sampling',
      schema: {
        type: 'object',
        required: ['sample_size'],
        properties: { sample_size: { type: 'integer', minimum: 1 } },
      },
      openapi_example: { sample_size: 30 },
    };
    render(
      <CatalogEventForm
        batchId="batch-1"
        entry={sampling}
        onCreated={() => {}}
        onCancel={() => {}}
      />,
    );
    expect(screen.getByTestId('catalog-form-SAMPLING')).toBeInTheDocument();
    expect(screen.getByTestId('catalog-field-sample_size')).toHaveValue(30);
  });
});
