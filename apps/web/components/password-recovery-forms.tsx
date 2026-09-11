'use client';

import Link from 'next/link';
import { useEffect, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import { apiFetch, ApiError } from '@/lib/api';

interface MessageResponse {
  message: string;
}

const GENERIC_REQUEST_MESSAGE =
  'If an eligible account exists, password recovery instructions will be sent.';
const RECOVERY_TOKEN_MIN_LENGTH = 32;
const RECOVERY_TOKEN_MAX_LENGTH = 256;

function operationalError(error: unknown): string {
  if (error instanceof ApiError && error.status === 429) {
    return 'Too many recovery requests. Please try again later.';
  }
  if (error instanceof ApiError && error.status === 422) {
    return 'Enter a valid email address.';
  }
  return 'Unable to request password recovery right now. Please try again.';
}

export function ForgotPasswordForm() {
  const submittingRef = useRef(false);
  const emailRef = useRef<HTMLInputElement>(null);
  const outcomeRef = useRef<HTMLParagraphElement>(null);
  const [submitting, setSubmitting] = useState(false);
  const [accepted, setAccepted] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [errorTarget, setErrorTarget] = useState<'email' | 'summary' | null>(null);

  useEffect(() => {
    if (accepted) outcomeRef.current?.focus();
    else if (errorTarget === 'email') emailRef.current?.focus();
    else if (error) outcomeRef.current?.focus();
  }, [accepted, error, errorTarget]);

  async function onSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submittingRef.current) return;
    submittingRef.current = true;
    setSubmitting(true);
    setAccepted(false);
    setError(null);
    setErrorTarget(null);

    const form = new FormData(event.currentTarget);
    try {
      await apiFetch<MessageResponse>('/v1/auth/recovery/request', {
        method: 'POST',
        body: JSON.stringify({ email: String(form.get('email') ?? '') }),
      });
      setAccepted(true);
    } catch (requestError) {
      setError(operationalError(requestError));
      setErrorTarget(
        requestError instanceof ApiError && requestError.status === 422 ? 'email' : 'summary',
      );
    } finally {
      submittingRef.current = false;
      setSubmitting(false);
    }
  }

  return (
    <form
      onSubmit={onSubmit}
      className="mt-8 flex flex-col gap-4"
      data-testid="forgot-password-form"
    >
      <label className="flex flex-col gap-1 text-sm">
        <span className="text-muted-foreground">Email</span>
        <input
          ref={emailRef}
          id="recovery-email"
          name="email"
          type="email"
          required
          autoComplete="email"
          data-testid="recovery-email-input"
          aria-invalid={errorTarget === 'email'}
          aria-describedby={errorTarget === 'email' ? 'recovery-request-error' : undefined}
          className="rounded-md border border-input bg-background px-3 py-2 outline-none ring-ring focus:ring-2"
        />
      </label>

      {accepted && (
        <p
          ref={outcomeRef}
          tabIndex={-1}
          role="status"
          id="recovery-request-success"
          data-testid="recovery-request-success"
          className="rounded-md bg-primary/10 px-3 py-2 text-sm text-primary outline-none"
        >
          {GENERIC_REQUEST_MESSAGE}
        </p>
      )}
      {error && (
        <p
          ref={outcomeRef}
          tabIndex={-1}
          role="alert"
          id="recovery-request-error"
          data-testid="recovery-request-error"
          className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive outline-none"
        >
          {error}
        </p>
      )}

      <button
        type="submit"
        disabled={submitting}
        data-testid="recovery-request-submit"
        className="rounded-md bg-primary px-4 py-2.5 text-sm font-medium text-primary-foreground transition disabled:opacity-60"
      >
        {submitting ? 'Sending…' : 'Send recovery instructions'}
      </button>
    </form>
  );
}

