'use client';

import { useEffect, useRef, useState } from 'react';
import { normalizeAdminReason, type AdminUser, type AdminUserAction } from '@/lib/admin-users';

const ACTION_COPY: Record<
  AdminUserAction,
  { label: string; title: string; consequence: string; destructive: boolean }
> = {
  disable: {
    label: 'Disable user',
    title: 'Disable this user?',
    consequence:
      'This disables sign-in, revokes active refresh sessions, and invalidates outstanding password-recovery links.',
    destructive: true,
  },
  enable: {
    label: 'Enable user',
    title: 'Enable this user?',
    consequence:
      'This restores sign-in eligibility. It does not restore revoked sessions or invalidated password-recovery links.',
    destructive: false,
  },
  'revoke-sessions': {
    label: 'Revoke sessions',
    title: 'Revoke all sessions?',
    consequence:
      'This revokes every active refresh session for this user. It does not change their password or password-recovery links.',
    destructive: true,
  },
};

export type AdminActionResult =
  { ok: true } | { ok: false; message: string; reasonInvalid?: boolean };

export function AdminUserActionDialog({
  action,
  user,
  onClose,
  onConfirm,
  trigger,
}: {
  action: AdminUserAction;
  user: AdminUser;
  onClose: () => void;
  onConfirm: (action: AdminUserAction, reason: string) => Promise<AdminActionResult>;
  trigger: HTMLButtonElement | null;
}) {
  const copy = ACTION_COPY[action];
  const [reason, setReason] = useState('');
  const [reasonError, setReasonError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const dialogRef = useRef<HTMLDivElement>(null);
  const reasonRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    reasonRef.current?.focus();
  }, []);

  function close() {
    if (busyRef.current) return;
    onClose();
    window.setTimeout(() => {
      if (trigger?.isConnected && !trigger.disabled) trigger.focus();
    }, 0);
  }

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        close();
        return;
      }
      if (event.key !== 'Tab') return;
      const focusable = Array.from(
        dialogRef.current?.querySelectorAll<HTMLElement>(
          'button:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ) ?? [],
      );
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  });

  async function confirm() {
    if (busyRef.current) return;
    const normalized = normalizeAdminReason(reason);
    if (!normalized) {
      setReasonError('Enter a reason between 1 and 500 characters.');
      reasonRef.current?.focus();
      return;
    }
    busyRef.current = true;
    setBusy(true);
    setReasonError(null);
    setActionError(null);
    try {
      const result = await onConfirm(action, normalized);
      if (result.ok) {
        onClose();
        return;
      }
      if (result.reasonInvalid) {
        setReasonError(result.message);
        reasonRef.current?.focus();
      } else {
        setActionError(result.message);
      }
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-40 flex items-center justify-center bg-black/40 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="admin-user-action-title"
      aria-describedby="admin-user-action-description"
      data-testid="admin-user-action-dialog"
    >
      <div
        ref={dialogRef}
        className="max-h-[calc(100vh-2rem)] w-full max-w-md overflow-y-auto rounded-2xl border border-border bg-card p-5 shadow-lg"
      >
        <h2 id="admin-user-action-title" className="font-display text-xl">
          {copy.title}
        </h2>
        <p id="admin-user-action-description" className="mt-2 text-sm text-muted-foreground">
          {user.full_name || user.email} ({user.email}). {copy.consequence}
        </p>
        <div className="mt-4">
          <label htmlFor="admin-user-action-reason" className="text-sm font-medium">
            Administrative reason (required)
          </label>
          <textarea
            id="admin-user-action-reason"
            ref={reasonRef}
            value={reason}
            onChange={(event) => {
              setReason(event.target.value);
              if (reasonError) setReasonError(null);
            }}
            rows={4}
            maxLength={500}
            disabled={busy}
            aria-invalid={reasonError ? true : undefined}
            aria-describedby={reasonError ? 'admin-user-action-reason-error' : undefined}
            className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
          />
          {reasonError && (
            <p
              id="admin-user-action-reason-error"
              className="mt-1 text-sm text-destructive"
              role="alert"
            >
              {reasonError}
            </p>
          )}
        </div>
        {actionError && (
          <p
            className="mt-3 text-sm text-destructive"
            role="alert"
            data-testid="admin-action-error"
          >
            {actionError}
          </p>
        )}
        <div className="mt-5 flex justify-end gap-2">
          <button
            type="button"
            onClick={close}
            disabled={busy}
            className="rounded-md border border-border px-3 py-2 text-sm disabled:opacity-60"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => void confirm()}
            disabled={busy}
            data-testid="admin-user-action-confirm"
            data-destructive={copy.destructive ? 'true' : 'false'}
            className={`rounded-md px-3 py-2 text-sm font-medium text-primary-foreground disabled:opacity-60 ${
              copy.destructive ? 'bg-destructive' : 'bg-primary'
            }`}
          >
            {busy ? 'Working…' : copy.label}
          </button>
        </div>
      </div>
    </div>
  );
}
