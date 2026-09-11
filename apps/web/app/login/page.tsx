import Link from 'next/link';
import { AuthForm } from '@/components/auth-form';

export default async function LoginPage({
  searchParams,
}: {
  searchParams?: Promise<{ 'password-reset'?: string; returnTo?: string | string[] }>;
}) {
  const resolvedSearchParams = await searchParams;
  const passwordReset = resolvedSearchParams?.['password-reset'] === 'success';
  const returnTo =
    typeof resolvedSearchParams?.returnTo === 'string' ? resolvedSearchParams.returnTo : null;
  return (
    <main
      className="mx-auto flex min-h-screen max-w-md flex-col justify-center px-6 py-12"
      data-testid="login-page"
    >
      <h1 className="font-display text-3xl">Welcome back</h1>
      <p className="mt-2 text-sm text-muted-foreground">Sign in to your Agrovix AgOS account.</p>
      {passwordReset && (
        <p
          role="status"
          data-testid="password-reset-success"
          className="mt-4 rounded-md bg-primary/10 px-3 py-2 text-sm text-primary"
        >
          Password reset successful. Sign in with your new password.
        </p>
      )}
      <AuthForm mode="login" returnTo={returnTo} />
      <Link
        href="/forgot-password"
        data-testid="login-forgot-password-link"
        className="mt-4 self-start text-sm text-primary hover:underline"
      >
        Forgot password?
      </Link>
      <p className="mt-6 text-sm text-muted-foreground">
        New to Agrovix?{' '}
        <Link
          href="/register"
          data-testid="login-to-register-link"
          className="text-primary hover:underline"
        >
          Create an account
        </Link>
      </p>
    </main>
  );
}
