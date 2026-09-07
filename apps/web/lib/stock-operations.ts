/**
 * Sprint 5.4 — Stock operations shared types + helpers.
 *
 * A single framework backs every inventory ledger operation
 * (receive / issue / transfer / adjust / reverse). Only the
 * operation-specific fields differ; everything else — validation,
 * idempotency, generation guards, refresh — is shared.
 *
 * Every value in this module maps 1:1 to a real backend field on
 * the Sprint 4 inventory API. Where the API rejects a shape (e.g.
 * quantity ≤ 0) the client rejects it too — but only as a UX
 * shortcut: backend validation remains authoritative.
 */

import type { ItemLedgerTx, ItemLot, ItemWarehouse, StockUnit } from '@/lib/inventory-items';

// ------------------------------------------------------------------ //
// Operation identity                                                 //
// ------------------------------------------------------------------ //
export type StockOperationType = 'receive' | 'issue' | 'transfer' | 'adjust' | 'reverse';

export const STOCK_OPERATION_LABELS: Record<StockOperationType, string> = {
  receive: 'Receive stock',
  issue: 'Issue stock',
  transfer: 'Transfer stock',
  adjust: 'Adjust stock',
  reverse: 'Reverse transaction',
};

// The backend enum values returned in `transaction_type`. Only
// these five are eligible reversal candidates (transfer_in gets
// reversed via its paired transfer_out row on the source side).
export const REVERSIBLE_TX_TYPES = new Set<string>([
  'receipt',
  'issue',
  'consumption',
  'transfer_out',
  'adjustment_increase',
  'adjustment_decrease',
]);

// ------------------------------------------------------------------ //
// Form state per operation                                           //
// ------------------------------------------------------------------ //
export interface ReceiveForm {
  warehouseId: string;
  lotCode: string;
  quantity: string;
  expiryDate: string;
  reason: string;
}
export interface IssueForm {
  warehouseId: string;
  lotId: string;
  quantity: string;
  reason: string;
}
export interface TransferForm {
  warehouseId: string;
  lotId: string;
  destinationWarehouseId: string;
  quantity: string;
  reason: string;
}
export interface AdjustForm {
  warehouseId: string;
  lotId: string;
  direction: 'increase' | 'decrease';
  quantity: string;
  reason: string;
}
export interface ReverseForm {
  reason: string;
}

export type StockOperationForm =
  | ({ type: 'receive' } & ReceiveForm)
  | ({ type: 'issue' } & IssueForm)
  | ({ type: 'transfer' } & TransferForm)
  | ({ type: 'adjust' } & AdjustForm)
  | ({ type: 'reverse' } & ReverseForm);

export function initialForm(type: StockOperationType): StockOperationForm {
  switch (type) {
    case 'receive':
      return {
        type,
        warehouseId: '',
        lotCode: '',
        quantity: '',
        expiryDate: '',
        reason: '',
      };
    case 'issue':
      return { type, warehouseId: '', lotId: '', quantity: '', reason: '' };
    case 'transfer':
      return {
        type,
        warehouseId: '',
        lotId: '',
        destinationWarehouseId: '',
        quantity: '',
        reason: '',
      };
    case 'adjust':
      return {
        type,
        warehouseId: '',
        lotId: '',
        direction: 'increase',
        quantity: '',
        reason: '',
      };
    case 'reverse':
      return { type, reason: '' };
  }
}

// ------------------------------------------------------------------ //
// Validation                                                         //
// ------------------------------------------------------------------ //
export interface ValidationResult {
  ok: boolean;
  fieldErrors: Partial<Record<string, string>>;
}

function parseQuantity(raw: string): number | null {
  const trimmed = raw.trim();
  if (!trimmed) return null;
  const n = Number(trimmed);
  return Number.isFinite(n) ? n : NaN;
}

