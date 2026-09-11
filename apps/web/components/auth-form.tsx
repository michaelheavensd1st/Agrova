'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { apiFetch, ApiError } from '@/lib/api';
import { safeAdminReturnTo } from '@/lib/admin-users';

type Mode = 'login' | 'register';

interface Props {
  mode: Mode;
  returnTo?: string | null;
}

function boundedAuthError(error: unknown, mode: Mode): string {
  if (!(error instanceof ApiError)) {
    return 'The authentication service is unavailable. Please try again.';
  }
  if (error.status === 401 && mode === 'login') {
    return 'The email or password is incorrect.';
  }
  if (error.status === 422) {
    return 'Check the information you entered and try again.';
  }
  if (error.status === 429) {
    return 'Too many attempts. Please wait and try again.';
  }
  if (error.status >= 500) {
    return 'The authentication service is unavailable. Please try again.';
  }
  return 'Unable to complete authentication. Please try again.';
}

export function AuthForm({ mode, returnTo }: Props) {
  const router = useRouter();
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setSubmitting(true);

    const data = new FormData(event.currentTarget);
    const payload: Record<string, string> = {
      email: String(data.get('email') ?? ''),
      password: String(data.get('password') ?? ''),
    };
    if (mode === 'register') {
      const fullName = String(data.get('full_name') ?? '');
      if (fullName) payload.full_name = fullName;
    }

    try {
      const path = mode === 'login' ? '/v1/auth/login' : '/v1/auth/register';
      await apiFetch(path, {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      if (mode === 'register') {
        router.push('/verify');
      } else {
        router.push(safeAdminReturnTo(returnTo) ?? '/dashboard');
      }
    } catch (err) {
      setError(boundedAuthError(err, mode));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={onSubmit} className="mt-8 flex flex-col gap-4" data-testid={`${mode}-form`}>
      {mode === 'register' && (
        <label className="flex flex-col gap-1 text-sm">
          <span className="text-muted-foreground">Full name</span>
          <input
            name="full_name"
            type="text"
            autoComplete="name"
            data-testid="auth-fullname-input"
            className="rounded-md border border-input bg-background px-3 py-2 outline-none ring-ring focus:ring-2"
          />
        </label>
      )}

      <label className="flex flex-col gap-1 text-sm">
        <span className="text-muted-foreground">Email</span>
        <input
          name="email"
          type="email"
          required
          autoComplete="email"
          data-testid="auth-email-input"
          className="rounded-md border border-input bg-background px-3 py-2 outline-none ring-ring focus:ring-2"
        />
      </label>

      <label className="flex flex-col gap-1 text-sm">
        <span className="text-muted-foreground">Password</span>
        <input
          name="password"
          type="password"
          required
          minLength={8}
          autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
          data-testid="auth-password-input"
          className="rounded-md border border-input bg-background px-3 py-2 outline-none ring-ring focus:ring-2"
        />
      </label>

      {error && (
        <p
          role="alert"
          data-testid="auth-error"
          className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive"
        >
          {error}
        </p>
      )}

      <button
        type="submit"
        disabled={submitting}
        data-testid={`${mode}-submit-button`}
        className="mt-2 rounded-md bg-primary px-4 py-2.5 text-sm font-medium text-primary-foreground transition disabled:opacity-60"
      >
        {submitting ? 'Please wait…' : mode === 'login' ? 'Sign in' : 'Create account'}
      </button>
    </form>
  );
}
