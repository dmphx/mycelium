import Brand from '../components/Brand';
import { useState } from 'react';

export default function Login() {
  const [error, setError] = useState<string | null>(null);
  const params = new URLSearchParams(window.location.search);
  const errFromQuery = params.get('error');
  return (
    <div className="min-h-screen bg-bg flex items-center justify-center p-6">
      <div className="w-full max-w-sm bg-card rounded-2xl border border-border p-8 shadow-2xl">
        <div className="flex items-center justify-center gap-3 mb-6">
          <Brand className="h-16 w-60" />
        </div>
        <h1 className="text-lg font-semibold text-center mb-6">Sign in</h1>
        {(error || errFromQuery) && (
          <div className="bg-red-500/10 border border-red-500/30 text-red-400 text-sm rounded p-3 mb-4">
            {error || errFromQuery}
          </div>
        )}
        <form method="post" action="/login" className="space-y-3">
          <input type="hidden" name="next" value="/" />
          <input
            type="hidden"
            name="csrf_token"
            value={document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content || ''}
          />
          <div>
            <label className="block text-[10px] uppercase tracking-wider text-muted mb-1 font-semibold">
              Username
            </label>
            <input
              type="text"
              name="username"
              required
              autoFocus
              className="w-full bg-bg border border-border rounded-lg px-4 py-2.5 text-sm
                          focus:outline-none focus:border-accent"
            />
          </div>
          <div>
            <label className="block text-[10px] uppercase tracking-wider text-muted mb-1 font-semibold">
              Password
            </label>
            <input
              type="password"
              name="password"
              required
              className="w-full bg-bg border border-border rounded-lg px-4 py-2.5 text-sm
                          focus:outline-none focus:border-accent"
            />
          </div>
          <button
            type="submit"
            className="w-full bg-accent hover:bg-accent/90 py-2.5 rounded-lg font-semibold text-sm"
          >
            Sign in
          </button>
        </form>
        <div className="text-center mt-4">
          <a href="/login/oidc" className="text-xs text-muted hover:text-white">
            Sign in with SSO
          </a>
        </div>
      </div>
    </div>
  );
}