export function validateForm(form: StockOperationForm): ValidationResult {
  const fieldErrors: Partial<Record<string, string>> = {};
  const requireQuantity = () => {
    if (form.type === 'reverse') return;
    const q = parseQuantity(form.quantity);
    if (q === null) fieldErrors.quantity = 'Quantity is required.';
    else if (Number.isNaN(q) || q <= 0) fieldErrors.quantity = 'Quantity must be greater than 0.';
  };
  switch (form.type) {
    case 'receive':
      if (!form.warehouseId) fieldErrors.warehouseId = 'Warehouse is required.';
      if (!form.lotCode.trim()) fieldErrors.lotCode = 'Lot code is required.';
      requireQuantity();
      break;
    case 'issue':
      if (!form.warehouseId) fieldErrors.warehouseId = 'Warehouse is required.';
      if (!form.lotId) fieldErrors.lotId = 'Lot is required.';
      requireQuantity();
      break;
    case 'transfer':
      if (!form.warehouseId) fieldErrors.warehouseId = 'Source warehouse is required.';
      if (!form.lotId) fieldErrors.lotId = 'Lot is required.';
      if (!form.destinationWarehouseId)
        fieldErrors.destinationWarehouseId = 'Destination warehouse is required.';
      else if (form.destinationWarehouseId === form.warehouseId)
        fieldErrors.destinationWarehouseId = 'Destination must differ from source.';
      requireQuantity();
      break;
    case 'adjust':
      if (!form.warehouseId) fieldErrors.warehouseId = 'Warehouse is required.';
      if (!form.lotId) fieldErrors.lotId = 'Lot is required.';
      if (!form.reason.trim()) fieldErrors.reason = 'Reason is required for adjustments.';
      requireQuantity();
      break;
    case 'reverse':
      if (!form.reason.trim()) fieldErrors.reason = 'Reason is required for reversals.';
      break;
  }
  return { ok: Object.keys(fieldErrors).length === 0, fieldErrors };
}

// ------------------------------------------------------------------ //
// Payload builders                                                   //
// ------------------------------------------------------------------ //
export interface BuildPayloadContext {
  itemId: string;
  canonicalUnit: StockUnit;
  reversalTransactionId?: string;
}

export interface BuiltRequest {
  path: string;
  body: Record<string, unknown>;
}

/**
 * Build the fetch path and JSON body for the given form. The path
 * always targets `/v1/warehouses/{sourceWarehouseId}/inventory:{op}`.
 * For reversals the source warehouse is the one that owns the
 * transaction being reversed — the caller supplies it explicitly.
 */
export function buildRequest(
  form: StockOperationForm,
  ctx: BuildPayloadContext,
): BuiltRequest | null {
  switch (form.type) {
    case 'receive': {
      const body: Record<string, unknown> = {
        item_id: ctx.itemId,
        lot_code: form.lotCode.trim(),
        quantity: form.quantity.trim(),
        unit: ctx.canonicalUnit,
      };
      if (form.expiryDate) body.expiry_date = form.expiryDate;
      if (form.reason.trim()) body.reason = form.reason.trim();
      return { path: `/v1/warehouses/${form.warehouseId}/inventory:receive`, body };
    }
    case 'issue': {
      const body: Record<string, unknown> = {
        lot_id: form.lotId,
        quantity: form.quantity.trim(),
        unit: ctx.canonicalUnit,
      };
      if (form.reason.trim()) body.reason = form.reason.trim();
      return { path: `/v1/warehouses/${form.warehouseId}/inventory:issue`, body };
    }
    case 'transfer': {
      const body: Record<string, unknown> = {
        lot_id: form.lotId,
        destination_warehouse_id: form.destinationWarehouseId,
        quantity: form.quantity.trim(),
        unit: ctx.canonicalUnit,
      };
      if (form.reason.trim()) body.reason = form.reason.trim();
      return { path: `/v1/warehouses/${form.warehouseId}/inventory:transfer`, body };
    }
    case 'adjust': {
      const body: Record<string, unknown> = {
        lot_id: form.lotId,
        quantity: form.quantity.trim(),
        unit: ctx.canonicalUnit,
        direction: form.direction,
        reason: form.reason.trim(),
      };
      return { path: `/v1/warehouses/${form.warehouseId}/inventory:adjust`, body };
    }
    case 'reverse': {
      if (!ctx.reversalTransactionId) return null;
      const body: Record<string, unknown> = {
        reverses_transaction_id: ctx.reversalTransactionId,
        reason: form.reason.trim(),
      };
      // Path warehouse must be filled by caller via a wrapper —
      // see buildReversalRequest below.
      return { path: '', body };
    }
  }
}

