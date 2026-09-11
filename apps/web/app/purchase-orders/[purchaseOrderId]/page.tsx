'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { useParams, usePathname, useRouter } from 'next/navigation';
import { ApiError, apiFetch } from '@/lib/api';
import { EmptyState, ErrorBanner, ForbiddenBanner } from '@/components/ape-ui';
import { SkeletonRows, toast } from '@/components/ui-polish';
import { PurchaseOrderDetail } from '@/components/purchase-orders/PurchaseOrderDetail';
import {
  PurchaseOrderLifecycleActions,
  type PurchaseOrderLifecycleActionResult,
  type PurchaseOrderLifecycleOperation,
} from '@/components/purchase-orders/PurchaseOrderLifecycleActions';
import { PurchaseOrderTransitionHistory } from '@/components/purchase-orders/PurchaseOrderTransitionHistory';
import {
  approvePurchaseOrder,
  cancelPurchaseOrder,
  getPurchaseOrder,
  listPurchaseOrderTransitions,
  rejectPurchaseOrder,
  revisePurchaseOrder,
  submitPurchaseOrder,
  type PurchaseOrder,
  type PurchaseOrderTransition,
  type LifecycleResponse,
  withdrawPurchaseOrder,
} from '@/lib/purchase-orders';
import type { CurrentUser } from '@/lib/types';
import { hasScopedPermission } from '@/lib/permissions';
import {
  listPurchaseReceipts,
  listReceiptWarehouses,
  type PurchaseReceipt,
} from '@/lib/purchase-receipts';
import { PurchaseReceiptHistory } from '@/components/purchase-orders/PurchaseReceiptHistory';
import {
  PurchaseReceiptForm,
  type ReceiptAuthoritativeFailure,
} from '@/components/purchase-orders/PurchaseReceiptForm';

function lifecycleConflictMessage(error: ApiError): string {
  const detail = error.payload.detail;
  if (!detail || typeof detail !== 'object')
    return 'This action is no longer valid. The Purchase Order has been refreshed.';
  const code = (detail as { code?: unknown }).code;
  if (code === 'purchase_order_self_approval_forbidden')
    return 'Independent approval is required. A creator cannot approve their own Purchase Order.';
  if (code === 'purchase_order_has_receipts')
    return 'This Purchase Order can no longer be cancelled because receipts have been recorded.';
  return 'This action is no longer valid. The Purchase Order has been refreshed.';
}

function lifecycleReasonValidationMessage(error: ApiError): string | null {
  const detail = error.payload.detail;
  if (Array.isArray(detail)) {
    const reasonLocated = detail.some((item) => {
      if (!item || typeof item !== 'object') return false;
      const location = (item as { loc?: unknown }).loc;
      return Array.isArray(location) && location.some((part) => part === 'reason');
    });
    return reasonLocated ? 'The reason was not accepted. Review it and try again.' : null;
  }
  if (!detail || typeof detail !== 'object') return null;
  const structured = detail as { code?: unknown; context?: unknown };
  if (structured.code === 'reason_required')
    return 'The reason was not accepted. Review it and try again.';
  if (structured.context && typeof structured.context === 'object') {
    const field = (structured.context as { field?: unknown }).field;
    if (field === 'reason') return 'The reason was not accepted. Review it and try again.';
  }
  return null;
}

function runLifecycleOperation(
  purchaseOrderId: string,
  operation: PurchaseOrderLifecycleOperation,
  reason?: string,
): Promise<LifecycleResponse> {
  switch (operation) {
    case 'submit':
      return submitPurchaseOrder(purchaseOrderId);
    case 'withdraw':
      return withdrawPurchaseOrder(purchaseOrderId, reason ?? '');
    case 'approve':
      return approvePurchaseOrder(purchaseOrderId, reason);
    case 'reject':
      return rejectPurchaseOrder(purchaseOrderId, reason ?? '');
    case 'revise':
      return revisePurchaseOrder(purchaseOrderId, reason ?? '');
    case 'cancel':
      return cancelPurchaseOrder(purchaseOrderId, reason ?? '');
  }
}

