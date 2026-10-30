import { csrfToken } from '../api';

export type PreferenceValues = {
  library_click_jellyfin: boolean;
  discover_language_include: string;
  discover_language_exclude: string;
};

async function request(method: 'GET' | 'POST', signal: AbortSignal, values?: Partial<PreferenceValues>) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal.addEventListener('abort', abort, { once: true });
  if (signal.aborted) controller.abort();
  const timer = setTimeout(abort, 15000);
  try {
    const response = await fetch(method === 'GET' ? '/ui/api/session' : '/ui/api/me/preferences', {
      method, signal: controller.signal, credentials: 'same-origin', redirect: 'error',
      headers: method === 'GET' ? { Accept: 'application/json' }
        : { Accept: 'application/json', 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() },
      ...(method === 'POST' ? { body: JSON.stringify(values) } : {}),
    });
    if (controller.signal.aborted) throw new Error('Preference request ended');
    if (response.status === 401 && !window.location.pathname.endsWith('/login')) window.location.href = '/login';
    if (!response.ok) throw new Error('Preference request unavailable');
    const body = await response.json();
    if (controller.signal.aborted) throw new Error('Preference request ended');
    return body;
  } finally { clearTimeout(timer); signal.removeEventListener('abort', abort); }
}

export async function writePreferences(values: Partial<PreferenceValues>, signal: AbortSignal) {
  const body = await request('POST', signal, values);
  if (!body || body.ok !== true) throw new Error('Save acknowledgement unavailable');
}

export async function readSavedPreferences(actorId: number, signal: AbortSignal): Promise<PreferenceValues> {
  const body = await request('GET', signal);
  if (!body || body.authenticated !== true || body.user?.id !== actorId
    || typeof body.user.library_click_jellyfin !== 'boolean'
    || typeof body.user.discover_language_include !== 'string'
    || typeof body.user.discover_language_exclude !== 'string') throw new Error('Current preferences unavailable');
  return {
    library_click_jellyfin: body.user.library_click_jellyfin,
    discover_language_include: body.user.discover_language_include,
    discover_language_exclude: body.user.discover_language_exclude,
  };
}
