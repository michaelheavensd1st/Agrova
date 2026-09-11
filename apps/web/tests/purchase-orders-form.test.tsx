import { describe, expect, it, vi, beforeEach } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';

vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api');
  return { ...actual, apiFetch: vi.fn() };
});

import { ApiError, apiFetch } from '@/lib/api';
import {
  PurchaseOrderForm,
  buildCreatePurchaseOrderBody,
  buildUpdatePurchaseOrderBody,
  mapPurchaseOrderFormError,
  newPurchaseOrderLine,
  purchaseOrderFormValues,
  validatePurchaseOrderForm,
} from '@/components/purchase-orders/PurchaseOrderForm';
import { PurchaseOrderConflictPanel } from '@/components/purchase-orders/PurchaseOrderConflictPanel';
import type { PurchaseOrder } from '@/lib/purchase-orders';
import type { CurrentUser } from '@/lib/types';

const mockedApiFetch = vi.mocked(apiFetch);

function po(overrides: Partial<PurchaseOrder> = {}): PurchaseOrder {
  return {
    id: 'po-1',
    organization_id: 'org-1',
    farm_id: 'farm-1',
    business_partner_id: 'bp-1',
    po_number: 'PO-1',
    supplier_reference: 'REF',
    status: 'DRAFT',
    currency_code: 'USD',
    order_date: '2026-08-01',
    expected_delivery_date: '2026-08-10',
    delivery_address: {
      line1: 'Road',
      line2: null,
      city: null,
      region: null,
      postal_code: null,
      country_code: 'GH',
    },
    notes: 'Original',
    supplier_code: 'SUP',
    supplier_legal_name: 'Supplier',
    supplier_trading_name: null,
    version: 4,
    created_by_id: 'u1',
    submitted_by_id: null,
    submitted_at: null,
    approved_by_id: null,
    approved_at: null,
    rejected_by_id: null,
    rejected_at: null,
    cancelled_by_id: null,
    cancelled_at: null,
    created_at: '2026-08-01T00:00:00Z',
    updated_at: '2026-08-01T00:00:00Z',
    subtotal: '2.000000',
    lines: [line('line-1', 'item-1'), line('line-2', 'item-2')],
    ...overrides,
  };
}
function line(id: string, item: string) {
  return {
    id,
    line_number: id === 'line-1' ? 1 : 2,
    inventory_item_id: item,
    item_code: item,
    item_name: item,
    item_sku: null,
    description: item,
    line_note: null,
    ordered_quantity: '1.000000',
    ordered_unit: 'kg',
    canonical_unit: 'kg',
    ordered_quantity_canonical: '1.000000',
    received_quantity: '0.000000',
    received_quantity_canonical: '0.000000',
    unit_price: '1.000000',
    extended_amount: '1.000000',
    created_at: '2026-08-01T00:00:00Z',
    updated_at: '2026-08-01T00:00:00Z',
  } as const;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}

beforeEach(() => {
  mockedApiFetch.mockReset();
  mockedApiFetch.mockImplementation((path: string) => {
    if (path.includes('/farms'))
      return Promise.resolve([
        {
          id: 'farm-1',
          organization_id: 'org-1',
          code: 'F1',
          name: 'Farm',
          deleted_at: null,
          is_active: true,
        },
      ] as never);
    if (path.includes('/business-partners'))
      return Promise.resolve({
        items: [
          {
            id: 'bp-1',
            code: 'SUP',
            legal_name: 'Supplier',
            trading_name: null,
            supplier_profile: { qualification_status: 'unqualified' },
          },
        ],
        next_cursor: null,
      } as never);
    if (path.includes('/inventory-items'))
      return Promise.resolve([
        {
          id: 'item-1',
          organization_id: 'org-1',
          code: 'I1',
          name: 'Item 1',
          sku: null,
          category: 'feed',
          canonical_unit: 'kg',
          description: null,
          is_active: true,
          metadata_json: null,
          deleted_at: null,
          created_at: '',
          updated_at: '',
        },
        {
          id: 'item-2',
          organization_id: 'org-1',
          code: 'I2',
          name: 'Item 2',
          sku: 'SKU2',
          category: 'feed',
          canonical_unit: 'kg',
          description: null,
          is_active: true,
          metadata_json: null,
          deleted_at: null,
          created_at: '',
          updated_at: '',
        },
      ] as never);
    return Promise.resolve([] as never);
  });
});

