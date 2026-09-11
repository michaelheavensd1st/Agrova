'use client';

import { FormEvent, type ReactNode, useEffect, useMemo, useRef, useState } from 'react';
import { ApiError, apiFetch } from '@/lib/api';
import {
  listBusinessPartners,
  QUALIFICATION_LABELS,
  type BusinessPartner,
} from '@/lib/business-partners';
import type { InventoryItem } from '@/lib/inventory-items';
import { parsePurchaseOrderDecimal } from '@/lib/purchase-order-decimals';
import type {
  CreatePurchaseOrderBody,
  DeliveryAddressInput,
  PurchaseOrder,
  PurchaseOrderUpdateLineInput,
  UpdatePurchaseOrderBody,
} from '@/lib/purchase-orders';
import type { Farm } from '@/lib/types';
import type { CurrentUser } from '@/lib/types';
import { hasScopedPermission } from '@/lib/permissions';
import {
  PurchaseOrderLineEditor,
  type PurchaseOrderLineFormValue,
} from './PurchaseOrderLineEditor';

export interface PurchaseOrderFormValues {
  businessPartnerId: string;
  farmId: string;
  currencyCode: string;
  orderDate: string;
  expectedDeliveryDate: string;
  supplierReference: string;
  notes: string;
  address: {
    line1: string;
    line2: string;
    city: string;
    region: string;
    postalCode: string;
    countryCode: string;
  };
  lines: PurchaseOrderLineFormValue[];
}

export type PurchaseOrderFormErrors = Record<string, string>;

const TOP_LEVEL_FIELD_ALIASES: Record<string, string> = {
  business_partner_id: 'business_partner_id',
  farm_id: 'farm_id',
  currency: 'currency_code',
  currency_code: 'currency_code',
  order_date: 'order_date',
  expected_delivery_date: 'expected_delivery_date',
  supplier_reference: 'supplier_reference',
  notes: 'notes',
  lines: 'lines',
};
const ADDRESS_FIELD_ALIASES: Record<string, string> = {
  line1: 'line1',
  line2: 'line2',
  city: 'city',
  state: 'region',
  region: 'region',
  postal_code: 'postal_code',
  country: 'country_code',
  country_code: 'country_code',
};
const LINE_FIELD_ALIASES: Record<string, string> = {
  inventory_item_id: 'inventory_item_id',
  ordered_quantity: 'ordered_quantity',
  unit_price: 'unit_price',
  unit: 'ordered_unit',
  ordered_unit: 'ordered_unit',
  description: 'description',
  line_note: 'line_note',
};
const DELIVERY_ERROR_CODES = new Set([
  'invalid_delivery_address',
  'invalid_country_code',
  'purchase_order_invalid_delivery_date',
]);
const APPROVED_DOMAIN_MESSAGES: Record<string, string> = {
  invalid_currency: 'Use a supported three-letter currency code.',
  purchase_order_invalid_delivery_date: 'Expected delivery cannot be before the order date.',
  invalid_delivery_address: 'Review the delivery address and try again.',
  invalid_country_code: 'Use a supported two-letter delivery country code.',
  business_partner_inactive: 'The selected supplier is inactive.',
  business_partner_not_supplier: 'Select a business partner that is available as a supplier.',
  business_partner_not_approved: 'The selected supplier is not approved for purchasing.',
  business_partner_blocked: 'The selected supplier is blocked for purchasing.',
  supplier_unavailable: 'The selected supplier is no longer available.',
  unit_incompatible: 'Select a unit compatible with the inventory item.',
  ordered_unit_mismatch: 'The selected unit does not match the ordered unit.',
  purchase_order_line_note_required: 'Add a note for this Purchase Order line.',
};
const SAVE_FALLBACK = 'Unable to save this Draft. Review the form and try again.';

