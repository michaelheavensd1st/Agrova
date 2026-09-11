'use client';

import Link from 'next/link';
import { useRouter, useSearchParams } from 'next/navigation';
import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, apiFetch } from '@/lib/api';
import {
  boundedAdminError,
  type AdminUser,
  type AdminUserAction,
  type AdminUserSessionsRevokeResponse,
} from '@/lib/admin-users';
import type { CurrentUser } from '@/lib/types';
import { AdminUserBadges } from '@/components/admin-users/admin-user-badges';
import {
  AdminUserActionDialog,
  type AdminActionResult,
} from '@/components/admin-users/admin-user-action-dialog';

function safeReturnTo(raw: string | null): string {
  if (!raw) return '/admin/users';
  return raw === '/admin/users' || raw.startsWith('/admin/users?') ? raw : '/admin/users';
}

export function AdminUserDetail({ userId }: { userId: string }) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const returnTo = safeReturnTo(searchParams.get('returnTo'));
  const [currentUser, setCurrentUser] = useState<CurrentUser | null>(null);
  const [user, setUser] = useState<AdminUser | null>(null);
  const [loading, setLoading] = useState(true);
  const [forbidden, setForbidden] = useState(false);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [selectedAction, setSelectedAction] = useState<AdminUserAction | null>(null);
  const [actionTrigger, setActionTrigger] = useState<HTMLButtonElement | null>(null);
  const generationRef = useRef(0);
  const mountedRef = useRef(false);
  const successRef = useRef<HTMLParagraphElement>(null);
  const currentUserIdRef = useRef(userId);
  currentUserIdRef.current = userId;

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      generationRef.current += 1;
    };
  }, []);

  const load = useCallback(async () => {
    const generation = ++generationRef.current;
    const isCurrent = () => mountedRef.current && generationRef.current === generation;
    setLoading(true);
    setForbidden(false);
    setNotFound(false);
    setError(null);
    setSelectedAction(null);
    try {
      const [viewer, target] = await Promise.all([
        apiFetch<CurrentUser>('/v1/auth/me'),
        apiFetch<AdminUser>(`/v1/admin/users/${userId}`),
      ]);
      if (!isCurrent()) return;
      setCurrentUser(viewer);
      setUser(target);
    } catch (requestError) {
      if (!isCurrent()) return;
      setCurrentUser(null);
      setUser(null);
      if (requestError instanceof ApiError && requestError.status === 401) {
        router.push(`/login?returnTo=${encodeURIComponent(`/admin/users/${userId}`)}`);
      } else if (requestError instanceof ApiError && requestError.status === 403) {
        setForbidden(true);
      } else if (requestError instanceof ApiError && requestError.status === 404) {
        setNotFound(true);
      } else {
        setError(boundedAdminError(requestError, 'detail'));
      }
    } finally {
      if (isCurrent()) setLoading(false);
    }
  }, [router, userId]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (success) successRef.current?.focus();
  }, [success]);

  function openAction(action: AdminUserAction, trigger: HTMLButtonElement) {
    setSuccess(null);
    setActionTrigger(trigger);
    setSelectedAction(action);
  }

  async function performAction(
    action: AdminUserAction,
    reason: string,
  ): Promise<AdminActionResult> {
    if (!user) return { ok: false, message: 'This user is no longer available.' };
    const capturedUserId = user.id;
    const capturedGeneration = generationRef.current;
    const isCurrentTarget = () =>
      mountedRef.current &&
      generationRef.current === capturedGeneration &&
      currentUserIdRef.current === capturedUserId;
    const actionPath =
      action === 'revoke-sessions'
        ? `/v1/admin/users/${capturedUserId}/sessions/revoke`
        : `/v1/admin/users/${capturedUserId}/${action}`;
    let revokedSessions: number | null = null;
    try {
      if (action === 'revoke-sessions') {
        const result = await apiFetch<AdminUserSessionsRevokeResponse>(actionPath, {
          method: 'POST',
          body: JSON.stringify({ reason }),
        });
        revokedSessions = result.revoked_sessions;
      } else {
        await apiFetch<AdminUser>(actionPath, {
          method: 'POST',
          body: JSON.stringify({ reason }),
        });
      }

      let authoritative: AdminUser;
      try {
        authoritative = await apiFetch<AdminUser>(`/v1/admin/users/${capturedUserId}`);
      } catch (reconciliationError) {
        if (!isCurrentTarget()) return { ok: true };
        setUser(null);
        setError(
          'The action completed, but the current account state could not be reloaded. Refresh this page.',
        );
        if (reconciliationError instanceof ApiError && reconciliationError.status === 401) {
          router.push('/login');
        } else if (reconciliationError instanceof ApiError && reconciliationError.status === 404) {
          setNotFound(true);
        }
        return { ok: true };
      }
      if (!isCurrentTarget()) return { ok: true };
      setUser(authoritative);
      setSuccess(
        action === 'disable'
          ? 'User disabled. Active sessions and outstanding recovery links were invalidated.'
          : action === 'enable'
            ? 'User enabled. Previously revoked sessions and recovery links remain unavailable.'
            : `${revokedSessions ?? 0} active session${revokedSessions === 1 ? '' : 's'} revoked.`,
      );
      return { ok: true };
    } catch (requestError) {
      if (requestError instanceof ApiError && requestError.status === 401) {
        router.push(`/login?returnTo=${encodeURIComponent(`/admin/users/${capturedUserId}`)}`);
        return { ok: false, message: 'Your session has expired.' };
      }
      if (requestError instanceof ApiError && requestError.status === 404) {
        setUser(null);
        setNotFound(true);
      }
      return {
        ok: false,
        message: boundedAdminError(
          requestError,
          requestError instanceof ApiError && requestError.status === 422 ? 'reason' : 'action',
        ),
        reasonInvalid: requestError instanceof ApiError && requestError.status === 422,
      };
    }
  }

  const isSelf = Boolean(currentUser && user && currentUser.id === user.id);

  return (
    <main className="mx-auto max-w-4xl px-6 py-10" data-testid="admin-user-detail">
      <Link href={returnTo} className="text-sm text-primary hover:underline">
        ← Back to users
      </Link>

      <section className="mt-6" aria-live="polite" aria-busy={loading}>
        {loading && <p data-testid="admin-user-detail-loading">Loading user…</p>}
        {!loading && forbidden && (
          <div
            className="rounded-xl bg-destructive/10 p-5"
            role="alert"
            data-testid="admin-user-detail-forbidden"
          >
            <h1 className="font-display text-2xl">Access denied</h1>
            <p className="mt-1 text-sm">You do not have access to platform administration.</p>
          </div>
        )}
        {!loading && notFound && (
          <div
            className="rounded-xl border border-dashed border-border p-6"
            data-testid="admin-user-not-found"
          >
            <h1 className="font-display text-2xl">User unavailable</h1>
            <p className="mt-1 text-sm text-muted-foreground">This user is no longer available.</p>
          </div>
        )}
        {!loading && error && (
          <p
            className="rounded-xl bg-destructive/10 p-5 text-sm text-destructive"
            role="alert"
            data-testid="admin-user-detail-error"
          >
            {error}
          </p>
        )}
        {!loading && user && currentUser && (
          <>
            <div className="rounded-2xl border border-border bg-card p-6">
              <div className="flex flex-wrap items-start justify-between gap-4">
                <div>
                  <p className="text-xs uppercase tracking-widest text-muted-foreground">
                    Platform user
                  </p>
                  <h1 className="mt-1 font-display text-3xl">{user.full_name || 'Unnamed user'}</h1>
                  <p className="mt-1 text-muted-foreground">{user.email}</p>
                </div>
                <AdminUserBadges user={user} />
              </div>
              <dl className="mt-6 grid gap-4 sm:grid-cols-2">
                <div>
                  <dt className="text-xs uppercase text-muted-foreground">Created</dt>
                  <dd>{new Date(user.created_at).toLocaleString()}</dd>
                </div>
                <div>
                  <dt className="text-xs uppercase text-muted-foreground">Last updated</dt>
                  <dd>{new Date(user.updated_at).toLocaleString()}</dd>
                </div>
              </dl>
            </div>

            {success && (
              <p
                ref={successRef}
                tabIndex={-1}
                className="mt-4 rounded-md bg-primary/10 px-3 py-2 text-sm text-primary"
                role="status"
                data-testid="admin-action-success"
              >
                {success}
              </p>
            )}

            <section
              className="mt-6 rounded-2xl border border-border bg-card p-6"
              aria-labelledby="admin-actions-heading"
            >
              <h2 id="admin-actions-heading" className="font-display text-xl">
                Administrative actions
              </h2>
              {isSelf && (
                <p
                  className="mt-2 text-sm text-muted-foreground"
                  data-testid="admin-self-action-note"
                >
                  You cannot disable your own account or revoke all of your own sessions.
                </p>
              )}
              <div className="mt-4 flex flex-wrap gap-3">
                {user.is_active ? (
                  !isSelf && (
                    <button
                      type="button"
                      onClick={(event) => openAction('disable', event.currentTarget)}
                      className="rounded-md border border-destructive px-3 py-2 text-sm font-medium text-destructive hover:bg-destructive/10"
                      data-testid="admin-disable-user"
                    >
                      Disable user
                    </button>
                  )
                ) : (
                  <button
                    type="button"
                    onClick={(event) => openAction('enable', event.currentTarget)}
                    className="rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground"
                    data-testid="admin-enable-user"
                  >
                    Enable user
                  </button>
                )}
                {!isSelf && (
                  <button
                    type="button"
                    onClick={(event) => openAction('revoke-sessions', event.currentTarget)}
                    className="rounded-md border border-destructive px-3 py-2 text-sm font-medium text-destructive hover:bg-destructive/10"
                    data-testid="admin-revoke-sessions"
                  >
                    Revoke sessions
                  </button>
                )}
              </div>
            </section>
          </>
        )}
      </section>

      {selectedAction && user && (
        <AdminUserActionDialog
          action={selectedAction}
          user={user}
          trigger={actionTrigger}
          onClose={() => setSelectedAction(null)}
          onConfirm={performAction}
        />
      )}
    </main>
  );
}
