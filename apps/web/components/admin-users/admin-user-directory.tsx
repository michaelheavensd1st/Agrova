'use client';

import Link from 'next/link';
import { useRouter, useSearchParams } from 'next/navigation';
import { FormEvent, useEffect, useMemo, useRef, useState } from 'react';
import { ApiError, apiFetch } from '@/lib/api';
import {
  ADMIN_USER_PAGE_LIMIT,
  adminUserDirectoryPath,
  boundedAdminError,
  directoryHref,
  normalizeDirectoryOffset,
  type AdminUserDirectoryQuery,
  type AdminUserPage,
} from '@/lib/admin-users';
import { AdminUserBadges } from '@/components/admin-users/admin-user-badges';

function parseQuery(searchParams: URLSearchParams): AdminUserDirectoryQuery {
  const status = searchParams.get('status');
  const verified = searchParams.get('verified');
  return {
    search: (searchParams.get('search') ?? '').slice(0, 255),
    status: status === 'active' || status === 'disabled' ? status : '',
    verified: verified === 'true' || verified === 'false' ? verified : '',
    offset: normalizeDirectoryOffset(searchParams.get('offset')),
  };
}

export function AdminUserDirectory() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const queryKey = searchParams.toString();
  const query = useMemo(() => parseQuery(new URLSearchParams(queryKey)), [queryKey]);
  const [searchDraft, setSearchDraft] = useState(query.search);
  const [page, setPage] = useState<AdminUserPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [forbidden, setForbidden] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const generationRef = useRef(0);
  const mountedRef = useRef(false);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      generationRef.current += 1;
    };
  }, []);

  useEffect(() => setSearchDraft(query.search), [query.search]);

  useEffect(() => {
    const generation = ++generationRef.current;
    const isCurrent = () => mountedRef.current && generationRef.current === generation;
    setLoading(true);
    setForbidden(false);
    setError(null);
    void apiFetch<AdminUserPage>(adminUserDirectoryPath(query))
      .then((result) => {
        if (!isCurrent()) return;
        if (query.offset > 0 && result.items.length === 0 && result.total > 0) {
          const nearestValidOffset =
            Math.floor((result.total - 1) / ADMIN_USER_PAGE_LIMIT) * ADMIN_USER_PAGE_LIMIT;
          if (nearestValidOffset !== query.offset) {
            router.replace(directoryHref({ ...query, offset: nearestValidOffset }));
            return;
          }
        }
        setPage(result);
      })
      .catch((requestError: unknown) => {
        if (!isCurrent()) return;
        setPage(null);
        if (requestError instanceof ApiError && requestError.status === 401) {
          router.push(`/login?returnTo=${encodeURIComponent(directoryHref(query))}`);
        } else if (requestError instanceof ApiError && requestError.status === 403) {
          setForbidden(true);
        } else {
          setError(boundedAdminError(requestError, 'directory'));
        }
      })
      .finally(() => {
        if (isCurrent()) setLoading(false);
      });
  }, [query, router]);

  function navigate(next: AdminUserDirectoryQuery) {
    router.replace(directoryHref(next));
  }

  function submitSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    navigate({ ...query, search: searchDraft.slice(0, 255), offset: 0 });
  }

  const hasFilters = Boolean(query.search || query.status || query.verified);
  const rangeStart = page && page.total > 0 ? page.offset + 1 : 0;
  const rangeEnd = page ? Math.min(page.offset + page.items.length, page.total) : 0;

  return (
    <main className="mx-auto max-w-6xl px-6 py-10" data-testid="admin-user-directory">
      <div className="mb-7">
        <p className="text-xs uppercase tracking-widest text-muted-foreground">
          Platform administration
        </p>
        <h1 className="font-display text-3xl">Users</h1>
        <p className="mt-2 text-sm text-muted-foreground">
          Search and inspect platform accounts. Administrative actions are recorded by the server.
        </p>
      </div>

      <section className="rounded-xl border border-border bg-card p-4" aria-label="User filters">
        <form
          onSubmit={submitSearch}
          className="grid gap-3 md:grid-cols-[1fr_auto_auto_auto]"
          data-testid="admin-user-filter-form"
        >
          <div>
            <label htmlFor="admin-user-search" className="text-sm font-medium">
              Email or full name
            </label>
            <input
              id="admin-user-search"
              value={searchDraft}
              onChange={(event) => setSearchDraft(event.target.value)}
              maxLength={255}
              className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
            />
          </div>
          <div>
            <label htmlFor="admin-user-status" className="text-sm font-medium">
              Account status
            </label>
            <select
              id="admin-user-status"
              value={query.status}
              onChange={(event) =>
                navigate({
                  ...query,
                  status: event.target.value as AdminUserDirectoryQuery['status'],
                  offset: 0,
                })
              }
              className="mt-1 block rounded-md border border-border bg-background px-3 py-2 text-sm"
            >
              <option value="">All statuses</option>
              <option value="active">Active</option>
              <option value="disabled">Disabled</option>
            </select>
          </div>
          <div>
            <label htmlFor="admin-user-verified" className="text-sm font-medium">
              Verification
            </label>
            <select
              id="admin-user-verified"
              value={query.verified}
              onChange={(event) =>
                navigate({
                  ...query,
                  verified: event.target.value as AdminUserDirectoryQuery['verified'],
                  offset: 0,
                })
              }
              className="mt-1 block rounded-md border border-border bg-background px-3 py-2 text-sm"
            >
              <option value="">All verification states</option>
              <option value="true">Verified</option>
              <option value="false">Unverified</option>
            </select>
          </div>
          <button
            type="submit"
            className="self-end rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground"
          >
            Search
          </button>
        </form>
      </section>

      <section className="mt-6" aria-live="polite" aria-busy={loading}>
        {loading && (
          <p
            className="rounded-xl border border-border bg-card p-6 text-sm"
            data-testid="admin-users-loading"
          >
            Loading users…
          </p>
        )}
        {!loading && forbidden && (
          <div
            className="rounded-xl border border-destructive/30 bg-destructive/10 p-5"
            role="alert"
            data-testid="admin-users-forbidden"
          >
            <h2 className="font-semibold">Access denied</h2>
            <p className="mt-1 text-sm">You do not have access to platform administration.</p>
          </div>
        )}
        {!loading && error && (
          <p
            className="rounded-xl bg-destructive/10 p-5 text-sm text-destructive"
            role="alert"
            data-testid="admin-users-error"
          >
            {error}
          </p>
        )}
        {!loading && page && page.items.length === 0 && (
          <div
            className="rounded-xl border border-dashed border-border p-8 text-center"
            data-testid="admin-users-empty"
          >
            <h2 className="font-display text-xl">
              {hasFilters ? 'No matching users' : 'No users available'}
            </h2>
            <p className="mt-2 text-sm text-muted-foreground">
              {hasFilters
                ? 'Change or clear the directory filters.'
                : 'The platform directory is empty.'}
            </p>
          </div>
        )}
        {!loading && page && page.items.length > 0 && (
          <>
            <div className="overflow-x-auto rounded-xl border border-border bg-card">
              <table className="w-full text-left text-sm">
                <thead className="border-b border-border bg-secondary/40">
                  <tr>
                    <th scope="col" className="px-4 py-3">
                      User
                    </th>
                    <th scope="col" className="px-4 py-3">
                      Status
                    </th>
                    <th scope="col" className="px-4 py-3">
                      Created
                    </th>
                    <th scope="col" className="px-4 py-3">
                      <span className="sr-only">Open</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {page.items.map((user) => (
                    <tr key={user.id} className="border-b border-border last:border-0">
                      <td className="px-4 py-3">
                        <p className="font-medium">{user.full_name || 'Unnamed user'}</p>
                        <p className="text-muted-foreground">{user.email}</p>
                      </td>
                      <td className="px-4 py-3">
                        <AdminUserBadges user={user} />
                      </td>
                      <td className="px-4 py-3">
                        {new Date(user.created_at).toLocaleDateString()}
                      </td>
                      <td className="px-4 py-3 text-right">
                        <Link
                          href={`/admin/users/${user.id}?returnTo=${encodeURIComponent(directoryHref(query))}`}
                          className="font-medium text-primary hover:underline"
                        >
                          View details
                        </Link>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <nav
              className="mt-4 flex items-center justify-between gap-4"
              aria-label="User directory pagination"
            >
              <button
                type="button"
                disabled={page.offset === 0}
                onClick={() =>
                  navigate({ ...query, offset: Math.max(0, page.offset - ADMIN_USER_PAGE_LIMIT) })
                }
                className="rounded-md border border-border px-3 py-2 text-sm disabled:opacity-50"
              >
                Previous
              </button>
              <p className="text-sm text-muted-foreground">
                Showing {rangeStart}–{rangeEnd} of {page.total}
              </p>
              <button
                type="button"
                disabled={page.offset + page.items.length >= page.total}
                onClick={() => navigate({ ...query, offset: page.offset + ADMIN_USER_PAGE_LIMIT })}
                className="rounded-md border border-border px-3 py-2 text-sm disabled:opacity-50"
              >
                Next
              </button>
            </nav>
          </>
        )}
      </section>
    </main>
  );
}