function normalizeValidationLocation(location: unknown[]): string | null {
  const parts = location.filter((part) => part !== 'body').map(String);
  if (parts.length === 1) return TOP_LEVEL_FIELD_ALIASES[parts[0]] ?? null;
  if (parts[0] === 'delivery_address' && parts.length === 2) {
    const field = ADDRESS_FIELD_ALIASES[parts[1]];
    return field ? `delivery_address.${field}` : null;
  }
  if (parts[0] === 'lines' && /^\d+$/.test(parts[1] ?? '') && parts.length === 3) {
    const field = LINE_FIELD_ALIASES[parts[2]];
    return field ? `lines.${parts[1]}.${field}` : null;
  }
  return null;
}

function normalizeDomainField(
  code: string | undefined,
  context: Record<string, unknown>,
  message: string,
): string | null {
  if (code === 'invalid_currency') return 'currency_code';
  if (code === 'purchase_order_invalid_delivery_date') return 'expected_delivery_date';
  if (code?.startsWith('business_partner_')) return 'business_partner_id';

  const rawField = typeof context.field === 'string' ? context.field : null;
  const addressScoped =
    Boolean(code && DELIVERY_ERROR_CODES.has(code)) ||
    message.toLowerCase().includes('delivery_address');
  if (addressScoped) {
    const addressField =
      ADDRESS_FIELD_ALIASES[
        rawField ?? (typeof context.country_code === 'string' ? 'country_code' : '')
      ];
    if (addressField) return `delivery_address.${addressField}`;
  }

  const lineNumber =
    typeof context.line_number === 'number' && Number.isInteger(context.line_number)
      ? context.line_number
      : null;
  if (lineNumber !== null && lineNumber > 0) {
    const inferredField =
      rawField ?? (code === 'purchase_order_line_note_required' ? 'line_note' : null);
    const lineField = inferredField ? LINE_FIELD_ALIASES[inferredField] : null;
    if (lineField) return `lines.${lineNumber - 1}.${lineField}`;
  }
  return rawField ? (TOP_LEVEL_FIELD_ALIASES[rawField] ?? null) : null;
}

export function mapPurchaseOrderFormError(caught: unknown): {
  fields: PurchaseOrderFormErrors;
  message: string;
} {
  if (!(caught instanceof ApiError)) return { fields: {}, message: SAVE_FALLBACK };
  if (caught.status >= 500)
    return { fields: {}, message: 'Something went wrong. Please try again.' };

  const detail = caught.payload.detail as unknown;
  const fields: PurchaseOrderFormErrors = {};
  if (Array.isArray(detail)) {
    for (const entry of detail as Array<{ loc?: unknown[]; msg?: string }>) {
      const field = normalizeValidationLocation(entry.loc ?? []);
      if (field) fields[field] = 'Review this value and try again.';
    }
    return { fields, message: SAVE_FALLBACK };
  }
  if (detail && typeof detail === 'object') {
    const object = detail as {
      code?: string;
      message?: string;
      context?: Record<string, unknown>;
    };
    const approvedMessage = object.code ? APPROVED_DOMAIN_MESSAGES[object.code] : undefined;
    const message = approvedMessage ?? SAVE_FALLBACK;
    const field = normalizeDomainField(object.code, object.context ?? {}, message);
    if (field && approvedMessage) fields[field] = message;
    return { fields, message };
  }
  return { fields, message: SAVE_FALLBACK };
}

let newLineSequence = 0;
export function newPurchaseOrderLine(): PurchaseOrderLineFormValue {
  newLineSequence += 1;
  return {
    rowKey: `new-line-${newLineSequence}`,
    inventoryItemId: '',
    orderedQuantity: '',
    orderedUnit: 'count',
    unitPrice: '',
    description: '',
    lineNote: '',
  };
}