export function buildReversalRequest(
  form: Extract<StockOperationForm, { type: 'reverse' }>,
  ctx: BuildPayloadContext & { warehouseId: string },
): BuiltRequest {
  return {
    path: `/v1/warehouses/${ctx.warehouseId}/inventory:reverse`,
    body: {
      reverses_transaction_id: ctx.reversalTransactionId,
      reason: form.reason.trim(),
    },
  };
}

// ------------------------------------------------------------------ //
// Payload fingerprint (idempotency key management)                   //
// ------------------------------------------------------------------ //
/**
 * A stable string fingerprint of the request body. Two submissions
 * with the same fingerprint may safely reuse the same
 * `Idempotency-Key`. As soon as the fingerprint changes we must
 * generate a fresh key so a new logical operation is not merged
 * with a previous one on the server.
 */
export function payloadFingerprint(built: BuiltRequest): string {
  return `${built.path}::${JSON.stringify(built.body)}`;
}

/**
 * `crypto.randomUUID()` works everywhere we ship (modern browsers +
 * Node ≥ 20) but degrade gracefully so unit tests without the API
 * still get a reasonably unique key.
 */
export function makeIdempotencyKey(): string {
  const g: unknown = globalThis as unknown;
  const cryptoObj =
    (g as { crypto?: { randomUUID?: () => string } })?.crypto ??
    ({} as { randomUUID?: () => string });
  if (typeof cryptoObj.randomUUID === 'function') return cryptoObj.randomUUID();
  const bytes = new Uint8Array(16);
  for (let i = 0; i < bytes.length; i += 1) bytes[i] = Math.floor(Math.random() * 256);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
}

// ------------------------------------------------------------------ //
// Lot / warehouse filtering helpers                                  //
// ------------------------------------------------------------------ //
export type StockWarehouseOperation =
  | 'receive'
  | 'issue'
  | 'transfer-source'
  | 'transfer-destination'
  | 'adjust-increase'
  | 'adjust-decrease';

/**
 * Whether a warehouse can participate in a new stock mutation.
 * Maintenance warehouses may receive stock, including transfers and
 * adjustment increases, but cannot be a source of stock leaving.
 */
export function isWarehouseEligibleForStockOperation(
  warehouse: Pick<ItemWarehouse, 'status'>,
  operation: StockWarehouseOperation,
): boolean {
  if (warehouse.status === 'active') return true;
  if (warehouse.status !== 'maintenance') return false;
  return (
    operation === 'receive' ||
    operation === 'transfer-destination' ||
    operation === 'adjust-increase'
  );
}

export function warehouseOptionsForStockOperation(
  warehouses: readonly ItemWarehouse[],
  operation: StockWarehouseOperation,
): ItemWarehouse[] {
  return warehouses.filter((warehouse) =>
    isWarehouseEligibleForStockOperation(warehouse, operation),
  );
}

/**
 * Lots that belong to a given warehouse. `readonly` inputs so
 * the caller keeps ownership of the source arrays.
 */
export function lotsForWarehouse(lots: readonly ItemLot[], warehouseId: string | null): ItemLot[] {
  if (!warehouseId) return [];
  return lots.filter((l) => l.warehouse_id === warehouseId);
}

/**
 * Destination warehouses for a transfer: same-organization set
 * minus the source. Access restrictions on the destination are
 * enforced by the backend (403 is surfaced by the caller); we
 * simply exclude the source from the picker.
 */
export function destinationOptions(
  warehouses: readonly ItemWarehouse[],
  sourceWarehouseId: string,
): ItemWarehouse[] {
  return warehouseOptionsForStockOperation(warehouses, 'transfer-destination').filter(
    (warehouse) => warehouse.id !== sourceWarehouseId,
  );
}

// ------------------------------------------------------------------ //
// Reversal eligibility                                               //
// ------------------------------------------------------------------ //
export function isReversibleTransaction(tx: Pick<ItemLedgerTx, 'transaction_type'>): boolean {
  return REVERSIBLE_TX_TYPES.has(tx.transaction_type);
}
