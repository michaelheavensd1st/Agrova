'use client';

/**
 * Sprint 5.4 — Unified stock operation dialog.
 *
 * A single dialog backs every ledger operation (receive / issue /
 * transfer / adjust / reverse). Only the operation-specific fields
 * differ; the container owns everything else:
 *
 *   - form state + validation;
 *   - pending / idempotency key discipline;
 *   - route/generation guards (orgId + itemId + warehouseId + lotId);
 *   - centralised 401/403/404 handling;
 *   - confirmation summary;
 *   - close/reset on success.
 *
 * The framework is intentionally colocated in a single file to keep
 * the number of new modules small. Each operation is a thin
 * render-fields block below the shared shell.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { apiFetch, ApiError } from '@/lib/api';
import { friendlyError, toast } from '@/components/ui-polish';
import {
  buildRequest,
  destinationOptions,
  initialForm,
  lotsForWarehouse,
  makeIdempotencyKey,
  payloadFingerprint,
  STOCK_OPERATION_LABELS,
  type StockOperationForm,
  type StockOperationType,
  validateForm,
  warehouseOptionsForStockOperation,
} from '@/lib/stock-operations';
import type { InventoryItem, ItemLedgerTx, ItemLot, ItemWarehouse } from '@/lib/inventory-items';

// ------------------------------------------------------------------ //
// Focus management helper                                            //
// ------------------------------------------------------------------ //
/**
 * Sprint 5.4.1 — collect the tabbable elements inside a dialog.
 * The heuristic is intentionally simple: it includes native form
 * controls and links with `href`, excludes anything disabled or
 * with `tabindex="-1"`. This is enough for our fields + footer
 * buttons; we don't ship any custom widgets that need special
 * handling.
 */
function getTabbableElements(root: HTMLElement): HTMLElement[] {
  const selector = [
    'button:not([disabled])',
    '[href]',
    'input:not([disabled])',
    'select:not([disabled])',
    'textarea:not([disabled])',
    '[tabindex]:not([tabindex="-1"])',
  ].join(',');
  return Array.from(root.querySelectorAll<HTMLElement>(selector)).filter((el) => {
    // A `tabindex="-1"` on the element itself is caught above; but
    // we also want to skip hidden-from-AT nodes.
    if (el.hasAttribute('disabled')) return false;
    if (el.getAttribute('aria-hidden') === 'true') return false;
    return true;
  });
}

// ------------------------------------------------------------------ //
// Types                                                              //
// ------------------------------------------------------------------ //
export interface StockOperationDialogProps {
  open: boolean;
  type: StockOperationType;
  organizationId: string;
  item: InventoryItem;
  warehouses: readonly ItemWarehouse[];
  lots: readonly ItemLot[];
  // Reversal-specific: the transaction being reversed. When
  // `type === 'reverse'` this is required.
  reversalTx?: ItemLedgerTx & { warehouse_id?: string };
  onClose(): void;
  onSuccess(): void | Promise<void>;
  /** 401 handler — the parent owns router.push('/login'). */
  onUnauthenticated(): void;
}

type StockOperationField =
  | 'warehouseId'
  | 'destinationWarehouseId'
  | 'lotId'
  | 'lotCode'
  | 'expiryDate'
  | 'quantity'
  | 'reason'
  | 'direction';

type ApiValidationField =
  | 'warehouse_id'
  | 'destination_warehouse_id'
  | 'lot_id'
  | 'lot_code'
  | 'expiry_date'
  | 'quantity'
  | 'reason'
  | 'direction';

const API_VALIDATION_FIELD_MAP: Record<ApiValidationField, StockOperationField> = {
  warehouse_id: 'warehouseId',
  destination_warehouse_id: 'destinationWarehouseId',
  lot_id: 'lotId',
  lot_code: 'lotCode',
  expiry_date: 'expiryDate',
  quantity: 'quantity',
  reason: 'reason',
  direction: 'direction',
};

const VISIBLE_FIELDS: Record<StockOperationType, ReadonlySet<StockOperationField>> = {
  receive: new Set(['warehouseId', 'lotCode', 'expiryDate', 'quantity', 'reason']),
  issue: new Set(['warehouseId', 'lotId', 'quantity', 'reason']),
  transfer: new Set(['warehouseId', 'destinationWarehouseId', 'lotId', 'quantity', 'reason']),
  adjust: new Set(['warehouseId', 'lotId', 'direction', 'quantity', 'reason']),
  reverse: new Set(['reason']),
};

const GENERIC_VALIDATION_ERROR = 'Some validation errors could not be matched to a form field.';

function operationLabel(type: StockOperationType, reversalTx?: ItemLedgerTx): string {
  if (
    type === 'reverse' &&
    (reversalTx?.transaction_type === 'transfer_out' ||
      reversalTx?.transaction_type === 'transfer_in')
  ) {
    return 'Reverse transfer';
  }
  return STOCK_OPERATION_LABELS[type];
}