export function purchaseOrderFormValues(po?: PurchaseOrder | null): PurchaseOrderFormValues {
  return {
    businessPartnerId: po?.business_partner_id ?? '',
    farmId: po?.farm_id ?? '',
    currencyCode: po?.currency_code ?? 'USD',
    orderDate: po?.order_date ?? new Date().toISOString().slice(0, 10),
    expectedDeliveryDate: po?.expected_delivery_date ?? '',
    supplierReference: po?.supplier_reference ?? '',
    notes: po?.notes ?? '',
    address: {
      line1: po?.delivery_address?.line1 ?? '',
      line2: po?.delivery_address?.line2 ?? '',
      city: po?.delivery_address?.city ?? '',
      region: po?.delivery_address?.region ?? '',
      postalCode: po?.delivery_address?.postal_code ?? '',
      countryCode: po?.delivery_address?.country_code ?? '',
    },
    lines:
      po?.lines.map((line) => ({
        rowKey: `persisted-${line.id}`,
        id: line.id,
        inventoryItemId: line.inventory_item_id,
        orderedQuantity: line.ordered_quantity,
        orderedUnit: line.ordered_unit,
        unitPrice: line.unit_price,
        description: line.description,
        lineNote: line.line_note ?? '',
      })) ?? [],
  };
}

function nullable(value: string): string | null {
  return value.trim() || null;
}
function addressValue(value: PurchaseOrderFormValues['address']): DeliveryAddressInput | null {
  const result: DeliveryAddressInput = {
    line1: nullable(value.line1),
    line2: nullable(value.line2),
    city: nullable(value.city),
    region: nullable(value.region),
    postal_code: nullable(value.postalCode),
    country_code: nullable(value.countryCode)?.toUpperCase() ?? null,
  };
  return Object.values(result).every((candidate) => candidate === null) ? null : result;
}
function linePayload(
  line: PurchaseOrderLineFormValue,
  includeId: boolean,
): PurchaseOrderUpdateLineInput {
  return {
    ...(includeId && line.id ? { id: line.id } : {}),
    inventory_item_id: line.inventoryItemId,
    ordered_quantity: line.orderedQuantity,
    ordered_unit: line.orderedUnit,
    unit_price: line.unitPrice,
    description: nullable(line.description),
    line_note: nullable(line.lineNote),
  };
}

export function buildCreatePurchaseOrderBody(
  values: PurchaseOrderFormValues,
): CreatePurchaseOrderBody {
  return {
    business_partner_id: values.businessPartnerId,
    currency_code: values.currencyCode.trim().toUpperCase(),
    order_date: values.orderDate,
    ...(values.expectedDeliveryDate ? { expected_delivery_date: values.expectedDeliveryDate } : {}),
    ...(addressValue(values.address) ? { delivery_address: addressValue(values.address) } : {}),
    ...(nullable(values.supplierReference)
      ? { supplier_reference: nullable(values.supplierReference) }
      : {}),
    ...(nullable(values.notes) ? { notes: nullable(values.notes) } : {}),
    ...(values.farmId ? { farm_id: values.farmId } : {}),
    lines: values.lines.map((line) => linePayload(line, false)),
  };
}

function same(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}
export function buildUpdatePurchaseOrderBody(
  initial: PurchaseOrder,
  values: PurchaseOrderFormValues,
): UpdatePurchaseOrderBody {
  const body: UpdatePurchaseOrderBody = { expected_version: initial.version };
  const currency = values.currencyCode.trim().toUpperCase();
  if (values.businessPartnerId !== initial.business_partner_id)
    body.business_partner_id = values.businessPartnerId;
  if (values.farmId !== (initial.farm_id ?? '')) body.farm_id = values.farmId || null;
  if (currency !== initial.currency_code) body.currency_code = currency;
  if (values.orderDate !== initial.order_date) body.order_date = values.orderDate;
  if (values.expectedDeliveryDate !== (initial.expected_delivery_date ?? ''))
    body.expected_delivery_date = values.expectedDeliveryDate || null;
  if (nullable(values.supplierReference) !== initial.supplier_reference)
    body.supplier_reference = nullable(values.supplierReference);
  if (nullable(values.notes) !== initial.notes) body.notes = nullable(values.notes);
  const address = addressValue(values.address);
  if (!same(address, initial.delivery_address)) body.delivery_address = address;
  const lines = values.lines.map((line) => linePayload(line, true));
  const originalLines = initial.lines.map((line) => ({
    id: line.id,
    inventory_item_id: line.inventory_item_id,
    ordered_quantity: line.ordered_quantity,
    ordered_unit: line.ordered_unit,
    unit_price: line.unit_price,
    description: line.description || null,
    line_note: line.line_note,
  }));
  if (!same(lines, originalLines)) body.lines = lines;
  return body;
}