export default function PurchaseOrderDetailPage() {
  const router = useRouter();
  const pathname = usePathname();
  const params = useParams<{ purchaseOrderId: string }>();
  const purchaseOrderId = params?.purchaseOrderId ?? '';
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [purchaseOrder, setPurchaseOrder] = useState<PurchaseOrder | null>(null);
  const [loading, setLoading] = useState(true);
  const [forbidden, setForbidden] = useState(false);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [detailRevision, setDetailRevision] = useState(0);
  const [detailIdentity, setDetailIdentity] = useState(purchaseOrderId);
  const [transitions, setTransitions] = useState<PurchaseOrderTransition[]>([]);
  const [transitionCursor, setTransitionCursor] = useState<string | undefined>();
  const [nextTransitionCursor, setNextTransitionCursor] = useState<string | null>(null);
  const [previousTransitionCursors, setPreviousTransitionCursors] = useState<string[]>([]);
  const [transitionsLoading, setTransitionsLoading] = useState(false);
  const [transitionsError, setTransitionsError] = useState<string | null>(null);
  const [transitionIdentity, setTransitionIdentity] = useState<string | null>(null);
  const [transitionRevision, setTransitionRevision] = useState(0);
  const [transitionNavigationPending, setTransitionNavigationPending] = useState(false);
  const [receipts, setReceipts] = useState<PurchaseReceipt[]>([]);
  const [receiptCursor, setReceiptCursor] = useState<string | undefined>();
  const [nextReceiptCursor, setNextReceiptCursor] = useState<string | null>(null);
  const [previousReceiptCursors, setPreviousReceiptCursors] = useState<string[]>([]);
  const [receiptsLoading, setReceiptsLoading] = useState(false);
  const [receiptsError, setReceiptsError] = useState<string | null>(null);
  const [receiptIdentity, setReceiptIdentity] = useState<string | null>(null);
  const [receiptRevision, setReceiptRevision] = useState(0);
  const [receiptNavigationPending, setReceiptNavigationPending] = useState(false);
  const [receiveOpen, setReceiveOpen] = useState(false);
  const [authRevision, setAuthRevision] = useState(0);
  const [warehouseLabels, setWarehouseLabels] = useState(new Map<string, string>());
  const [lifecycleError, setLifecycleError] = useState<{
    purchaseOrderId: string;
    message: string;
  } | null>(null);
  const [pendingMutation, setPendingMutation] = useState<{
    purchaseOrderId: string;
    token: number;
    visitGeneration: number;
    operation: PurchaseOrderLifecycleOperation;
  } | null>(null);
  const detailGenerationRef = useRef(0);
  const transitionGenerationRef = useRef(0);
  const receiptGenerationRef = useRef(0);
  const receiptOptionsGenerationRef = useRef(0);
  const receiptNavigationLockRef = useRef(false);
  const transitionNavigationLockRef = useRef(false);
  const activeMutationsRef = useRef(new Map<string, number>());
  const nextMutationTokenRef = useRef(0);
  const routeVisitGenerationRef = useRef(0);
  const focusStatusAfterRefreshRef = useRef<string | null>(null);
  const focusReceiptAfterRefreshRef = useRef<string | null>(null);
  const statusAnnouncementRef = useRef<HTMLDivElement>(null);
  const receivingHeadingRef = useRef<HTMLHeadingElement>(null);
  const currentIdRef = useRef(purchaseOrderId);
  const mountedRef = useRef(true);
  const returnToRef = useRef(pathname);
  const receiveButtonRef = useRef<HTMLButtonElement>(null);

  returnToRef.current = pathname;

  if (currentIdRef.current !== purchaseOrderId) {
    const previousId = currentIdRef.current;
    currentIdRef.current = purchaseOrderId;
    routeVisitGenerationRef.current += 1;
    activeMutationsRef.current.delete(previousId);
    focusStatusAfterRefreshRef.current = null;
    focusReceiptAfterRefreshRef.current = null;
    detailGenerationRef.current += 1;
    transitionGenerationRef.current += 1;
    receiptGenerationRef.current += 1;
    receiptOptionsGenerationRef.current += 1;
    transitionNavigationLockRef.current = false;
    receiptNavigationLockRef.current = false;
    setReceiveOpen(false);
  }

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      detailGenerationRef.current += 1;
      transitionGenerationRef.current += 1;
      receiptGenerationRef.current += 1;
      receiptOptionsGenerationRef.current += 1;
      routeVisitGenerationRef.current += 1;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    void apiFetch<CurrentUser>('/v1/auth/me')
      .then((me) => {
        if (!cancelled) setUser(me);
      })
      .catch((caught) => {
        if (!cancelled && caught instanceof ApiError && caught.status === 401) {
          router.push(`/login?returnTo=${encodeURIComponent(returnToRef.current)}`);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [authRevision, router]);

  useEffect(() => {
    if (!purchaseOrderId) return;
    const generation = ++detailGenerationRef.current;
    const capturedId = purchaseOrderId;
    const controller = new AbortController();
    const isCurrent = () =>
      mountedRef.current &&
      generation === detailGenerationRef.current &&
      capturedId === currentIdRef.current;
    setDetailIdentity(capturedId);
    setPurchaseOrder(null);
    setLoading(true);
    setForbidden(false);
    setNotFound(false);
    setError(null);
    setTransitions([]);
    setTransitionCursor(undefined);
    setNextTransitionCursor(null);
    setPreviousTransitionCursors([]);
    setTransitionsError(null);
    setTransitionIdentity(null);
    transitionNavigationLockRef.current = false;
    setTransitionNavigationPending(false);
    setReceipts([]);
    setReceiptCursor(undefined);
    setNextReceiptCursor(null);
    setPreviousReceiptCursors([]);
    setReceiptsError(null);
    setReceiptIdentity(null);
    receiptNavigationLockRef.current = false;
    setReceiptNavigationPending(false);
    void getPurchaseOrder(capturedId, controller.signal)
      .then((po) => {
        if (!isCurrent()) return;
        setPurchaseOrder(po);
        setForbidden(false);
        setNotFound(false);
        setError(null);
      })
      .catch((caught) => {
        if (!isCurrent() || (caught instanceof DOMException && caught.name === 'AbortError'))
          return;
        if (caught instanceof ApiError && caught.status === 401) {
          router.push(`/login?returnTo=${encodeURIComponent(returnToRef.current)}`);
        } else if (caught instanceof ApiError && caught.status === 403) {
          setForbidden(true);
        } else if (caught instanceof ApiError && caught.status === 404) {
          setNotFound(true);
        } else {
          setError('Unable to load this purchase order. Try again.');
        }
      })
      .finally(() => {
        if (isCurrent()) setLoading(false);
      });
    return () => controller.abort();
  }, [detailRevision, purchaseOrderId, router]);

  const loadTransitions = useCallback(() => {
    if (!purchaseOrder) return () => undefined;
    const generation = ++transitionGenerationRef.current;
    const capturedId = purchaseOrder.id;
    const capturedIdentity = `${capturedId}\u0000${transitionCursor ?? ''}`;
    const controller = new AbortController();
    const isCurrent = () =>
      mountedRef.current &&
      generation === transitionGenerationRef.current &&
      capturedId === currentIdRef.current;
    setTransitionsLoading(true);
    setTransitionsError(null);
    setTransitions([]);
    setNextTransitionCursor(null);
    setTransitionIdentity(capturedIdentity);
    void listPurchaseOrderTransitions(capturedId, {
      cursor: transitionCursor,
      limit: 50,
      signal: controller.signal,
    })
      .then((page) => {
        if (!isCurrent()) return;
        setTransitions(page.items);
        setNextTransitionCursor(page.next_cursor);
      })
      .catch((caught) => {
        if (!isCurrent() || (caught instanceof DOMException && caught.name === 'AbortError'))
          return;
        if (caught instanceof ApiError && caught.status === 401) {
          router.push(`/login?returnTo=${encodeURIComponent(returnToRef.current)}`);
        } else if (caught instanceof ApiError && caught.status === 403) {
          setTransitionsError('You do not have permission to view transition history.');
        } else if (caught instanceof ApiError && caught.status === 404) {
          setTransitionsError('Transition history is unavailable.');
        } else if (
          transitionCursor &&
          caught instanceof ApiError &&
          [400, 422].includes(caught.status)
        ) {
          setTransitionCursor(undefined);
          setPreviousTransitionCursors([]);
          toast(
            'That transition-history page is no longer valid. Returned to the first page.',
            'info',
          );
        } else {
          setTransitionsError('Unable to load transition history.');
        }
      })
      .finally(() => {
        if (isCurrent()) {
          transitionNavigationLockRef.current = false;
          setTransitionNavigationPending(false);
          setTransitionsLoading(false);
        }
      });
    return () => controller.abort();
  }, [purchaseOrder, router, transitionCursor]);

  useEffect(() => loadTransitions(), [loadTransitions, transitionRevision]);

  const receiptContext = visiblePermissionContext(purchaseOrder);
  const canReadReceipts = hasScopedPermission(user, 'purchase_receipt.read', receiptContext);
  const canCreateReceipt = hasScopedPermission(user, 'purchase_receipt.create', receiptContext);

  useEffect(() => {
    if (!purchaseOrder || !canReadReceipts) {
      setReceipts([]);
      setReceiptIdentity(null);
      return;
    }
    const generation = ++receiptGenerationRef.current;
    const capturedId = purchaseOrder.id;
    const identity = `${capturedId}\u0000${receiptCursor ?? ''}`;
    const controller = new AbortController();
    setReceiptsLoading(true);
    setReceiptsError(null);
    setReceipts([]);
    setNextReceiptCursor(null);
    setReceiptIdentity(identity);
    void listPurchaseReceipts(capturedId, {
      cursor: receiptCursor,
      limit: 50,
      signal: controller.signal,
    })
      .then((page) => {
        if (generation !== receiptGenerationRef.current || capturedId !== currentIdRef.current)
          return;
        setReceipts(page.items);
        setNextReceiptCursor(page.next_cursor);
      })
      .catch((caught) => {
        if (
          generation !== receiptGenerationRef.current ||
          (caught instanceof DOMException && caught.name === 'AbortError')
        )
          return;
        if (caught instanceof ApiError && caught.status === 401)
          router.push(`/login?returnTo=${encodeURIComponent(returnToRef.current)}`);
        else if (caught instanceof ApiError && caught.status === 403)
          setReceiptsError('You do not have permission to view receipt history.');
        else if (caught instanceof ApiError && caught.status === 404)
          setReceiptsError('Receipt history is unavailable.');
        else if (receiptCursor && caught instanceof ApiError && caught.status === 422) {
          setReceiptCursor(undefined);
          setPreviousReceiptCursors([]);
          toast(
            'That receipt-history page is no longer valid. Returned to the first page.',
            'info',
          );
        } else setReceiptsError('Unable to load receipt history.');
      })
      .finally(() => {
        if (generation === receiptGenerationRef.current) {
          receiptNavigationLockRef.current = false;
          setReceiptNavigationPending(false);
          setReceiptsLoading(false);
        }
      });
    return () => controller.abort();
  }, [canReadReceipts, purchaseOrder, receiptCursor, receiptRevision, router]);

  useEffect(() => {
    if (!purchaseOrder || (!canReadReceipts && !canCreateReceipt)) return;
    const generation = ++receiptOptionsGenerationRef.current;
    const controller = new AbortController();
    const capturedId = purchaseOrder.id;
    setWarehouseLabels(new Map());
    void listReceiptWarehouses(purchaseOrder.id, controller.signal)
      .then((warehouses) => {
        if (
          generation !== receiptOptionsGenerationRef.current ||
          capturedId !== currentIdRef.current
        )
          return;
        setWarehouseLabels(
          new Map(
            warehouses.map((warehouse) => [warehouse.id, `${warehouse.name} (${warehouse.code})`]),
          ),
        );
      })
      .catch(() => undefined);
    return () => controller.abort();
  }, [canCreateReceipt, canReadReceipts, purchaseOrder]);

  useEffect(() => {
    if (
      focusStatusAfterRefreshRef.current !== purchaseOrderId ||
      detailIdentity !== purchaseOrderId ||
      !purchaseOrder ||
      loading
    )
      return;
    focusStatusAfterRefreshRef.current = null;
    statusAnnouncementRef.current?.focus();
  }, [detailIdentity, loading, purchaseOrder, purchaseOrderId]);

  useEffect(() => {
    if (
      focusReceiptAfterRefreshRef.current !== purchaseOrderId ||
      detailIdentity !== purchaseOrderId ||
      !purchaseOrder ||
      loading
    )
      return;
    focusReceiptAfterRefreshRef.current = null;
    if (
      ['APPROVED', 'PARTIALLY_RECEIVED'].includes(purchaseOrder.status) &&
      receiveButtonRef.current
    ) {
      receiveButtonRef.current.focus();
    } else {
      (receivingHeadingRef.current ?? statusAnnouncementRef.current)?.focus();
    }
  }, [detailIdentity, loading, purchaseOrder, purchaseOrderId]);

  const detailIsCurrent = detailIdentity === purchaseOrderId;
  const visiblePurchaseOrder = detailIsCurrent ? purchaseOrder : null;
  const currentTransitionIdentity = visiblePurchaseOrder
    ? `${visiblePurchaseOrder.id}\u0000${transitionCursor ?? ''}`
    : null;
  const transitionsAreCurrent = transitionIdentity === currentTransitionIdentity;
  const visibleTransitions = transitionsAreCurrent ? transitions : [];
  const visibleTransitionsLoading =
    visiblePurchaseOrder !== null && (transitionsLoading || !transitionsAreCurrent);
  const visibleTransitionsError = transitionsAreCurrent ? transitionsError : null;
  const visibleNextTransitionCursor = transitionsAreCurrent ? nextTransitionCursor : null;

  function nextTransitions() {
    if (!nextTransitionCursor || transitionNavigationLockRef.current) return;
    transitionNavigationLockRef.current = true;
    setTransitionNavigationPending(true);
    setPreviousTransitionCursors((existing) => [...existing, transitionCursor || '']);
    setTransitionCursor(nextTransitionCursor);
  }

  function previousTransitions() {
    if (previousTransitionCursors.length === 0 || transitionNavigationLockRef.current) return;
    transitionNavigationLockRef.current = true;
    setTransitionNavigationPending(true);
    const previous = previousTransitionCursors[previousTransitionCursors.length - 1];
    setPreviousTransitionCursors((existing) => existing.slice(0, -1));
    setTransitionCursor(previous || undefined);
  }

  function closeReceive({ restoreFocus = true }: { restoreFocus?: boolean } = {}) {
    setReceiveOpen(false);
    if (restoreFocus) window.setTimeout(() => receiveButtonRef.current?.focus(), 0);
  }

  function refreshAfterReceiptFailure(failure: ReceiptAuthoritativeFailure) {
    closeReceive();
    if (failure === 'authorization-changed') {
      setAuthRevision((revision) => revision + 1);
      return;
    }
    if (failure === 'purchase-order-changed') {
      setReceiptCursor(undefined);
      setPreviousReceiptCursors([]);
      setDetailRevision((revision) => revision + 1);
    }
  }

  async function performLifecycleAction(
    operation: PurchaseOrderLifecycleOperation,
    reason?: string,
  ): Promise<PurchaseOrderLifecycleActionResult> {
    const capturedId = purchaseOrderId;
    if (!capturedId || activeMutationsRef.current.has(capturedId)) return { kind: 'failed' };
    const token = ++nextMutationTokenRef.current;
    const visitGeneration = routeVisitGenerationRef.current;
    activeMutationsRef.current.set(capturedId, token);
    setPendingMutation({ purchaseOrderId: capturedId, token, visitGeneration, operation });
    setLifecycleError(null);
    const isCurrent = () =>
      mountedRef.current &&
      currentIdRef.current === capturedId &&
      routeVisitGenerationRef.current === visitGeneration &&
      activeMutationsRef.current.get(capturedId) === token;
    try {
      const result = await runLifecycleOperation(capturedId, operation, reason);
      if (!isCurrent()) return { kind: 'failed' };
      detailGenerationRef.current += 1;
      transitionGenerationRef.current += 1;
      receiptGenerationRef.current += 1;
      focusStatusAfterRefreshRef.current = capturedId;
      setLoading(true);
      setPurchaseOrder(result.purchaseOrder);
      setDetailIdentity(capturedId);
      setTransitionCursor(undefined);
      setPreviousTransitionCursors([]);
      setTransitions([]);
      setTransitionIdentity(null);
      setNextTransitionCursor(null);
      setDetailRevision((revision) => revision + 1);
      setTransitionRevision((revision) => revision + 1);
      setReceiptCursor(undefined);
      setPreviousReceiptCursors([]);
      setReceiptRevision((revision) => revision + 1);
      if (result.replayed) {
        toast(
          'This lifecycle action was already applied. Current state has been refreshed.',
          'info',
        );
      } else {
        toast(`Purchase Order ${operation} completed.`, 'success');
      }
      return { kind: 'completed' };
    } catch (caught) {
      if (!isCurrent()) return { kind: 'failed' };
      if (caught instanceof ApiError && caught.status === 401) {
        router.push(`/login?returnTo=${encodeURIComponent(returnToRef.current)}`);
        return { kind: 'completed' };
      }
      if (caught instanceof ApiError && caught.status === 403) {
        setLifecycleError({
          purchaseOrderId: capturedId,
          message: 'You no longer have permission to perform this action.',
        });
        return { kind: 'failed' };
      }
      if (caught instanceof ApiError && caught.status === 404) {
        setNotFound(true);
        return { kind: 'completed' };
      }
      if (caught instanceof ApiError && caught.status === 409) {
        setLifecycleError({
          purchaseOrderId: capturedId,
          message: lifecycleConflictMessage(caught),
        });
        detailGenerationRef.current += 1;
        transitionGenerationRef.current += 1;
        setTransitionCursor(undefined);
        setPreviousTransitionCursors([]);
        setDetailRevision((revision) => revision + 1);
        setTransitionRevision((revision) => revision + 1);
        return { kind: 'failed' };
      }
      if (caught instanceof ApiError && caught.status === 422) {
        const reasonMessage = lifecycleReasonValidationMessage(caught);
        if (reasonMessage) return { kind: 'reason-error', message: reasonMessage };
        setLifecycleError({
          purchaseOrderId: capturedId,
          message: 'The lifecycle reason was not accepted. Review it and try again.',
        });
        return { kind: 'failed' };
      }
      setLifecycleError({
        purchaseOrderId: capturedId,
        message: 'Something went wrong. Please try again.',
      });
      return { kind: 'failed' };
    } finally {
      if (activeMutationsRef.current.get(capturedId) === token)
        activeMutationsRef.current.delete(capturedId);
      if (mountedRef.current)
        setPendingMutation((current) =>
          current?.purchaseOrderId === capturedId &&
          current.token === token &&
          current.visitGeneration === visitGeneration
            ? null
            : current,
        );
    }
  }

  if (!detailIsCurrent || (loading && !visiblePurchaseOrder)) {
    return (
      <main
        className="mx-auto max-w-6xl px-4 py-8 sm:px-6"
        data-testid="po-detail-loading"
        aria-busy="true"
      >
        <SkeletonRows rows={8} />
      </main>
    );
  }
  if (detailIsCurrent && forbidden)
    return (
      <main className="mx-auto max-w-6xl px-4 py-8 sm:px-6">
        <ForbiddenBanner />
      </main>
    );
  if (detailIsCurrent && notFound)
    return (
      <main className="mx-auto max-w-6xl px-4 py-8 sm:px-6">
        <EmptyState
          title="Purchase order unavailable"
          description="This resource does not exist or is not available to your scope."
        />
      </main>
    );
  if (detailIsCurrent && error)
    return (
      <main className="mx-auto max-w-6xl px-4 py-8 sm:px-6">
        <div role="alert">
          <ErrorBanner message={error} />
        </div>
      </main>
    );
  if (!visiblePurchaseOrder) return null;

  return (
    <main className="mx-auto max-w-6xl space-y-6 px-4 py-8 sm:px-6">
      <div
        ref={statusAnnouncementRef}
        tabIndex={-1}
        role="status"
        aria-live="polite"
        className="sr-only"
        data-testid="po-lifecycle-status-focus"
      >
        Purchase Order status updated to {visiblePurchaseOrder.status}.
      </div>
      <PurchaseOrderDetail purchaseOrder={visiblePurchaseOrder} currentUserId={user?.id ?? null} />
      <PurchaseOrderLifecycleActions
        purchaseOrder={visiblePurchaseOrder}
        user={user}
        pendingOperation={
          pendingMutation?.purchaseOrderId === purchaseOrderId &&
          pendingMutation.visitGeneration === routeVisitGenerationRef.current
            ? pendingMutation.operation
            : null
        }
        error={lifecycleError?.purchaseOrderId === purchaseOrderId ? lifecycleError.message : null}
        onAction={performLifecycleAction}
      />
      <PurchaseOrderTransitionHistory
        transitions={visibleTransitions}
        currentUserId={user?.id ?? null}
        loading={visibleTransitionsLoading}
        error={visibleTransitionsError}
        nextCursor={visibleNextTransitionCursor}
        canGoBack={previousTransitionCursors.length > 0}
        navigationPending={transitionNavigationPending}
        onNext={nextTransitions}
        onPrevious={previousTransitions}
      />
      {(canCreateReceipt || canReadReceipts) && (
        <section
          className="rounded-xl border border-border bg-card p-5"
          data-testid="purchase-receiving"
        >
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 ref={receivingHeadingRef} tabIndex={-1} className="font-display text-xl">
                Receiving
              </h2>
              <p className="mt-1 text-sm text-muted-foreground">
                Receive ordered goods and review immutable posted receipts.
              </p>
            </div>
            {canCreateReceipt &&
              ['APPROVED', 'PARTIALLY_RECEIVED'].includes(visiblePurchaseOrder.status) && (
                <button
                  ref={receiveButtonRef}
                  type="button"
                  className="rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground"
                  onClick={() => setReceiveOpen(true)}
                  data-testid="receive-po-action"
                >
                  Receive purchase order
                </button>
              )}
          </div>
        </section>
      )}
      {canReadReceipts && (
        <PurchaseReceiptHistory
          purchaseOrder={visiblePurchaseOrder}
          receipts={
            receiptIdentity === `${visiblePurchaseOrder.id}\u0000${receiptCursor ?? ''}`
              ? receipts
              : []
          }
          warehouseLabels={warehouseLabels}
          currentUserId={user?.id ?? null}
          loading={
            receiptsLoading ||
            receiptIdentity !== `${visiblePurchaseOrder.id}\u0000${receiptCursor ?? ''}`
          }
          error={receiptsError}
          nextCursor={nextReceiptCursor}
          canGoBack={previousReceiptCursors.length > 0}
          navigationPending={receiptNavigationPending}
          onNext={() => {
            if (!nextReceiptCursor || receiptNavigationLockRef.current) return;
            receiptNavigationLockRef.current = true;
            setReceiptNavigationPending(true);
            setPreviousReceiptCursors((current) => [...current, receiptCursor ?? '']);
            setReceiptCursor(nextReceiptCursor);
          }}
          onPrevious={() => {
            if (!previousReceiptCursors.length || receiptNavigationLockRef.current) return;
            receiptNavigationLockRef.current = true;
            setReceiptNavigationPending(true);
            const prior = previousReceiptCursors.at(-1) ?? '';
            setPreviousReceiptCursors((current) => current.slice(0, -1));
            setReceiptCursor(prior || undefined);
          }}
        />
      )}
      {canCreateReceipt && (
        <PurchaseReceiptForm
          purchaseOrder={visiblePurchaseOrder}
          open={receiveOpen}
          onClose={() => closeReceive()}
          onAuthoritativeFailure={refreshAfterReceiptFailure}
          onCompleted={(replayed) => {
            focusReceiptAfterRefreshRef.current = visiblePurchaseOrder.id;
            closeReceive({ restoreFocus: false });
            toast(
              replayed
                ? 'This receipt was already recorded. Current data has been refreshed.'
                : 'Purchase receipt posted.',
              replayed ? 'info' : 'success',
            );
            setReceiptCursor(undefined);
            setPreviousReceiptCursors([]);
            setDetailRevision((revision) => revision + 1);
          }}
        />
      )}
    </main>
  );
}

function visiblePermissionContext(purchaseOrder: PurchaseOrder | null) {
  if (!purchaseOrder) return null;
  return {
    organizationId: purchaseOrder.organization_id,
    ...(purchaseOrder.farm_id ? { farmId: purchaseOrder.farm_id } : {}),
  };
}