describe('PurchaseOrderForm semantics', () => {
  it('loads only operational inventory items for line selectors', async () => {
    render(
      <PurchaseOrderForm
        mode="create"
        organizationId="org-1"
        submitting={false}
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
      />,
    );

    await waitFor(() =>
      expect(mockedApiFetch).toHaveBeenCalledWith(
        '/v1/organizations/org-1/inventory-items?operational_only=true',
        expect.objectContaining({ signal: expect.any(AbortSignal) }),
      ),
    );
  });

  it('restricts create and edit farm choices to the applicable scoped permission', async () => {
    mockedApiFetch.mockImplementation((path: string) => {
      if (path.includes('/farms'))
        return Promise.resolve([
          { id: 'farm-1', organization_id: 'org-1', code: 'F1', name: 'Farm One', is_active: true },
          { id: 'farm-2', organization_id: 'org-1', code: 'F2', name: 'Farm Two', is_active: true },
        ] as never);
      if (path.includes('/business-partners'))
        return Promise.resolve({ items: [], next_cursor: null } as never);
      return Promise.resolve([] as never);
    });
    const scopedUser: CurrentUser = {
      id: 'u1',
      email: 'u@example.test',
      full_name: 'Scoped user',
      is_active: true,
      is_verified: true,
      is_superuser: false,
      permissions: [],
      permission_scopes: [
        {
          organization_id: 'org-1',
          farm_id: 'farm-1',
          permissions: ['purchase_order.create', 'purchase_order.update'],
        },
        {
          organization_id: 'org-2',
          farm_id: 'farm-2',
          permissions: ['purchase_order.create', 'purchase_order.update'],
        },
      ],
    };
    const props = {
      organizationId: 'org-1',
      submitting: false,
      user: scopedUser,
      onSubmit: vi.fn(),
      onCancel: vi.fn(),
    };
    const view = render(
      <PurchaseOrderForm {...props} mode="create" farmPermission="purchase_order.create" />,
    );
    await waitFor(() =>
      expect(screen.getByRole('option', { name: /Farm One/ })).toBeInTheDocument(),
    );
    expect(screen.queryByRole('option', { name: /Farm Two/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'Organization-wide' })).not.toBeInTheDocument();

    view.rerender(
      <PurchaseOrderForm
        {...props}
        mode="edit"
        initial={po()}
        farmPermission="purchase_order.update"
      />,
    );
    expect(screen.getByRole('option', { name: /Farm One/ })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: /Farm Two/ })).not.toBeInTheDocument();
  });

  it('routes selector 401 through the authentication callback without exposing details', async () => {
    const onUnauthorized = vi.fn();
    mockedApiFetch.mockRejectedValue(
      new ApiError(401, { detail: 'raw expired-session backend detail' }),
    );
    render(
      <PurchaseOrderForm
        mode="create"
        organizationId="org-1"
        submitting={false}
        onUnauthorized={onUnauthorized}
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    await waitFor(() => expect(onUnauthorized).toHaveBeenCalledTimes(1));
    expect(screen.queryByText(/raw expired-session/)).not.toBeInTheDocument();
    expect(screen.queryByText('Unable to load form options.')).not.toBeInTheDocument();
  });

  it('ignores a stale selector 401 after the form organization changes', async () => {
    const oldFarmRequest = deferred<never>();
    const onUnauthorized = vi.fn();
    mockedApiFetch.mockImplementation((path: string) => {
      if (path.includes('/org-1/farms')) return oldFarmRequest.promise;
      if (path.includes('/business-partners'))
        return Promise.resolve({ items: [], next_cursor: null } as never);
      return Promise.resolve([] as never);
    });
    const props = {
      mode: 'create' as const,
      submitting: false,
      onUnauthorized,
      onSubmit: vi.fn(),
      onCancel: vi.fn(),
    };
    const view = render(<PurchaseOrderForm {...props} organizationId="org-1" />);
    view.rerender(<PurchaseOrderForm {...props} organizationId="org-2" />);
    await waitFor(() => expect(screen.getByTestId('po-form-submit')).toBeEnabled());
    await act(async () => {
      oldFarmRequest.reject(new ApiError(401, { detail: 'stale session detail' }));
      try {
        await oldFarmRequest.promise;
      } catch {
        // Expected obsolete rejection.
      }
    });
    expect(onUnauthorized).not.toHaveBeenCalled();
    expect(screen.queryByText(/stale session detail/)).not.toBeInTheDocument();
  });

  it('clears a current selector error after a successful reload', async () => {
    let failing = true;
    mockedApiFetch.mockImplementation((path: string) => {
      if (failing) return Promise.reject(new ApiError(500, { detail: 'raw selector failure' }));
      if (path.includes('/farms'))
        return Promise.resolve([
          { id: 'farm-recovered', code: 'REC', name: 'Recovered Farm', is_active: true },
        ] as never);
      if (path.includes('/business-partners'))
        return Promise.resolve({ items: [], next_cursor: null } as never);
      return Promise.resolve([] as never);
    });
    const props = {
      mode: 'create' as const,
      organizationId: 'org-1',
      submitting: false,
      onSubmit: vi.fn(),
      onCancel: vi.fn(),
    };
    const view = render(<PurchaseOrderForm {...props} optionsRevision={0} />);
    expect(await screen.findByText('Something went wrong. Please try again.')).toBeInTheDocument();
    failing = false;
    view.rerender(<PurchaseOrderForm {...props} optionsRevision={1} />);
    expect(await screen.findByRole('option', { name: /Recovered Farm/ })).toBeInTheDocument();
    expect(screen.queryByText('Something went wrong. Please try again.')).not.toBeInTheDocument();
    expect(screen.queryByText(/raw selector failure/)).not.toBeInTheDocument();
  });

  it('does not let stale selector success clear a newer selector failure', async () => {
    const oldFarms = deferred<never[]>();
    mockedApiFetch.mockImplementation((path: string) => {
      if (path.includes('/org-1/farms')) return oldFarms.promise as never;
      if (path.includes('/org-2/'))
        return Promise.reject(new ApiError(500, { detail: 'new current failure' }));
      if (path.includes('/business-partners'))
        return Promise.resolve({ items: [], next_cursor: null } as never);
      return Promise.resolve([] as never);
    });
    const props = {
      mode: 'create' as const,
      submitting: false,
      onSubmit: vi.fn(),
      onCancel: vi.fn(),
    };
    const view = render(<PurchaseOrderForm {...props} organizationId="org-1" />);
    view.rerender(<PurchaseOrderForm {...props} organizationId="org-2" />);
    expect(await screen.findByText('Something went wrong. Please try again.')).toBeInTheDocument();
    await act(async () => {
      oldFarms.resolve([]);
      await oldFarms.promise;
    });
    expect(screen.getByText('Something went wrong. Please try again.')).toBeInTheDocument();
  });

  it('builds create payloads with exact maximum Decimal strings and no client IDs', () => {
    const values = purchaseOrderFormValues();
    values.businessPartnerId = 'bp-1';
    values.orderDate = '2026-08-01';
    values.lines = [
      {
        ...newPurchaseOrderLine(),
        inventoryItemId: 'item-1',
        orderedQuantity: '999999999999.999999',
        orderedUnit: 'kg',
        unitPrice: '99999999999999.999999',
      },
    ];
    const body = buildCreatePurchaseOrderBody(values);
    expect(body.lines?.[0]).toMatchObject({
      ordered_quantity: '999999999999.999999',
      unit_price: '99999999999999.999999',
    });
    expect(body.lines?.[0]).not.toHaveProperty('id');
  });

  it('builds a minimal version-aware header patch and explicit nullable clears', () => {
    const initial = po();
    const values = purchaseOrderFormValues(initial);
    values.notes = '';
    values.supplierReference = '';
    values.expectedDeliveryDate = '';
    expect(buildUpdatePurchaseOrderBody(initial, values)).toEqual({
      expected_version: 4,
      expected_delivery_date: null,
      supplier_reference: null,
      notes: null,
    });
  });

  it('returns only expected_version for a no-op edit', () => {
    const initial = po();
    expect(buildUpdatePurchaseOrderBody(initial, purchaseOrderFormValues(initial))).toEqual({
      expected_version: 4,
    });
  });

  it('preserves survivor UUIDs, omits new IDs, and represents deletion by omission', () => {
    const initial = po();
    const values = purchaseOrderFormValues(initial);
    values.lines = [
      values.lines[1],
      {
        ...newPurchaseOrderLine(),
        inventoryItemId: 'item-1',
        orderedQuantity: '2.000000',
        orderedUnit: 'kg',
        unitPrice: '3.000000',
      },
    ];
    const body = buildUpdatePurchaseOrderBody(initial, values);
    expect(body.lines?.[0].id).toBe('line-2');
    expect(body.lines?.[1]).not.toHaveProperty('id');
    expect(body.lines?.some((candidate) => candidate.id === 'line-1')).toBe(false);
  });

  it('validates syntax, bounds, zero-price notes, dates, and currency without Number conversion', () => {
    const values = purchaseOrderFormValues();
    values.businessPartnerId = 'bp';
    values.orderDate = '2026-08-10';
    values.expectedDeliveryDate = '2026-08-01';
    values.currencyCode = 'US';
    values.lines = [
      {
        ...newPurchaseOrderLine(),
        inventoryItemId: 'item',
        orderedQuantity: '1e3',
        unitPrice: '0.000000',
        orderedUnit: 'kg',
      },
    ];
    const errors = validatePurchaseOrderForm(values);
    expect(errors.currency_code).toBeTruthy();
    expect(errors.expected_delivery_date).toBeTruthy();
    expect(errors['lines.0.ordered_quantity']).toBeTruthy();
    expect(errors['lines.0.line_note']).toBeTruthy();
  });

  it('disables no-op edit save and exposes qualification state honestly', async () => {
    render(
      <PurchaseOrderForm
        mode="edit"
        organizationId="org-1"
        initial={po()}
        submitting={false}
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    await waitFor(() => expect(screen.getByTestId('po-form-submit')).toBeDisabled());
    expect(await screen.findByText(/Unqualified/)).toBeInTheDocument();
  });

  it('supports keyboard-operable add/remove without re-keying persisted rows', async () => {
    render(
      <PurchaseOrderForm
        mode="edit"
        organizationId="org-1"
        initial={po()}
        submitting={false}
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    await waitFor(() => expect(screen.getByTestId('po-line-persisted-line-1')).toBeInTheDocument());
    fireEvent.click(screen.getByLabelText('Remove line 1'));
    expect(screen.getByTestId('po-line-persisted-line-2')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('po-line-add'));
    expect(screen.getByTestId('po-line-persisted-line-2')).toBeInTheDocument();
  });

  it('synchronously hides org A selector data and rejects its late response under org B', async () => {
    const lateFarmA = deferred<unknown[]>();
    mockedApiFetch.mockImplementation((path: string) => {
      if (path === '/v1/organizations/org-1/farms') return lateFarmA.promise as never;
      if (path === '/v1/organizations/org-2/farms')
        return Promise.resolve([
          { id: 'farm-b', organization_id: 'org-2', code: 'FB', name: 'Farm B', is_active: true },
        ] as never);
      if (path.includes('/business-partners'))
        return Promise.resolve({ items: [], next_cursor: null } as never);
      if (path.includes('/inventory-items')) return Promise.resolve([] as never);
      return Promise.resolve([] as never);
    });
    const props = {
      mode: 'create' as const,
      submitting: false,
      onSubmit: vi.fn(),
      onCancel: vi.fn(),
    };
    const view = render(<PurchaseOrderForm {...props} organizationId="org-1" />);
    view.rerender(<PurchaseOrderForm {...props} organizationId="org-2" />);
    expect(screen.queryByRole('option', { name: /Farm A/ })).not.toBeInTheDocument();
    expect(await screen.findByRole('option', { name: /Farm B/ })).toBeInTheDocument();
    lateFarmA.resolve([
      { id: 'farm-a', organization_id: 'org-1', code: 'FA', name: 'Farm A', is_active: true },
    ]);
    await waitFor(() =>
      expect(screen.queryByRole('option', { name: /Farm A/ })).not.toBeInTheDocument(),
    );
    expect(screen.getByRole('option', { name: /Farm B/ })).toBeInTheDocument();
  });

  it('renders an accessible conflict panel with both explicit choices', () => {
    render(
      <PurchaseOrderConflictPanel
        originalVersion={4}
        latest={po({ version: 5 })}
        onReviewLatest={vi.fn()}
        onDiscard={vi.fn()}
      />,
    );
    expect(screen.getByRole('alert')).toHaveTextContent('version 4');
    expect(screen.getByRole('alert')).toHaveFocus();
    expect(screen.getByRole('button', { name: 'Review latest' })).toBeEnabled();
    expect(screen.getByRole('button', { name: /Discard local edits/ })).toBeEnabled();
  });

  it('sanitizes ambiguous form errors while retaining structured field attribution', () => {
    const hostile = 'SQLSTATE driver stack SELECT secret FROM tenant';
    const ambiguous = mapPurchaseOrderFormError(new ApiError(422, { detail: hostile }));
    expect(ambiguous).toEqual({
      fields: {},
      message: 'Unable to save this Draft. Review the form and try again.',
    });
    const indexed = mapPurchaseOrderFormError(
      new ApiError(422, {
        detail: [{ loc: ['body', 'currency_code'], msg: hostile }] as never,
      }),
    );
    expect(indexed.fields.currency_code).toBe('Review this value and try again.');
    expect(JSON.stringify(indexed)).not.toContain(hostile);

    const recognized = mapPurchaseOrderFormError(
      domainError('invalid_currency', hostile, { field: 'currency_code' }),
    );
    expect(recognized).toEqual({
      fields: { currency_code: 'Use a supported three-letter currency code.' },
      message: 'Use a supported three-letter currency code.',
    });
    expect(JSON.stringify(recognized)).not.toContain(hostile);
  });

  it('maps an unsupported domain currency to the rendered currency control and focuses it', async () => {
    const mapped = mapPurchaseOrderFormError(
      domainError('invalid_currency', 'currency_code is not an official ISO 4217 code.', {}),
    );
    const props = {
      mode: 'create' as const,
      organizationId: 'org-1',
      submitting: false,
      onSubmit: vi.fn(),
      onCancel: vi.fn(),
    };
    const view = render(<PurchaseOrderForm {...props} />);
    await waitFor(() => expect(screen.getByTestId('po-form-submit')).toBeEnabled());
    view.rerender(
      <PurchaseOrderForm {...props} externalErrors={mapped.fields} generalError={mapped.message} />,
    );
    const currency = screen.getByTestId('po-form-currency');
    await waitFor(() => expect(currency).toHaveFocus());
    expect(currency).toHaveAttribute('aria-invalid', 'true');
    expect(currency).toHaveAccessibleDescription('Use a supported three-letter currency code.');
    expect(screen.queryByText(/"code"|"context"/)).not.toBeInTheDocument();
  });

  it('maps invalid country and delivery line1 domain contexts to edit address controls', async () => {
    const props = {
      mode: 'edit' as const,
      organizationId: 'org-1',
      initial: po(),
      submitting: false,
      onSubmit: vi.fn(),
      onCancel: vi.fn(),
    };
    const view = render(<PurchaseOrderForm {...props} />);
    await waitFor(() => expect(screen.getByTestId('po-form-submit')).toBeDisabled());
    const country = mapPurchaseOrderFormError(
      domainError('invalid_country_code', 'The delivery country is invalid.', {
        country_code: 'ZZ',
      }),
    );
    view.rerender(
      <PurchaseOrderForm
        {...props}
        externalErrors={country.fields}
        generalError={country.message}
      />,
    );
    const countryInput = screen.getByTestId('po-form-address-countryCode');
    await waitFor(() => expect(countryInput).toHaveFocus());
    expect(countryInput).toHaveAttribute('aria-invalid', 'true');
    expect(countryInput).toHaveAccessibleDescription(
      'Use a supported two-letter delivery country code.',
    );

    const line1 = mapPurchaseOrderFormError(
      domainError('invalid_delivery_address', 'delivery_address.line1 is invalid.', {
        field: 'line1',
      }),
    );
    view.rerender(
      <PurchaseOrderForm {...props} externalErrors={line1.fields} generalError={line1.message} />,
    );
    const line1Input = screen.getByTestId('po-form-address-line1');
    await waitFor(() => expect(line1Input).toHaveFocus());
    expect(line1Input).toHaveAttribute('aria-invalid', 'true');
    expect(line1Input).toHaveAccessibleDescription('Review the delivery address and try again.');
  });

  it('preserves indexed Pydantic line mapping and leaves ambiguous fields summary-only', async () => {
    const indexed = mapPurchaseOrderFormError(
      new ApiError(422, {
        detail: [
          { loc: ['body', 'lines', 0, 'unit_price'], msg: 'Enter a legal price.' },
        ] as unknown as string,
      }),
    );
    const props = {
      mode: 'edit' as const,
      organizationId: 'org-1',
      initial: po(),
      submitting: false,
      onSubmit: vi.fn(),
      onCancel: vi.fn(),
    };
    const view = render(<PurchaseOrderForm {...props} />);
    await waitFor(() => expect(screen.getByTestId('po-line-0-price')).toBeEnabled());
    view.rerender(
      <PurchaseOrderForm
        {...props}
        externalErrors={indexed.fields}
        generalError={indexed.message}
      />,
    );
    const price = screen.getByTestId('po-line-0-price');
    await waitFor(() => expect(price).toHaveFocus());
    expect(price).toHaveAccessibleDescription('Review this value and try again.');
    expect(screen.queryByText('Enter a legal price.')).not.toBeInTheDocument();

    const ambiguous = mapPurchaseOrderFormError(
      domainError('unknown_rule', 'Review the supplied Draft.', { field: 'mystery' }),
    );
    expect(ambiguous.fields).toEqual({});
    view.rerender(
      <PurchaseOrderForm
        {...props}
        externalErrors={ambiguous.fields}
        generalError={ambiguous.message}
      />,
    );
    await waitFor(() => expect(screen.getByRole('alert')).toHaveFocus());
    expect(
      screen.getByText('Unable to save this Draft. Review the form and try again.'),
    ).toBeInTheDocument();
    expect(screen.queryByText(/"mystery"/)).not.toBeInTheDocument();
  });
});

function domainError(code: string, message: string, context: Record<string, unknown>): ApiError {
  return new ApiError(422, {
    detail: { code, message, context } as unknown as string,
  });
}