export function validatePurchaseOrderForm(
  values: PurchaseOrderFormValues,
): PurchaseOrderFormErrors {
  const errors: PurchaseOrderFormErrors = {};
  if (!values.businessPartnerId) errors.business_partner_id = 'Supplier is required.';
  if (!/^[A-Za-z]{3}$/.test(values.currencyCode.trim()))
    errors.currency_code = 'Enter a three-letter currency code.';
  if (!values.orderDate) errors.order_date = 'Order date is required.';
  if (
    values.expectedDeliveryDate &&
    values.orderDate &&
    values.expectedDeliveryDate < values.orderDate
  )
    errors.expected_delivery_date = 'Expected delivery cannot precede the order date.';
  if (values.address.countryCode && !/^[A-Za-z]{2}$/.test(values.address.countryCode.trim()))
    errors['delivery_address.country_code'] = 'Enter a two-letter country code.';
  values.lines.forEach((line, index) => {
    const prefix = `lines.${index}`;
    if (!line.inventoryItemId)
      errors[`${prefix}.inventory_item_id`] = 'Inventory item is required.';
    try {
      const quantity = parsePurchaseOrderDecimal(line.orderedQuantity);
      if (quantity.lte(0) || quantity.gte('1000000000000'))
        errors[`${prefix}.ordered_quantity`] =
          'Quantity must be greater than zero and below 1,000,000,000,000.';
    } catch {
      errors[`${prefix}.ordered_quantity`] = 'Enter a decimal with up to six places.';
    }
    try {
      const price = parsePurchaseOrderDecimal(line.unitPrice);
      if (price.lt(0) || price.gte('100000000000000'))
        errors[`${prefix}.unit_price`] =
          'Price must be zero or greater and below 100,000,000,000,000.';
      else if (price.isZero() && !line.lineNote.trim())
        errors[`${prefix}.line_note`] = 'A line note is required when price is zero.';
    } catch {
      errors[`${prefix}.unit_price`] = 'Enter a decimal with up to six places.';
    }
    if (!line.orderedUnit) errors[`${prefix}.ordered_unit`] = 'Unit is required.';
  });
  return errors;
}

