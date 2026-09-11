import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

const { replaceMock } = vi.hoisted(() => ({ replaceMock: vi.fn() }));

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn(), replace: replaceMock }),
}));

vi.mock('next/link', () => ({
  default: ({ children, href, ...rest }: any) => (
    <a href={typeof href === 'string' ? href : '#'} {...rest}>
      {children}
    </a>
  ),
}));

vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api');
  return { ...actual, apiFetch: vi.fn() };
});

import { ApiError, apiFetch } from '@/lib/api';
import LoginPage from '@/app/login/page';
import { ForgotPasswordForm, ResetPasswordForm } from '@/components/password-recovery-forms';

const mockedApiFetch = apiFetch as unknown as ReturnType<typeof vi.fn>;
const VALID_RECOVERY_TOKEN = 'opaque-secret-token-with-at-least-32-characters';

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe('web password recovery', () => {
  beforeEach(() => {
    mockedApiFetch.mockReset();
    replaceMock.mockReset();
  });

  it('links the login page to public password recovery', async () => {
    render(await LoginPage({ searchParams: Promise.resolve({}) }));
    expect(screen.getByTestId('login-forgot-password-link')).toHaveAttribute(
      'href',
      '/forgot-password',
    );
  });

  it('confirms reset completion on the token-free login route', async () => {
    render(await LoginPage({ searchParams: Promise.resolve({ 'password-reset': 'success' }) }));
    expect(screen.getByTestId('password-reset-success')).toHaveTextContent(
      'Password reset successful. Sign in with your new password.',
    );
    expect(document.body.textContent).not.toContain('token=');
  });

  it('submits a recovery request and shows only the generic accepted message', async () => {
    mockedApiFetch.mockResolvedValue({ message: 'server copy is deliberately ignored' });
    render(<ForgotPasswordForm />);
    fireEvent.change(screen.getByTestId('recovery-email-input'), {
      target: { value: 'person@example.com' },
    });
    fireEvent.submit(screen.getByTestId('forgot-password-form'));

    await waitFor(() => expect(screen.getByTestId('recovery-request-success')).toBeVisible());
    expect(screen.getByTestId('recovery-request-success')).toHaveFocus();
    expect(screen.getByTestId('recovery-request-success')).toHaveTextContent(
      'If an eligible account exists, password recovery instructions will be sent.',
    );
    expect(mockedApiFetch).toHaveBeenCalledWith('/v1/auth/recovery/request', {
      method: 'POST',
      body: JSON.stringify({ email: 'person@example.com' }),
    });
  });

  it('renders a neutral rate-limit error', async () => {
    mockedApiFetch.mockRejectedValue(new ApiError(429, { detail: 'provider-specific detail' }));
    render(<ForgotPasswordForm />);
    fireEvent.change(screen.getByTestId('recovery-email-input'), {
      target: { value: 'person@example.com' },
    });
    fireEvent.submit(screen.getByTestId('forgot-password-form'));
    await waitFor(() =>
      expect(screen.getByTestId('recovery-request-error')).toHaveTextContent(
        'Too many recovery requests. Please try again later.',
      ),
    );
    expect(screen.getByTestId('recovery-request-error')).toHaveFocus();
    expect(screen.getByTestId('recovery-email-input')).toHaveAttribute('aria-invalid', 'false');
  });

  it('focuses and associates the email field for request validation errors', async () => {
    mockedApiFetch.mockRejectedValue(new ApiError(422, { detail: 'hostile validation detail' }));
    render(<ForgotPasswordForm />);
    fireEvent.change(screen.getByTestId('recovery-email-input'), {
      target: { value: 'invalid@example.com' },
    });
    fireEvent.submit(screen.getByTestId('forgot-password-form'));
    await waitFor(() => expect(screen.getByTestId('recovery-email-input')).toHaveFocus());
    expect(screen.getByTestId('recovery-email-input')).toHaveAttribute(
      'aria-describedby',
      'recovery-request-error',
    );
    expect(screen.getByTestId('recovery-email-input')).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByTestId('recovery-request-error')).toHaveTextContent(
      'Enter a valid email address.',
    );
    expect(document.body.textContent).not.toContain('hostile validation detail');
  });

  it('allows one forgot-password request per pending window and unlocks after success', async () => {
    const first = deferred<{ message: string }>();
    mockedApiFetch.mockReturnValueOnce(first.promise).mockResolvedValueOnce({ message: 'ok' });
    render(<ForgotPasswordForm />);
    fireEvent.change(screen.getByTestId('recovery-email-input'), {
      target: { value: 'person@example.com' },
    });
    const form = screen.getByTestId('forgot-password-form');
    const button = screen.getByTestId('recovery-request-submit');

    fireEvent.submit(form);
    fireEvent.submit(form);
    fireEvent.click(button);
    expect(mockedApiFetch).toHaveBeenCalledTimes(1);

    first.resolve({ message: 'ok' });
    await waitFor(() => expect(button).not.toBeDisabled());
    fireEvent.click(button);
    await waitFor(() => expect(mockedApiFetch).toHaveBeenCalledTimes(2));
  });

  it('unlocks forgot-password after failure and still prevents same-tick Enter/button duplication', async () => {
    const first = deferred<{ message: string }>();
    mockedApiFetch.mockReturnValueOnce(first.promise).mockResolvedValueOnce({ message: 'ok' });
    render(<ForgotPasswordForm />);
    fireEvent.change(screen.getByTestId('recovery-email-input'), {
      target: { value: 'person@example.com' },
    });
    const form = screen.getByTestId('forgot-password-form');
    const button = screen.getByTestId('recovery-request-submit');
    fireEvent.submit(form);
    fireEvent.click(button);
    expect(mockedApiFetch).toHaveBeenCalledTimes(1);
    first.reject(new Error('offline'));
    await waitFor(() => expect(button).not.toBeDisabled());
    fireEvent.submit(form);
    await waitFor(() => expect(mockedApiFetch).toHaveBeenCalledTimes(2));
  });

  it('does not submit without a recovery token', () => {
    render(<ResetPasswordForm initialToken={null} />);
    expect(screen.getByTestId('reset-password-missing-token')).toBeVisible();
    expect(mockedApiFetch).not.toHaveBeenCalled();
  });

  it('blocks mismatched passwords before calling the API', () => {
    render(<ResetPasswordForm initialToken={VALID_RECOVERY_TOKEN} />);
    fireEvent.change(screen.getByTestId('reset-password-input'), {
      target: { value: 'new-password-one' },
    });
    fireEvent.change(screen.getByTestId('reset-password-confirmation'), {
      target: { value: 'new-password-two' },
    });
    fireEvent.submit(screen.getByTestId('reset-password-form'));
    expect(screen.getByTestId('reset-password-error')).toHaveTextContent('Passwords do not match.');
    expect(screen.getByTestId('reset-password-confirmation')).toHaveFocus();
    expect(screen.getByTestId('reset-password-confirmation')).toHaveAttribute(
      'aria-describedby',
      'reset-password-error',
    );
    expect(screen.getByTestId('reset-password-confirmation')).toHaveAttribute(
      'aria-invalid',
      'true',
    );
    expect(mockedApiFetch).not.toHaveBeenCalled();
  });

  it('replaces the token URL and clears sensitive form state after a successful reset', async () => {
    mockedApiFetch.mockResolvedValue({ message: 'ok' });
    render(<ResetPasswordForm initialToken={VALID_RECOVERY_TOKEN} />);
    const password = screen.getByTestId('reset-password-input') as HTMLInputElement;
    const confirmation = screen.getByTestId('reset-password-confirmation') as HTMLInputElement;
    fireEvent.change(password, { target: { value: 'new-password-one' } });
    fireEvent.change(confirmation, { target: { value: 'new-password-one' } });
    fireEvent.submit(screen.getByTestId('reset-password-form'));

    await waitFor(() => expect(replaceMock).toHaveBeenCalledWith('/login?password-reset=success'));
    expect(mockedApiFetch).toHaveBeenCalledWith('/v1/auth/recovery/reset', {
      method: 'POST',
      body: JSON.stringify({ token: VALID_RECOVERY_TOKEN, new_password: 'new-password-one' }),
    });
    expect(password.value).toBe('');
    expect(confirmation.value).toBe('');
    expect(screen.getByTestId('reset-password-success')).toHaveFocus();
    expect(document.body.textContent).not.toContain(VALID_RECOVERY_TOKEN);
  });

  it('maps invalid tokens without rendering the token', async () => {
    mockedApiFetch.mockRejectedValue(new ApiError(400, { detail: 'secret backend reason' }));
    render(<ResetPasswordForm initialToken={VALID_RECOVERY_TOKEN} />);
    fireEvent.change(screen.getByTestId('reset-password-input'), {
      target: { value: 'new-password-one' },
    });
    fireEvent.change(screen.getByTestId('reset-password-confirmation'), {
      target: { value: 'new-password-one' },
    });
    fireEvent.submit(screen.getByTestId('reset-password-form'));
    await waitFor(() =>
      expect(screen.getByTestId('reset-password-invalid-link')).toHaveTextContent(
        'Invalid or expired recovery link.',
      ),
    );
    expect(screen.getByTestId('reset-password-invalid-link')).toHaveFocus();
    expect(replaceMock).toHaveBeenCalledWith('/reset-password');
    expect(document.body.textContent).not.toContain(VALID_RECOVERY_TOKEN);
  });

  it('clears terminal invalid-link credentials and cannot reuse the invalid token', async () => {
    mockedApiFetch.mockRejectedValue(new ApiError(400, { detail: 'hostile token detail' }));
    render(<ResetPasswordForm initialToken={VALID_RECOVERY_TOKEN} />);
    const password = screen.getByTestId('reset-password-input') as HTMLInputElement;
    const confirmation = screen.getByTestId('reset-password-confirmation') as HTMLInputElement;
    const form = screen.getByTestId('reset-password-form');
    fireEvent.change(password, { target: { value: 'new-password-one' } });
    fireEvent.change(confirmation, { target: { value: 'new-password-one' } });
    fireEvent.submit(form);
    await waitFor(() => expect(screen.getByTestId('reset-password-invalid-link')).toBeVisible());

    expect(password.value).toBe('');
    expect(confirmation.value).toBe('');
    expect(replaceMock).toHaveBeenCalledWith('/reset-password');
    expect(document.body.textContent).not.toContain(VALID_RECOVERY_TOKEN);
    expect(document.body.textContent).not.toContain('hostile token detail');
    fireEvent.submit(form);
    expect(mockedApiFetch).toHaveBeenCalledTimes(1);
  });

  it.each(['short-token', 'x'.repeat(257)])(
    'terminates malformed token recovery without submitting or retaining credentials',
    async (malformedToken) => {
      const hostile = 'SQLSTATE 23505 token=internal-secret stack /srv/app.py:99';
      mockedApiFetch.mockRejectedValue(new ApiError(422, { detail: hostile }));
      render(<ResetPasswordForm initialToken={malformedToken} />);
      const password = screen.getByTestId('reset-password-input') as HTMLInputElement;
      const confirmation = screen.getByTestId('reset-password-confirmation') as HTMLInputElement;
      const form = screen.getByTestId('reset-password-form');
      fireEvent.change(password, { target: { value: 'new-password-one' } });
      fireEvent.change(confirmation, { target: { value: 'new-password-one' } });
      fireEvent.submit(form);

      await waitFor(() => expect(screen.getByTestId('reset-password-invalid-link')).toBeVisible());
      expect(screen.getByTestId('reset-password-invalid-link')).toHaveTextContent(
        'Invalid or expired recovery link.',
      );
      expect(password.value).toBe('');
      expect(confirmation.value).toBe('');
      expect(replaceMock).toHaveBeenCalledWith('/reset-password');
      expect(document.body.textContent).not.toContain(malformedToken);
      expect(document.body.textContent).not.toContain(hostile);
      expect(mockedApiFetch).not.toHaveBeenCalled();
      fireEvent.submit(form);
      expect(mockedApiFetch).not.toHaveBeenCalled();
    },
  );

  it.each([32, 256])(
    'allows a %i-character token through to the backend authority',
    async (tokenLength) => {
      const boundaryToken = 'x'.repeat(tokenLength);
      mockedApiFetch.mockRejectedValue(
        new ApiError(503, { detail: 'backend remains authoritative' }),
      );
      render(<ResetPasswordForm initialToken={boundaryToken} />);
      fireEvent.change(screen.getByTestId('reset-password-input'), {
        target: { value: 'new-password-one' },
      });
      fireEvent.change(screen.getByTestId('reset-password-confirmation'), {
        target: { value: 'new-password-one' },
      });
      fireEvent.submit(screen.getByTestId('reset-password-form'));

      await waitFor(() =>
        expect(mockedApiFetch).toHaveBeenCalledWith('/v1/auth/recovery/reset', {
          method: 'POST',
          body: JSON.stringify({ token: boundaryToken, new_password: 'new-password-one' }),
        }),
      );
      expect(screen.queryByTestId('reset-password-invalid-link')).not.toBeInTheDocument();
    },
  );

  it('uses bounded 422 copy, rejects hostile detail, associates the field, and focuses it', async () => {
    const hostile = 'SQLSTATE 23505 token=opaque-secret-token stack /srv/app.py:99';
    mockedApiFetch.mockRejectedValue(new ApiError(422, { detail: hostile }));
    render(<ResetPasswordForm initialToken={VALID_RECOVERY_TOKEN} />);
    fireEvent.change(screen.getByTestId('reset-password-input'), {
      target: { value: 'new-password-one' },
    });
    fireEvent.change(screen.getByTestId('reset-password-confirmation'), {
      target: { value: 'new-password-one' },
    });
    fireEvent.submit(screen.getByTestId('reset-password-form'));

    await waitFor(() => expect(screen.getByTestId('reset-password-input')).toHaveFocus());
    expect(screen.getByTestId('reset-password-error')).toHaveTextContent(
      'Choose a valid password that differs from your current password.',
    );
    expect(screen.getByTestId('reset-password-input')).toHaveAttribute(
      'aria-describedby',
      'reset-password-error',
    );
    expect(screen.getByTestId('reset-password-input')).toHaveAttribute('aria-invalid', 'true');
    expect(document.body.textContent).not.toContain(hostile);
    expect(document.body.textContent).not.toContain(VALID_RECOVERY_TOKEN);
  });

  it('focuses bounded reset server errors and unlocks for retry', async () => {
    mockedApiFetch
      .mockRejectedValueOnce(new ApiError(503, { detail: 'internal stack' }))
      .mockResolvedValueOnce({ message: 'ok' });
    render(<ResetPasswordForm initialToken={VALID_RECOVERY_TOKEN} />);
    fireEvent.change(screen.getByTestId('reset-password-input'), {
      target: { value: 'new-password-one' },
    });
    fireEvent.change(screen.getByTestId('reset-password-confirmation'), {
      target: { value: 'new-password-one' },
    });
    const form = screen.getByTestId('reset-password-form');
    const button = screen.getByTestId('reset-password-submit');
    fireEvent.submit(form);
    await waitFor(() => expect(screen.getByTestId('reset-password-error')).toHaveFocus());
    expect(document.body.textContent).not.toContain('internal stack');
    expect(button).not.toBeDisabled();
    fireEvent.submit(form);
    await waitFor(() => expect(mockedApiFetch).toHaveBeenCalledTimes(2));
  });

  it('allows one reset request per pending window across submit and button activation', async () => {
    const first = deferred<{ message: string }>();
    mockedApiFetch.mockReturnValueOnce(first.promise);
    render(<ResetPasswordForm initialToken={VALID_RECOVERY_TOKEN} />);
    fireEvent.change(screen.getByTestId('reset-password-input'), {
      target: { value: 'new-password-one' },
    });
    fireEvent.change(screen.getByTestId('reset-password-confirmation'), {
      target: { value: 'new-password-one' },
    });
    const form = screen.getByTestId('reset-password-form');
    const button = screen.getByTestId('reset-password-submit');
    fireEvent.submit(form);
    fireEvent.submit(form);
    fireEvent.click(button);
    fireEvent.click(button);
    expect(mockedApiFetch).toHaveBeenCalledTimes(1);
    first.resolve({ message: 'ok' });
    await waitFor(() => expect(replaceMock).toHaveBeenCalledWith('/login?password-reset=success'));
    expect(mockedApiFetch).toHaveBeenCalledTimes(1);
  });
});