export function ResetPasswordForm({ initialToken }: { initialToken: string | null }) {
  const router = useRouter();
  const formRef = useRef<HTMLFormElement>(null);
  const submittingRef = useRef(false);
  const passwordRef = useRef<HTMLInputElement>(null);
  const confirmationRef = useRef<HTMLInputElement>(null);
  const outcomeRef = useRef<HTMLParagraphElement>(null);
  const [token, setToken] = useState(initialToken ?? '');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [errorTarget, setErrorTarget] = useState<'password' | 'confirmation' | 'summary' | null>(
    null,
  );
  const [terminalState, setTerminalState] = useState<'invalid' | 'success' | null>(null);

  useEffect(() => {
    if (terminalState) {
      outcomeRef.current?.focus();
      if (terminalState === 'success') router.replace('/login?password-reset=success');
      return;
    }
    if (!error) return;
    if (errorTarget === 'password') passwordRef.current?.focus();
    else if (errorTarget === 'confirmation') confirmationRef.current?.focus();
    else outcomeRef.current?.focus();
  }, [error, errorTarget, router, terminalState]);

  async function onSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submittingRef.current || !token) return;
    setError(null);
    setErrorTarget(null);

    const form = new FormData(event.currentTarget);
    const newPassword = String(form.get('new_password') ?? '');
    const confirmation = String(form.get('confirm_password') ?? '');
    const normalizedTokenLength = token.trim().length;
    if (
      normalizedTokenLength < RECOVERY_TOKEN_MIN_LENGTH ||
      normalizedTokenLength > RECOVERY_TOKEN_MAX_LENGTH
    ) {
      formRef.current?.reset();
      setToken('');
      setTerminalState('invalid');
      router.replace('/reset-password');
      return;
    }
    if (newPassword !== confirmation) {
      setError('Passwords do not match.');
      setErrorTarget('confirmation');
      return;
    }

    submittingRef.current = true;
    setSubmitting(true);
    try {
      await apiFetch<MessageResponse>('/v1/auth/recovery/reset', {
        method: 'POST',
        body: JSON.stringify({ token, new_password: newPassword }),
      });
      formRef.current?.reset();
      setToken('');
      setError(null);
      setErrorTarget(null);
      setTerminalState('success');
    } catch (resetError) {
      if (resetError instanceof ApiError && resetError.status === 400) {
        formRef.current?.reset();
        setToken('');
        setError(null);
        setErrorTarget(null);
        setTerminalState('invalid');
        router.replace('/reset-password');
      } else if (resetError instanceof ApiError && resetError.status === 422) {
        setError('Choose a valid password that differs from your current password.');
        setErrorTarget('password');
      } else {
        setError('Unable to reset your password right now. Please try again.');
        setErrorTarget('summary');
      }
    } finally {
      submittingRef.current = false;
      setSubmitting(false);
    }
  }

  if (terminalState === 'success') {
    return (
      <p
        ref={outcomeRef}
        tabIndex={-1}
        role="status"
        data-testid="reset-password-success"
        className="mt-6 rounded-md bg-primary/10 px-3 py-2 text-sm text-primary outline-none"
      >
        Password reset successful. Redirecting to sign in.
      </p>
    );
  }

  if (terminalState === 'invalid' || !token) {
    return (
      <div className="mt-6 flex flex-col gap-4" data-testid="reset-password-missing-token">
        <p
          ref={outcomeRef}
          tabIndex={-1}
          role="alert"
          data-testid="reset-password-invalid-link"
          className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive outline-none"
        >
          {terminalState === 'invalid'
            ? 'Invalid or expired recovery link.'
            : 'This recovery link is missing or no longer available.'}
        </p>
        <Link href="/forgot-password" className="text-sm text-primary hover:underline">
          Request a new recovery link
        </Link>
      </div>
    );
  }

  return (
    <form
      ref={formRef}
      onSubmit={onSubmit}
      className="mt-8 flex flex-col gap-4"
      data-testid="reset-password-form"
    >
      <label className="flex flex-col gap-1 text-sm">
        <span className="text-muted-foreground">New password</span>
        <input
          ref={passwordRef}
          id="reset-new-password"
          name="new_password"
          type="password"
          required
          minLength={8}
          maxLength={128}
          autoComplete="new-password"
          data-testid="reset-password-input"
          aria-invalid={errorTarget === 'password'}
          aria-describedby={errorTarget === 'password' ? 'reset-password-error' : undefined}
          className="rounded-md border border-input bg-background px-3 py-2 outline-none ring-ring focus:ring-2"
        />
      </label>
      <label className="flex flex-col gap-1 text-sm">
        <span className="text-muted-foreground">Confirm new password</span>
        <input
          ref={confirmationRef}
          id="reset-confirm-password"
          name="confirm_password"
          type="password"
          required
          minLength={8}
          maxLength={128}
          autoComplete="new-password"
          data-testid="reset-password-confirmation"
          aria-invalid={errorTarget === 'confirmation'}
          aria-describedby={errorTarget === 'confirmation' ? 'reset-password-error' : undefined}
          className="rounded-md border border-input bg-background px-3 py-2 outline-none ring-ring focus:ring-2"
        />
      </label>

      {error && (
        <p
          ref={outcomeRef}
          tabIndex={-1}
          role="alert"
          id="reset-password-error"
          data-testid="reset-password-error"
          className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive outline-none"
        >
          {error}
        </p>
      )}
      <button
        type="submit"
        disabled={submitting}
        data-testid="reset-password-submit"
        className="rounded-md bg-primary px-4 py-2.5 text-sm font-medium text-primary-foreground transition disabled:opacity-60"
      >
        {submitting ? 'Resetting…' : 'Reset password'}
      </button>
    </form>
  );
}