export function PurchaseOrderForm({
  mode,
  organizationId,
  initial,
  submitting,
  externalErrors = {},
  generalError,
  optionsRevision = 0,
  user,
  farmPermission,
  onUnauthorized,
  onSubmit,
  onCancel,
}: {
  mode: 'create' | 'edit';
  organizationId: string;
  initial?: PurchaseOrder | null;
  submitting: boolean;
  externalErrors?: PurchaseOrderFormErrors;
  generalError?: string | null;
  optionsRevision?: number;
  user?: CurrentUser | null;
  farmPermission?: 'purchase_order.create' | 'purchase_order.update';
  onUnauthorized?: () => void;
  onSubmit: (values: PurchaseOrderFormValues) => void;
  onCancel: () => void;
}) {
  const [values, setValues] = useState(() => purchaseOrderFormValues(initial));
  const [supplierSearch, setSupplierSearch] = useState('');
  const [localErrors, setLocalErrors] = useState<PurchaseOrderFormErrors>({});
  const [farms, setFarms] = useState<Farm[]>([]);
  const [suppliers, setSuppliers] = useState<BusinessPartner[]>([]);
  const [items, setItems] = useState<InventoryItem[]>([]);
  const [optionsIdentity, setOptionsIdentity] = useState<string | null>(null);
  const [optionsLoading, setOptionsLoading] = useState(true);
  const generationRef = useRef(0);
  const onUnauthorizedRef = useRef(onUnauthorized);
  onUnauthorizedRef.current = onUnauthorized;
  const summaryRef = useRef<HTMLDivElement>(null);
  const formRef = useRef<HTMLFormElement>(null);
  const errors = { ...externalErrors, ...localErrors };
  const requestedOptionsIdentity = `${organizationId}\u0000${supplierSearch.trim()}`;
  useEffect(() => {
    if (generalError || Object.keys(externalErrors).length > 0)
      window.setTimeout(() => {
        const invalid = formRef.current?.querySelector<HTMLElement>('[aria-invalid="true"]');
        (invalid ?? summaryRef.current)?.focus();
      }, 0);
  }, [externalErrors, generalError]);
  useEffect(() => {
    const generation = ++generationRef.current;
    const controller = new AbortController();
    setOptionsLoading(true);
    setOptionsIdentity(null);
    setFarms([]);
    setSuppliers([]);
    setItems([]);
    setLocalErrors((current) => {
      if (!current.options) return current;
      const next = { ...current };
      delete next.options;
      return next;
    });
    void Promise.all([
      apiFetch<Farm[]>(`/v1/organizations/${organizationId}/farms`, { signal: controller.signal }),
      listBusinessPartners({
        organizationId,
        capability: 'supplier',
        active: true,
        search: supplierSearch.trim() || undefined,
        limit: 200,
      }),
      apiFetch<InventoryItem[]>(
        `/v1/organizations/${organizationId}/inventory-items?operational_only=true`,
        { signal: controller.signal },
      ),
    ])
      .then(([farmRows, partnerPage, itemRows]) => {
        if (generation !== generationRef.current) return;
        setFarms(
          farmRows.filter((farm) => (farm as Farm & { is_active?: boolean }).is_active !== false),
        );
        setSuppliers(partnerPage.items);
        setItems(itemRows.filter((item) => item.is_active && !item.deleted_at));
        setOptionsIdentity(requestedOptionsIdentity);
      })
      .catch((caught) => {
        if (
          generation !== generationRef.current ||
          (caught instanceof DOMException && caught.name === 'AbortError')
        )
          return;
        if (caught instanceof ApiError && caught.status === 401) {
          onUnauthorizedRef.current?.();
          return;
        }
        setLocalErrors({
          options:
            caught instanceof ApiError && caught.status >= 500
              ? 'Something went wrong. Please try again.'
              : 'Unable to load form options.',
        });
      })
      .finally(() => {
        if (generation === generationRef.current) setOptionsLoading(false);
      });
    return () => {
      generationRef.current += 1;
      controller.abort();
    };
  }, [optionsRevision, organizationId, requestedOptionsIdentity, supplierSearch]);
  const visible = optionsIdentity === requestedOptionsIdentity;
  const visibleFarms = visible
    ? farms.filter(
        (farm) =>
          !user ||
          !farmPermission ||
          hasScopedPermission(user, farmPermission, { organizationId, farmId: farm.id }),
      )
    : [];
  const visibleSuppliers = visible ? suppliers : [];
  const visibleItems = visible ? items : [];
  const dirty = useMemo(
    () =>
      mode === 'create'
        ? true
        : Object.keys(buildUpdatePurchaseOrderBody(initial!, values)).length > 1,
    [initial, mode, values],
  );
  function submit(event: FormEvent) {
    event.preventDefault();
    if (submitting) return;
    const found = validatePurchaseOrderForm(values);
    setLocalErrors(found);
    if (Object.keys(found).length) {
      window.setTimeout(() => {
        const invalid = formRef.current?.querySelector<HTMLElement>('[aria-invalid="true"]');
        (invalid ?? summaryRef.current)?.focus();
      }, 0);
      return;
    }
    onSubmit(values);
  }
  function set<K extends keyof PurchaseOrderFormValues>(key: K, value: PurchaseOrderFormValues[K]) {
    setValues((current) => ({ ...current, [key]: value }));
    setLocalErrors({});
  }
  return (
    <form ref={formRef} onSubmit={submit} data-testid={`po-form-${mode}`} className="space-y-6">
      {(generalError || errors.options || Object.keys(errors).length > 0) && (
        <div
          ref={summaryRef}
          tabIndex={-1}
          role="alert"
          aria-live="assertive"
          className="rounded-md bg-destructive/10 p-3 text-sm text-destructive"
        >
          <p className="font-medium">Please review the form.</p>
          {generalError && <p>{generalError}</p>}
          {errors.options && <p>{errors.options}</p>}
        </div>
      )}
      <fieldset
        disabled={submitting || optionsLoading}
        className="grid gap-4 rounded-xl border border-border p-5 sm:grid-cols-2 lg:grid-cols-3"
        aria-busy={optionsLoading || submitting}
      >
        <legend className="px-2 font-display text-lg">Draft details</legend>
        <InputField
          label="Search suppliers"
          value={supplierSearch}
          onChange={setSupplierSearch}
          testId="po-form-supplier-search"
        />
        <SelectField
          label="Supplier"
          value={values.businessPartnerId}
          onChange={(value) => set('businessPartnerId', value)}
          error={errors.business_partner_id}
          testId="po-form-supplier"
        >
          <option value="">Select supplier</option>
          {visibleSuppliers.map((supplier) => (
            <option key={supplier.id} value={supplier.id}>
              {supplier.code} — {supplier.trading_name || supplier.legal_name} ·{' '}
              {supplier.supplier_profile
                ? QUALIFICATION_LABELS[supplier.supplier_profile.qualification_status]
                : 'No qualification profile'}
            </option>
          ))}
        </SelectField>
        <SelectField
          label="Farm"
          value={values.farmId}
          onChange={(value) => set('farmId', value)}
          error={errors.farm_id}
          testId="po-form-farm"
        >
          {(!user ||
            !farmPermission ||
            hasScopedPermission(user, farmPermission, { organizationId })) && (
            <option value="">Organization-wide</option>
          )}
          {visibleFarms.map((farm) => (
            <option key={farm.id} value={farm.id}>
              {farm.code} — {farm.name}
            </option>
          ))}
        </SelectField>
        <InputField
          label="Currency"
          value={values.currencyCode}
          onChange={(value) => set('currencyCode', value.toUpperCase())}
          error={errors.currency_code}
          testId="po-form-currency"
          maxLength={3}
          required
        />
        <InputField
          label="Order date"
          value={values.orderDate}
          onChange={(value) => set('orderDate', value)}
          error={errors.order_date}
          testId="po-form-order-date"
          type="date"
          required
        />
        <InputField
          label="Expected delivery"
          value={values.expectedDeliveryDate}
          onChange={(value) => set('expectedDeliveryDate', value)}
          error={errors.expected_delivery_date}
          testId="po-form-delivery-date"
          type="date"
        />
        <InputField
          label="Supplier reference"
          value={values.supplierReference}
          onChange={(value) => set('supplierReference', value)}
          error={errors.supplier_reference}
          testId="po-form-reference"
          maxLength={120}
        />
        <label className="text-sm sm:col-span-2 lg:col-span-3">
          <span className="mb-1 block text-muted-foreground">Notes</span>
          <textarea
            value={values.notes}
            onChange={(event) => set('notes', event.target.value)}
            maxLength={4000}
            rows={3}
            data-testid="po-form-notes"
            aria-invalid={Boolean(errors.notes)}
            aria-describedby={errors.notes ? 'po-form-notes-error' : undefined}
            className="w-full rounded-md border bg-background px-3 py-2"
          />
          {errors.notes && <ErrorText id="po-form-notes-error" message={errors.notes} />}
        </label>
      </fieldset>
      <fieldset
        disabled={submitting}
        className="grid gap-3 rounded-xl border border-border p-5 sm:grid-cols-2 lg:grid-cols-3"
      >
        <legend className="px-2 font-display text-lg">Delivery address</legend>
        {(['line1', 'line2', 'city', 'region', 'postalCode', 'countryCode'] as const).map(
          (field) => (
            <InputField
              key={field}
              label={
                {
                  line1: 'Line 1',
                  line2: 'Line 2',
                  city: 'City',
                  region: 'Region',
                  postalCode: 'Postal code',
                  countryCode: 'Country code',
                }[field]
              }
              value={values.address[field]}
              onChange={(value) =>
                set('address', {
                  ...values.address,
                  [field]: field === 'countryCode' ? value.toUpperCase() : value,
                })
              }
              error={
                errors[
                  `delivery_address.${field === 'postalCode' ? 'postal_code' : field === 'countryCode' ? 'country_code' : field}`
                ]
              }
              testId={`po-form-address-${field}`}
              maxLength={field === 'countryCode' ? 2 : 200}
            />
          ),
        )}
      </fieldset>
      <PurchaseOrderLineEditor
        lines={values.lines}
        items={visibleItems}
        errors={errors}
        disabled={submitting || optionsLoading}
        onChange={(lines) => set('lines', lines)}
        onAdd={() => set('lines', [...values.lines, newPurchaseOrderLine()])}
      />
      <div className="flex flex-wrap justify-end gap-2">
        <button
          type="button"
          onClick={onCancel}
          disabled={submitting}
          className="rounded-md border px-4 py-2"
        >
          Cancel
        </button>
        <button
          type="submit"
          disabled={submitting || optionsLoading || (mode === 'edit' && !dirty)}
          data-testid="po-form-submit"
          className="rounded-md bg-primary px-4 py-2 text-primary-foreground disabled:opacity-50"
        >
          {submitting ? 'Saving…' : mode === 'create' ? 'Create Draft' : 'Save Draft'}
        </button>
      </div>
    </form>
  );
}

