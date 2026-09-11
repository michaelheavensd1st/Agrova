import { ApiError } from '@/lib/api';

export const ADMIN_USER_PAGE_LIMIT = 50;

export interface AdminUser {
  id: string;
  email: string;
  full_name: string | null;
  is_active: boolean;
  is_verified: boolean;
  is_superuser: boolean;
  created_at: string;
  updated_at: string;
}

export interface AdminUserPage {
  items: AdminUser[];
  total: number;
  limit: number;
  offset: number;
}

export interface AdminUserSessionsRevokeResponse {
  user: AdminUser;
  revoked_sessions: number;
}

export type AdminUserAction = 'disable' | 'enable' | 'revoke-sessions';

export interface AdminUserDirectoryQuery {
  search: string;
  status: '' | 'active' | 'disabled';
  verified: '' | 'true' | 'false';
  offset: number;
}

export function adminUserDirectoryPath(query: AdminUserDirectoryQuery): string {
  const params = new URLSearchParams();
  const search = query.search.trim();
  if (search) params.set('search', search);
  if (query.status) params.set('status', query.status);
  if (query.verified) params.set('verified', query.verified);
  params.set('limit', String(ADMIN_USER_PAGE_LIMIT));
  if (query.offset > 0) params.set('offset', String(query.offset));
  return `/v1/admin/users?${params.toString()}`;
}

export function normalizeDirectoryOffset(raw: string | null): number {
  if (!raw || !/^\d+$/.test(raw)) return 0;
  const parsed = Number(raw);
  return Number.isSafeInteger(parsed) ? parsed : 0;
}

export function normalizeAdminReason(reason: string): string | null {
  const normalized = reason.trim();
  return normalized.length >= 1 && normalized.length <= 500 ? normalized : null;
}

export type AdminErrorContext = 'directory' | 'detail' | 'reason' | 'action';

export function boundedAdminError(error: unknown, context: AdminErrorContext): string {
  if (!(error instanceof ApiError)) {
    return context === 'directory'
      ? 'Unable to load the user directory. Try again.'
      : context === 'detail'
        ? 'Unable to load this user. Try again.'
        : 'Unable to complete this administration action. Try again.';
  }
  if (error.status === 403) return 'You do not have access to platform administration.';
  if (error.status === 404) return 'This user is no longer available.';
  if (error.status === 409) return 'This action is not allowed for the selected account.';
  if (error.status === 422 && context === 'reason') {
    return 'Enter a reason between 1 and 500 characters.';
  }
  if (context === 'directory') return 'Unable to load the user directory. Try again.';
  if (context === 'detail') return 'Unable to load this user. Try again.';
  return 'Unable to complete this administration action. Try again.';
}

export function directoryHref(query: AdminUserDirectoryQuery): string {
  const params = new URLSearchParams();
  const search = query.search.trim();
  if (search) params.set('search', search);
  if (query.status) params.set('status', query.status);
  if (query.verified) params.set('verified', query.verified);
  if (query.offset > 0) params.set('offset', String(query.offset));
  const encoded = params.toString();
  return encoded ? `/admin/users?${encoded}` : '/admin/users';
}

const ADMIN_USER_ID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const DIRECTORY_QUERY_KEYS = new Set(['search', 'status', 'verified', 'offset']);

export function safeAdminReturnTo(raw: string | null | undefined): string | null {
  if (!raw || !raw.startsWith('/') || raw.startsWith('//') || raw.includes('\\')) return null;
  try {
    decodeURIComponent(raw);
  } catch {
    return null;
  }

  let destination: URL;
  try {
    destination = new URL(raw, 'https://agrovix.internal');
  } catch {
    return null;
  }
  if (destination.origin !== 'https://agrovix.internal' || destination.hash) return null;

  if (destination.pathname === '/admin/users') {
    const keys = Array.from(destination.searchParams.keys());
    if (keys.some((key, index) => !DIRECTORY_QUERY_KEYS.has(key) || keys.indexOf(key) !== index)) {
      return null;
    }
    const search = destination.searchParams.get('search') ?? '';
    const status = destination.searchParams.get('status');
    const verified = destination.searchParams.get('verified');
    const offsetRaw = destination.searchParams.get('offset');
    const offset = offsetRaw === null || /^\d+$/.test(offsetRaw) ? Number(offsetRaw ?? 0) : NaN;
    if (search.length > 255) return null;
    if (status !== null && status !== 'active' && status !== 'disabled') return null;
    if (verified !== null && verified !== 'true' && verified !== 'false') return null;
    if (!Number.isSafeInteger(offset) || offset < 0 || offset % ADMIN_USER_PAGE_LIMIT !== 0) {
      return null;
    }
    return directoryHref({
      search,
      status: status ?? '',
      verified: verified ?? '',
      offset,
    });
  }

  const detailMatch = destination.pathname.match(/^\/admin\/users\/([^/]+)$/);
  if (detailMatch && ADMIN_USER_ID_PATTERN.test(detailMatch[1]) && destination.search === '') {
    return destination.pathname;
  }
  return null;
}