// ------------------------------------------------------------------ //
// Component                                                          //
// ------------------------------------------------------------------ //
export function StockOperationDialog(props: StockOperationDialogProps) {
  const {
    open,
    type,
    organizationId,
    item,
    warehouses,
    lots,
    reversalTx,
    onClose,
    onSuccess,
    onUnauthenticated,
  } = props;

  const [form, setForm] = useState<StockOperationForm>(() => initialForm(type));
  const [fieldErrors, setFieldErrors] = useState<Partial<Record<StockOperationField, string>>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);

  // Idempotency-Key discipline:
  //  - one key per submission attempt;
  //  - reused only when the payload fingerprint is unchanged (i.e. a
  //    retry of the same submission after an uncertain network result);
  //  - regenerated as soon as any form field changes;
  //  - cleared on confirmed success (see the closing branch).
  const idemKeyRef = useRef<string | null>(null);
  const idemFingerprintRef = useRef<string | null>(null);

  // Route/mutation generation guard. Every submission captures the
  // full identity (org + item + warehouse + lot [+ tx]) and only
  // writes state back if the identity is still the current one.
  const generationRef = useRef(0);
  const routeIdRef = useRef('');
  routeIdRef.current = `${organizationId}::${item.id}`;

  // Reset on identity change. Any change to organizationId, itemId,
  // operation type, or reversal target invalidates in-flight state.
  useEffect(() => {
    generationRef.current += 1;
    setForm(initialForm(type));
    setFieldErrors({});
    setBusy(false);
    setError(null);
    setConfirming(false);
    idemKeyRef.current = null;
    idemFingerprintRef.current = null;
  }, [organizationId, item.id, type, reversalTx?.id]);

  // Reset on close (also handles user-cancelled flows).
  useEffect(() => {
    if (!open) {
      generationRef.current += 1;
      setForm(initialForm(type));
      setFieldErrors({});
      setBusy(false);
      setError(null);
      setConfirming(false);
      idemKeyRef.current = null;
      idemFingerprintRef.current = null;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // Sprint 5.4.1 — bump the generation ref on unmount so any
  // in-flight async completion (POST resolution, refresh callback)
  // observes `!isCurrent()` and skips its state write. Without
  // this, an unmount that happens between submit and resolve would
  // leave the completion free to call `onClose` / `toast` /
  // `onSuccess` / `onUnauthenticated` on a torn-down tree.
  useEffect(() => {
    return () => {
      generationRef.current += 1;
    };
  }, []);

  // Available lots depend on the currently-selected warehouse (or
  // the reversal transaction's warehouse). For a reversal we don't
  // show a lot picker at all.
  const activeWarehouseId = useMemo<string>(() => {
    if (form.type === 'reverse') return reversalTx?.warehouse_id ?? '';
    return 'warehouseId' in form ? form.warehouseId : '';
  }, [form, reversalTx?.warehouse_id]);

  const lotOptions = useMemo(
    () => lotsForWarehouse(lots, activeWarehouseId || null),
    [lots, activeWarehouseId],
  );
  const adjustmentDirection = form.type === 'adjust' ? form.direction : null;
  const sourceWarehouseOptions = useMemo(() => {
    switch (form.type) {
      case 'receive':
        return warehouseOptionsForStockOperation(warehouses, 'receive');
      case 'issue':
        return warehouseOptionsForStockOperation(warehouses, 'issue');
      case 'transfer':
        return warehouseOptionsForStockOperation(warehouses, 'transfer-source');
      case 'adjust':
        return warehouseOptionsForStockOperation(
          warehouses,
          adjustmentDirection === 'increase' ? 'adjust-increase' : 'adjust-decrease',
        );
      case 'reverse':
        return [];
    }
  }, [form.type, adjustmentDirection, warehouses]);
  const destOptions = useMemo(
    () => destinationOptions(warehouses, activeWarehouseId || ''),
    [warehouses, activeWarehouseId],
  );

  // Whenever the form payload changes, the current idempotency key
  // becomes stale. We regenerate lazily on submit so the key only
  // ever appears on the wire attached to the exact fingerprint it
  // was minted for.
  const updateForm = useCallback((patch: Partial<StockOperationForm>) => {
    setForm((prev) => {
      // TypeScript narrowing across a discriminated union with a
      // partial patch requires a cast — the underlying invariant
      // (patch must be shaped like `prev`) is preserved by the
      // callers, which only pass same-type patches.
      return { ...prev, ...patch } as StockOperationForm;
    });
    idemFingerprintRef.current = null;
    idemKeyRef.current = null;
    // Clear stale field errors as the user edits.
    setFieldErrors({});
    setError(null);
  }, []);

  const canSubmit = !busy;

  // ---- Submission ------------------------------------------------- //
  async function handleSubmit() {
    if (busy) return;
    const validation = validateForm(form);
    if (!validation.ok) {
      setFieldErrors(validation.fieldErrors);
      return;
    }
    if (!confirming) {
      // First press → show confirmation summary. Actual API call
      // happens on the confirm button (see below).
      setFieldErrors({});
      setError(null);
      setConfirming(true);
      return;
    }
    // Compose request.
    let path = '';
    let body: Record<string, unknown> | null = null;
    if (form.type === 'reverse') {
      if (!reversalTx?.warehouse_id) {
        setError('Reversal target warehouse is not available.');
        return;
      }
      path = `/v1/warehouses/${reversalTx.warehouse_id}/inventory:reverse`;
      body = {
        reverses_transaction_id: reversalTx.id,
        reason: form.reason.trim(),
      };
    } else {
      const built = buildRequest(form, {
        itemId: item.id,
        canonicalUnit: item.canonical_unit,
      });
      if (!built) {
        setError('Failed to build request payload.');
        return;
      }
      path = built.path;
      body = built.body;
    }

    // Idempotency-Key selection. Fingerprint the composed request;
    // reuse the previously-issued key only if the fingerprint hasn't
    // changed since the last submission attempt (i.e. this is a
    // retry of the identical submission after an uncertain network
    // result).
    const fp = payloadFingerprint({ path, body });
    if (idemFingerprintRef.current !== fp || !idemKeyRef.current) {
      idemKeyRef.current = makeIdempotencyKey();
      idemFingerprintRef.current = fp;
    }
    const idemKey = idemKeyRef.current;

    // Capture the identity that owns this mutation.
    const gen = ++generationRef.current;
    const capturedRouteId = routeIdRef.current;
    const isCurrent = () => generationRef.current === gen && routeIdRef.current === capturedRouteId;

    setBusy(true);
    setError(null);
    try {
      await apiFetch(path, {
        method: 'POST',
        headers: { 'Idempotency-Key': idemKey },
        body: JSON.stringify(body),
      });
      if (!isCurrent()) return;
      toast(`${operationLabel(form.type, reversalTx)} succeeded.`, 'success');
      // Confirmed success → clear the stored idempotency key so a
      // subsequent submission mints a fresh one.
      idemKeyRef.current = null;
      idemFingerprintRef.current = null;
      setBusy(false);
      onClose();
      // The parent refreshes affected data (availability, lots,
      // activity). A refresh failure is surfaced by the parent as
      // a non-fatal toast so the user knows the write itself
      // succeeded.
      try {
        await onSuccess();
      } catch {
        if (!isCurrent()) return;
        toast(
          'The operation succeeded but the display could not be fully refreshed. Try refreshing the page.',
          'error',
        );
      }
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        // Sprint 5.4.1 — guard onUnauthenticated behind isCurrent
        // so a stale POST completion from a torn-down / navigated-
        // away dialog cannot trigger a login redirect on the new
        // route. The parent's own auth guards will surface any
        // genuine 401 on the current page.
        if (!isCurrent()) return;
        onUnauthenticated();
        return;
      }
      if (!isCurrent()) return;
      if (err instanceof ApiError && err.status === 403) {
        setError("You don't have permission to perform this operation here.");
        setBusy(false);
        return;
      }
      if (err instanceof ApiError && err.status === 404) {
        // Tenancy-sensitive: do NOT reveal whether the target
        // exists outside the caller's scope. A generic message
        // is intentionally used.
        setError('The target of this operation is not available.');
        setBusy(false);
        return;
      }
      if (err instanceof ApiError && err.status === 422) {
        // Field-level 422 payload: `{detail: [{loc:[...], msg}]}`
        const payload = err.payload as { detail?: unknown };
        const fieldMap: Partial<Record<StockOperationField, string>> = {};
        let hasUnmappedError = false;
        if (Array.isArray(payload.detail)) {
          for (const entry of payload.detail as Array<{ loc?: unknown; msg?: string }>) {
            const loc = Array.isArray(entry.loc) ? entry.loc[entry.loc.length - 1] : null;
            const mappedField =
              typeof loc === 'string'
                ? API_VALIDATION_FIELD_MAP[loc as ApiValidationField]
                : undefined;
            if (mappedField && VISIBLE_FIELDS[form.type].has(mappedField) && entry.msg) {
              fieldMap[mappedField] = entry.msg;
            } else {
              hasUnmappedError = true;
            }
          }
        }
        if (Object.keys(fieldMap).length > 0) {
          setFieldErrors(fieldMap);
          setError(hasUnmappedError ? GENERIC_VALIDATION_ERROR : null);
          // Return to the form view so the user can correct the
          // offending fields — the confirmation summary hides them.
          setConfirming(false);
          setBusy(false);
          return;
        }
        if (hasUnmappedError) {
          setError(GENERIC_VALIDATION_ERROR);
          setConfirming(false);
          setBusy(false);
          return;
        }
      }
      setError(friendlyError(err));
      setBusy(false);
    }
  }

  // ESC-to-close (only while not mid-request).
  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !busy) onClose();
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [open, busy, onClose]);

  // ---- Sprint 5.4.1 focus management -------------------------------- //
  // We need to (a) autofocus a sensible first control when the
  // dialog opens, (b) trap Tab / Shift+Tab within the dialog while
  // it is open, and (c) restore focus to the element that owned it
  // right before the dialog opened.
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const previouslyFocusedRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!open) return;
    // Remember the trigger so we can restore focus on close.
    previouslyFocusedRef.current =
      typeof document !== 'undefined' && document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    // Autofocus the first tabbable control inside the dialog.
    const node = dialogRef.current;
    if (node) {
      const first = getTabbableElements(node)[0];
      // rAF because focus can lose to layout on the same tick.
      const raf = window.requestAnimationFrame(() => {
        first?.focus();
      });
      return () => {
        window.cancelAnimationFrame(raf);
        // Restore focus to whatever owned it before we opened.
        const previous = previouslyFocusedRef.current;
        if (previous && typeof previous.focus === 'function') previous.focus();
      };
    }
    return () => {
      const previous = previouslyFocusedRef.current;
      if (previous && typeof previous.focus === 'function') previous.focus();
    };
    // Rerun when the operation-type changes (the field surface is
    // rebuilt, so we re-focus the first control) or on open toggle.
  }, [open, type]);

  useEffect(() => {
    if (!open) return;
    const node = dialogRef.current;
    if (!node) return;
    const trap = (e: KeyboardEvent) => {
      if (e.key !== 'Tab') return;
      const focusables = getTabbableElements(node);
      if (focusables.length === 0) {
        e.preventDefault();
        return;
      }
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      const active = document.activeElement as HTMLElement | null;
      if (e.shiftKey) {
        if (active === first || !node.contains(active)) {
          e.preventDefault();
          last.focus();
        }
      } else {
        if (active === last || !node.contains(active)) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    node.addEventListener('keydown', trap);
    return () => node.removeEventListener('keydown', trap);
  }, [open, confirming, type]);

  if (!open) return null;

  const testIdRoot = `stock-op-${type}`;
  const label = operationLabel(type, reversalTx);

  return (
    <div
      ref={dialogRef}
      role="dialog"
      aria-modal="true"
      aria-labelledby={`${testIdRoot}-title`}
      aria-describedby={`${testIdRoot}-desc`}
      className="fixed inset-0 z-40 flex items-center justify-center bg-black/40 p-4"
      data-testid={testIdRoot}
    >
      <div className="w-full max-w-lg rounded-2xl border border-border bg-card p-5 shadow-lg">
        <h2 id={`${testIdRoot}-title`} className="font-display text-lg">
          {label}
        </h2>
        <p id={`${testIdRoot}-desc`} className="mt-1 text-xs text-muted-foreground">
          Item <span className="font-mono">{item.code}</span> · unit{' '}
          <span className="font-mono">{item.canonical_unit}</span>
        </p>

        {error && (
          <div
            data-testid={`${testIdRoot}-error`}
            role="alert"
            className="mt-3 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive"
          >
            {error}
          </div>
        )}

        {!confirming ? (
          <form
            className="mt-4 space-y-3"
            onSubmit={(e) => {
              e.preventDefault();
              void handleSubmit();
            }}
          >
            {form.type === 'receive' && (
              <ReceiveFields
                form={form}
                warehouses={sourceWarehouseOptions}
                onChange={updateForm}
                errors={fieldErrors}
                testIdRoot={testIdRoot}
              />
            )}
            {form.type === 'issue' && (
              <IssueFields
                form={form}
                warehouses={sourceWarehouseOptions}
                lotsInWarehouse={lotOptions}
                onChange={updateForm}
                errors={fieldErrors}
                testIdRoot={testIdRoot}
              />
            )}
            {form.type === 'transfer' && (
              <TransferFields
                form={form}
                warehouses={sourceWarehouseOptions}
                lotsInWarehouse={lotOptions}
                destinationChoices={destOptions}
                onChange={updateForm}
                errors={fieldErrors}
                testIdRoot={testIdRoot}
              />
            )}
            {form.type === 'adjust' && (
              <AdjustFields
                form={form}
                warehouses={sourceWarehouseOptions}
                lotsInWarehouse={lotOptions}
                onChange={updateForm}
                errors={fieldErrors}
                testIdRoot={testIdRoot}
              />
            )}
            {form.type === 'reverse' && reversalTx && (
              <ReverseFields
                form={form}
                tx={reversalTx}
                onChange={updateForm}
                errors={fieldErrors}
                testIdRoot={testIdRoot}
              />
            )}
            <DialogFooter
              testIdRoot={testIdRoot}
              busy={busy}
              canSubmit={canSubmit}
              onCancel={onClose}
              submitLabel="Review"
            />
          </form>
        ) : (
          <ConfirmationSummary
            type={type}
            form={form}
            item={item}
            reversalTx={reversalTx}
            warehouses={warehouses}
            testIdRoot={testIdRoot}
            busy={busy}
            onBack={() => setConfirming(false)}
            onConfirm={() => void handleSubmit()}
          />
        )}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ //
// Per-operation field blocks                                          //
// ------------------------------------------------------------------ //
function FieldError({ id, msg }: { id: string; msg?: string }) {
  if (!msg) return null;
  return (
    <p id={id} className="mt-1 text-xs text-destructive" data-testid={id} role="alert">
      {msg}
    </p>
  );
}

function ReceiveFields({
  form,
  warehouses,
  onChange,
  errors,
  testIdRoot,
}: {
  form: Extract<StockOperationForm, { type: 'receive' }>;
  warehouses: readonly ItemWarehouse[];
  onChange: (patch: Partial<StockOperationForm>) => void;
  errors: Partial<Record<string, string>>;
  testIdRoot: string;
}) {
  return (
    <>
      <WarehouseField
        value={form.warehouseId}
        warehouses={warehouses}
        error={errors.warehouseId}
        onChange={(warehouseId) => onChange({ type: 'receive', warehouseId })}
        testId={`${testIdRoot}-warehouse`}
      />
      <label className="block text-sm">
        Lot code
        <input
          type="text"
          data-testid={`${testIdRoot}-lot-code`}
          value={form.lotCode}
          onChange={(e) => onChange({ type: 'receive', lotCode: e.target.value })}
          className="mt-1 block w-full rounded-md border border-border bg-background px-2 py-1"
          aria-invalid={!!errors.lotCode}
          aria-describedby={errors.lotCode ? `${testIdRoot}-lot-code-error` : undefined}
        />
      </label>
      <FieldError id={`${testIdRoot}-lot-code-error`} msg={errors.lotCode} />
      <QuantityField
        value={form.quantity}
        error={errors.quantity}
        onChange={(quantity) => onChange({ type: 'receive', quantity })}
        testId={`${testIdRoot}-quantity`}
      />
      <label className="block text-sm">
        Expiry date (optional)
        <input
          type="date"
          data-testid={`${testIdRoot}-expiry`}
          value={form.expiryDate}
          onChange={(e) => onChange({ type: 'receive', expiryDate: e.target.value })}
          className="mt-1 block w-full rounded-md border border-border bg-background px-2 py-1"
          aria-invalid={!!errors.expiryDate}
          aria-describedby={errors.expiryDate ? `${testIdRoot}-expiry-error` : undefined}
        />
      </label>
      <FieldError id={`${testIdRoot}-expiry-error`} msg={errors.expiryDate} />
      <ReasonField
        value={form.reason}
        error={errors.reason}
        onChange={(reason) => onChange({ type: 'receive', reason })}
        testId={`${testIdRoot}-reason`}
        required={false}
      />
    </>
  );
}

function IssueFields({
  form,
  warehouses,
  lotsInWarehouse,
  onChange,
  errors,
  testIdRoot,
}: {
  form: Extract<StockOperationForm, { type: 'issue' }>;
  warehouses: readonly ItemWarehouse[];
  lotsInWarehouse: readonly ItemLot[];
  onChange: (patch: Partial<StockOperationForm>) => void;
  errors: Partial<Record<string, string>>;
  testIdRoot: string;
}) {
  return (
    <>
      <WarehouseField
        value={form.warehouseId}
        warehouses={warehouses}
        error={errors.warehouseId}
        onChange={(warehouseId) => onChange({ type: 'issue', warehouseId, lotId: '' })}
        testId={`${testIdRoot}-warehouse`}
      />
      <LotField
        value={form.lotId}
        lots={lotsInWarehouse}
        error={errors.lotId}
        onChange={(lotId) => onChange({ type: 'issue', lotId })}
        testId={`${testIdRoot}-lot`}
      />
      <QuantityField
        value={form.quantity}
        error={errors.quantity}
        onChange={(quantity) => onChange({ type: 'issue', quantity })}
        testId={`${testIdRoot}-quantity`}
      />
      <ReasonField
        value={form.reason}
        error={errors.reason}
        onChange={(reason) => onChange({ type: 'issue', reason })}
        testId={`${testIdRoot}-reason`}
        required={false}
      />
    </>
  );
}

function TransferFields({
  form,
  warehouses,
  lotsInWarehouse,
  destinationChoices,
  onChange,
  errors,
  testIdRoot,
}: {
  form: Extract<StockOperationForm, { type: 'transfer' }>;
  warehouses: readonly ItemWarehouse[];
  lotsInWarehouse: readonly ItemLot[];
  destinationChoices: readonly ItemWarehouse[];
  onChange: (patch: Partial<StockOperationForm>) => void;
  errors: Partial<Record<string, string>>;
  testIdRoot: string;
}) {
  return (
    <>
      <WarehouseField
        label="Source warehouse"
        value={form.warehouseId}
        warehouses={warehouses}
        error={errors.warehouseId}
        onChange={(warehouseId) =>
          onChange({ type: 'transfer', warehouseId, lotId: '', destinationWarehouseId: '' })
        }
        testId={`${testIdRoot}-warehouse`}
      />
      <LotField
        value={form.lotId}
        lots={lotsInWarehouse}
        error={errors.lotId}
        onChange={(lotId) => onChange({ type: 'transfer', lotId })}
        testId={`${testIdRoot}-lot`}
      />
      <label className="block text-sm">
        Destination warehouse
        <select
          data-testid={`${testIdRoot}-destination`}
          value={form.destinationWarehouseId}
          onChange={(e) => onChange({ type: 'transfer', destinationWarehouseId: e.target.value })}
          disabled={!form.warehouseId}
          className="mt-1 block w-full rounded-md border border-border bg-background px-2 py-1 disabled:opacity-60"
          aria-invalid={!!errors.destinationWarehouseId}
          aria-describedby={
            errors.destinationWarehouseId ? `${testIdRoot}-destination-error` : undefined
          }
        >
          <option value="">Select…</option>
          {destinationChoices.map((w) => (
            <option key={w.id} value={w.id}>
              {w.name}
            </option>
          ))}
        </select>
      </label>
      <FieldError id={`${testIdRoot}-destination-error`} msg={errors.destinationWarehouseId} />
      <QuantityField
        value={form.quantity}
        error={errors.quantity}
        onChange={(quantity) => onChange({ type: 'transfer', quantity })}
        testId={`${testIdRoot}-quantity`}
      />
      <ReasonField
        value={form.reason}
        error={errors.reason}
        onChange={(reason) => onChange({ type: 'transfer', reason })}
        testId={`${testIdRoot}-reason`}
        required={false}
      />
    </>
  );
}

function AdjustFields({
  form,
  warehouses,
  lotsInWarehouse,
  onChange,
  errors,
  testIdRoot,
}: {
  form: Extract<StockOperationForm, { type: 'adjust' }>;
  warehouses: readonly ItemWarehouse[];
  lotsInWarehouse: readonly ItemLot[];
  onChange: (patch: Partial<StockOperationForm>) => void;
  errors: Partial<Record<string, string>>;
  testIdRoot: string;
}) {
  return (
    <>
      <WarehouseField
        value={form.warehouseId}
        warehouses={warehouses}
        error={errors.warehouseId}
        onChange={(warehouseId) => onChange({ type: 'adjust', warehouseId, lotId: '' })}
        testId={`${testIdRoot}-warehouse`}
      />
      <LotField
        value={form.lotId}
        lots={lotsInWarehouse}
        error={errors.lotId}
        onChange={(lotId) => onChange({ type: 'adjust', lotId })}
        testId={`${testIdRoot}-lot`}
      />
      <fieldset
        className="text-sm"
        aria-invalid={!!errors.direction}
        aria-describedby={errors.direction ? `${testIdRoot}-direction-error` : undefined}
      >
        <legend className="mb-1">Direction</legend>
        <label className="mr-3 inline-flex items-center gap-1">
          <input
            type="radio"
            name={`${testIdRoot}-direction`}
            data-testid={`${testIdRoot}-direction-increase`}
            checked={form.direction === 'increase'}
            onChange={() => onChange({ type: 'adjust', direction: 'increase' })}
          />
          Increase
        </label>
        <label className="inline-flex items-center gap-1">
          <input
            type="radio"
            name={`${testIdRoot}-direction`}
            data-testid={`${testIdRoot}-direction-decrease`}
            checked={form.direction === 'decrease'}
            onChange={() => {
              const warehouseRemainsEligible = warehouseOptionsForStockOperation(
                warehouses,
                'adjust-decrease',
              ).some((warehouse) => warehouse.id === form.warehouseId);
              onChange({
                type: 'adjust',
                direction: 'decrease',
                ...(warehouseRemainsEligible ? {} : { warehouseId: '', lotId: '' }),
              });
            }}
          />
          Decrease
        </label>
      </fieldset>
      <FieldError id={`${testIdRoot}-direction-error`} msg={errors.direction} />
      <QuantityField
        value={form.quantity}
        error={errors.quantity}
        onChange={(quantity) => onChange({ type: 'adjust', quantity })}
        testId={`${testIdRoot}-quantity`}
      />
      <ReasonField
        value={form.reason}
        error={errors.reason}
        onChange={(reason) => onChange({ type: 'adjust', reason })}
        testId={`${testIdRoot}-reason`}
        required
      />
    </>
  );
}

function ReverseFields({
  form,
  tx,
  onChange,
  errors,
  testIdRoot,
}: {
  form: Extract<StockOperationForm, { type: 'reverse' }>;
  tx: ItemLedgerTx;
  onChange: (patch: Partial<StockOperationForm>) => void;
  errors: Partial<Record<string, string>>;
  testIdRoot: string;
}) {
  return (
    <>
      <div
        data-testid={`${testIdRoot}-original`}
        className="rounded-md border border-border bg-secondary/30 p-3 text-sm"
      >
        <div className="mb-1 text-xs uppercase tracking-widest text-muted-foreground">
          Original transaction
        </div>
        <div>
          <span className="font-mono">{tx.transaction_type}</span> ·{' '}
          <span className="font-mono">
            {tx.quantity} {tx.unit}
          </span>{' '}
          · <span className="font-mono text-xs">{tx.performed_at}</span>
        </div>
        {tx.reason && <div className="mt-1 text-xs text-muted-foreground">{tx.reason}</div>}
      </div>
      <p className="text-xs text-muted-foreground">
        The original ledger entry is immutable and remains in the record. The reversal will post an
        inverse transaction that offsets the original balance change.
      </p>
      <ReasonField
        value={form.reason}
        error={errors.reason}
        onChange={(reason) => onChange({ type: 'reverse', reason })}
        testId={`${testIdRoot}-reason`}
        required
      />
    </>
  );
}

// ------------------------------------------------------------------ //
// Shared field components                                            //
// ------------------------------------------------------------------ //
function WarehouseField({
  label = 'Warehouse',
  value,
  warehouses,
  error,
  onChange,
  testId,
}: {
  label?: string;
  value: string;
  warehouses: readonly ItemWarehouse[];
  error?: string;
  onChange: (v: string) => void;
  testId: string;
}) {
  return (
    <>
      <label className="block text-sm">
        {label}
        <select
          data-testid={testId}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className="mt-1 block w-full rounded-md border border-border bg-background px-2 py-1"
          aria-invalid={!!error}
          aria-describedby={error ? `${testId}-error` : undefined}
        >
          <option value="">Select…</option>
          {warehouses.map((w) => (
            <option key={w.id} value={w.id}>
              {w.name}
            </option>
          ))}
        </select>
      </label>
      <FieldError id={`${testId}-error`} msg={error} />
    </>
  );
}

function LotField({
  value,
  lots,
  error,
  onChange,
  testId,
}: {
  value: string;
  lots: readonly ItemLot[];
  error?: string;
  onChange: (v: string) => void;
  testId: string;
}) {
  return (
    <>
      <label className="block text-sm">
        Lot
        <select
          data-testid={testId}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className="mt-1 block w-full rounded-md border border-border bg-background px-2 py-1"
          aria-invalid={!!error}
          aria-describedby={error ? `${testId}-error` : undefined}
        >
          <option value="">Select…</option>
          {lots.map((l) => (
            <option key={l.id} value={l.id}>
              {l.lot_code} — bal {l.balance} {l.balance_unit}
            </option>
          ))}
        </select>
      </label>
      <FieldError id={`${testId}-error`} msg={error} />
    </>
  );
}

function QuantityField({
  value,
  error,
  onChange,
  testId,
}: {
  value: string;
  error?: string;
  onChange: (v: string) => void;
  testId: string;
}) {
  return (
    <>
      <label className="block text-sm">
        Quantity
        <input
          type="number"
          inputMode="decimal"
          step="any"
          min="0"
          data-testid={testId}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className="mt-1 block w-full rounded-md border border-border bg-background px-2 py-1"
          aria-invalid={!!error}
          aria-describedby={error ? `${testId}-error` : undefined}
        />
      </label>
      <FieldError id={`${testId}-error`} msg={error} />
    </>
  );
}

function ReasonField({
  value,
  error,
  onChange,
  testId,
  required,
}: {
  value: string;
  error?: string;
  onChange: (v: string) => void;
  testId: string;
  required: boolean;
}) {
  return (
    <>
      <label className="block text-sm">
        Reason {required ? '' : '(optional)'}
        <textarea
          rows={2}
          maxLength={500}
          data-testid={testId}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className="mt-1 block w-full rounded-md border border-border bg-background px-2 py-1"
          aria-invalid={!!error}
          aria-describedby={error ? `${testId}-error` : undefined}
        />
      </label>
      <FieldError id={`${testId}-error`} msg={error} />
    </>
  );
}

// ------------------------------------------------------------------ //
// Confirmation summary + footer                                      //
// ------------------------------------------------------------------ //
function ConfirmationSummary({
  type,
  form,
  item,
  reversalTx,
  warehouses,
  testIdRoot,
  busy,
  onBack,
  onConfirm,
}: {
  type: StockOperationType;
  form: StockOperationForm;
  item: InventoryItem;
  reversalTx?: ItemLedgerTx & { warehouse_id?: string };
  warehouses: readonly ItemWarehouse[];
  testIdRoot: string;
  busy: boolean;
  onBack: () => void;
  onConfirm: () => void;
}) {
  const warehouseName = (id: string) => warehouses.find((w) => w.id === id)?.name ?? id ?? '—';
  const rows: Array<[string, string]> = [];
  rows.push(['Item', item.code]);
  rows.push(['Unit', item.canonical_unit]);
  if (form.type === 'receive') {
    rows.push(['Warehouse', warehouseName(form.warehouseId)]);
    rows.push(['Lot code', form.lotCode]);
    rows.push(['Quantity', form.quantity]);
    if (form.expiryDate) rows.push(['Expiry', form.expiryDate]);
    if (form.reason.trim()) rows.push(['Reason', form.reason.trim()]);
  } else if (form.type === 'issue') {
    rows.push(['Warehouse', warehouseName(form.warehouseId)]);
    rows.push(['Quantity', form.quantity]);
    if (form.reason.trim()) rows.push(['Reason', form.reason.trim()]);
  } else if (form.type === 'transfer') {
    rows.push(['From', warehouseName(form.warehouseId)]);
    rows.push(['To', warehouseName(form.destinationWarehouseId)]);
    rows.push(['Quantity', form.quantity]);
    if (form.reason.trim()) rows.push(['Reason', form.reason.trim()]);
  } else if (form.type === 'adjust') {
    rows.push(['Warehouse', warehouseName(form.warehouseId)]);
    rows.push(['Direction', form.direction]);
    rows.push(['Quantity', form.quantity]);
    rows.push(['Reason', form.reason.trim()]);
  } else if (form.type === 'reverse' && reversalTx) {
    rows.push(['Reverses', reversalTx.transaction_type]);
    rows.push(['Original qty', `${reversalTx.quantity} ${reversalTx.unit}`]);
    rows.push(['Reason', form.reason.trim()]);
  }
  return (
    <div className="mt-4">
      <p data-testid={`${testIdRoot}-confirm-heading`} className="text-sm font-medium">
        Please confirm
      </p>
      <p className="mt-1 text-xs text-muted-foreground">
        {type === 'reverse'
          ? reversalTx?.transaction_type === 'transfer_out' ||
            reversalTx?.transaction_type === 'transfer_in'
            ? 'This atomically reverses both sides of the transfer. The original ledger entries stay intact.'
            : 'This posts an inverse transaction. The original ledger entry stays intact.'
          : 'This will be recorded in the ledger and cannot be edited afterward — only reversed.'}
      </p>
      <dl
        data-testid={`${testIdRoot}-summary`}
        className="mt-3 grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm"
      >
        {rows.map(([k, v]) => (
          <div key={k} className="contents">
            <dt className="text-muted-foreground">{k}</dt>
            <dd className="font-mono">{v}</dd>
          </div>
        ))}
      </dl>
      <div className="mt-5 flex justify-end gap-2">
        <button
          type="button"
          onClick={onBack}
          disabled={busy}
          data-testid={`${testIdRoot}-back`}
          className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-secondary disabled:opacity-60"
        >
          Back
        </button>
        <button
          type="button"
          onClick={onConfirm}
          disabled={busy}
          data-testid={`${testIdRoot}-confirm`}
          className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-60"
        >
          {busy ? 'Working…' : 'Confirm'}
        </button>
      </div>
    </div>
  );
}

function DialogFooter({
  testIdRoot,
  busy,
  canSubmit,
  onCancel,
  submitLabel,
}: {
  testIdRoot: string;
  busy: boolean;
  canSubmit: boolean;
  onCancel: () => void;
  submitLabel: string;
}) {
  return (
    <div className="mt-5 flex justify-end gap-2">
      <button
        type="button"
        onClick={onCancel}
        disabled={busy}
        data-testid={`${testIdRoot}-cancel`}
        className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-secondary disabled:opacity-60"
      >
        Cancel
      </button>
      <button
        type="submit"
        disabled={!canSubmit}
        data-testid={`${testIdRoot}-submit`}
        className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground disabled:opacity-60"
      >
        {submitLabel}
      </button>
    </div>
  );
}