function InputField({
  label,
  value,
  onChange,
  error,
  testId,
  type = 'text',
  maxLength,
  required,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  error?: string;
  testId: string;
  type?: string;
  maxLength?: number;
  required?: boolean;
}) {
  const errorId = `${testId}-error`;
  return (
    <label className="text-sm">
      <span className="mb-1 block text-muted-foreground">
        {label}
        {required && ' *'}
      </span>
      <input
        type={type}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        maxLength={maxLength}
        required={required}
        data-testid={testId}
        aria-invalid={Boolean(error)}
        aria-describedby={error ? errorId : undefined}
        className="w-full rounded-md border bg-background px-3 py-2"
      />
      {error && <ErrorText id={errorId} message={error} />}
    </label>
  );
}
function SelectField({
  label,
  value,
  onChange,
  error,
  testId,
  children,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  error?: string;
  testId: string;
  children: ReactNode;
}) {
  const errorId = `${testId}-error`;
  return (
    <label className="text-sm">
      <span className="mb-1 block text-muted-foreground">{label}</span>
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        data-testid={testId}
        aria-invalid={Boolean(error)}
        aria-describedby={error ? errorId : undefined}
        className="w-full rounded-md border bg-background px-3 py-2"
      >
        {children}
      </select>
      {error && <ErrorText id={errorId} message={error} />}
    </label>
  );
}
function ErrorText({ id, message }: { id?: string; message: string }) {
  return (
    <span id={id} role="alert" className="mt-1 block text-xs text-destructive">
      {message}
    </span>
  );
}
