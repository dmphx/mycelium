import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import { api } from '../api';
import type { SessionInfo } from '../types';
import { readSavedPreferences, writePreferences, type PreferenceValues } from '../hooks/preferenceRequests';
import { usePlugins } from '../hooks/usePlugins';
import { useMDBListRead } from '../hooks/readMDBList';
import { useMDBListSelection } from '../hooks/useMDBListSelection';
import PluginSettingsCard from '../components/PluginSettingsCard';
import PluginMetadataStatus from '../components/PluginMetadataStatus';

export default function Settings() {
  const metadata = usePlugins();
  const { plugins } = metadata;
  const { data: session } = useQuery({ queryKey: ['session'], queryFn: api.session });
  const isAdmin = session?.user?.role === 'admin';

  const visiblePlugins = plugins.filter(p => {
    const anyFieldEnabled = (p.user_fields || []).some(f => !!(session?.user as any)?.[f]);
    return anyFieldEnabled || !!p.settings_ui;
  });

  return (
    <div className="space-y-6">
      <ChangePasswordCard />
      <PreferencesCard />
      <MDBListCard />
      {isAdmin && <NotificationsCard />}

      <div>
        <h2 className="text-xl font-bold mb-1">Plugins</h2>
        <p className="text-muted text-sm mb-3">Enable features and connect accounts for your profile.</p>
        <PluginMetadataStatus metadata={metadata} empty={visiblePlugins.length === 0} emptyMessage="No plugin settings available for your profile." />
      </div>
      {visiblePlugins.map(plugin => (
        <PluginCard key={plugin.name} plugin={plugin} session={session} />
      ))}
    </div>
  );
}

function PluginCard({ plugin, session }: {
  plugin: ReturnType<typeof usePlugins>['plugins'][number];
  session: any;
}) {
  const hasFields = plugin.user_fields?.length > 0;
  const hasUi = !!plugin.settings_ui;

  // User-field toggles: only show if the admin has already enabled at least one
  // field for this user. This keeps toggles admin-controlled  -  users can turn
  // off what they have access to, but cannot self-grant new access.
  const anyFieldEnabled = hasFields &&
    plugin.user_fields.some(f => !!(session?.user as any)?.[f]);

  if (!anyFieldEnabled && !hasUi) return null;

  return (
    <div className="bg-card rounded-lg border border-border p-6 space-y-4">
      <div>
        <h2 className="text-base font-bold leading-tight">{plugin.label}</h2>
        {plugin.description && (
          <p className="text-muted text-xs mt-0.5">{plugin.description}</p>
        )}
      </div>

      {anyFieldEnabled && <PluginUserFieldsSection plugin={plugin} />}
      {hasUi && <PluginSettingsCard plugin={plugin} embedded />}
    </div>
  );
}

function PluginUserFieldsSection({ plugin }: { plugin: ReturnType<typeof usePlugins>['plugins'][number] }) {
  const qc = useQueryClient();
  const { data: session } = useQuery({ queryKey: ['session'], queryFn: api.session });
  const mutation = useMutation({
    mutationFn: (fields: Record<string, boolean>) => api.setPluginFields(fields),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['session'] }),
  });

  return (
    <div className="flex flex-wrap gap-4">
      {plugin.user_fields.map(field => {
        const label = plugin.user_field_labels?.[field] || field;
        const value = !!(session?.user as any)?.[field];
        return (
          <label key={field} className="flex items-center gap-2 cursor-pointer select-none">
            <span className="text-sm text-muted">{label}</span>
            <button
              type="button"
              role="switch"
              aria-checked={value}
              onClick={() => mutation.mutate({ [field]: !value })}
              disabled={mutation.isPending}
              className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors
                ${value ? 'bg-accent' : 'bg-zinc-600'}
                ${mutation.isPending ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'}`}
            >
              <span className={`inline-block h-3.5 w-3.5 transform rounded-full bg-white shadow transition-transform
                ${value ? 'translate-x-4' : 'translate-x-1'}`} />
            </button>
            <span className={`text-xs font-medium ${value ? 'text-accent' : 'text-muted'}`}>
              {value ? 'On' : 'Off'}
            </span>
          </label>
        );
      })}
    </div>
  );
}

function PreferencesCard() {
  const qc = useQueryClient();
  const account = useQuery({ queryKey: ['session'], queryFn: api.session });
  const session = account.data;
  const actorId = session?.authenticated ? session.user?.id ?? null : null;
  const ready = actorId !== null;
  const clickJellyfin = !!session?.user?.library_click_jellyfin;
  const [includeLang, setIncludeLang] = useState('');
  const [excludeLang, setExcludeLang] = useState('');
  const [saving, setSaving] = useState<'library' | 'languages' | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [unknown, setUnknown] = useState(false);
  const [message, setMessage] = useState('');
  const [failed, setFailed] = useState(false);
  const dirty = useRef(false);
  const previousActor = useRef<number | null>(null);
  const currentActor = useRef(actorId);
  currentActor.current = actorId;
  const mounted = useRef(false);
  const busy = useRef(false);
  const request = useRef<AbortController | null>(null);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; request.current?.abort(); };
  }, []);
  useEffect(() => {
    if (previousActor.current !== actorId) {
      request.current?.abort(); request.current = null; busy.current = false;
      previousActor.current = actorId; dirty.current = false;
      setSaving(null); setRefreshing(false); setUnknown(false); setMessage(''); setFailed(false);
    }
    if (!dirty.current) {
      setIncludeLang(session?.user?.discover_language_include || '');
      setExcludeLang(session?.user?.discover_language_exclude || '');
    }
  }, [actorId, session?.user?.discover_language_include, session?.user?.discover_language_exclude]);

  const cache = (actor: number, values: Partial<PreferenceValues>) => qc.setQueryData<SessionInfo>(['session'], old =>
    old?.authenticated && old.user?.id === actor ? { ...old, user: { ...old.user, ...values } } : old,
  );
  // Matches the existing native preferences endpoint's comma/trim/lowercase normalization.
  const normalize = (value: string) => value.split(',').map(part => part.trim().toLowerCase()).filter(Boolean).join(',');
  const save = async (kind: 'library' | 'languages') => {
    if (!ready || busy.current || unknown || actorId === null) return;
    busy.current = true;
    const actor = actorId, controller = new AbortController(); request.current = controller;
    const current = () => mounted.current && currentActor.current === actor;
    const values: Partial<PreferenceValues> = kind === 'library' ? { library_click_jellyfin: !clickJellyfin }
      : { discover_language_include: normalize(includeLang), discover_language_exclude: normalize(excludeLang) };
    setSaving(kind); setFailed(false); setMessage('');
    try {
      await writePreferences(values, controller.signal);
      if (!current()) return;
      if (kind === 'languages') {
        dirty.current = false;
        setIncludeLang(values.discover_language_include!); setExcludeLang(values.discover_language_exclude!);
      }
      cache(actor, values); setUnknown(false);
      setMessage(kind === 'languages' ? 'Language preferences saved.' : 'Preference saved.');
    } catch {
      if (!current()) return;
      setUnknown(true); setFailed(true);
      setMessage('Could not confirm preferences were saved. Your edits are still here. Refresh saved preferences before trying again.');
    } finally {
      if (current()) { busy.current = false; request.current = null; setSaving(null); }
    }
  };
  const refresh = async () => {
    if (!ready || busy.current || actorId === null) return;
    busy.current = true;
    const actor = actorId, controller = new AbortController(); request.current = controller;
    const current = () => mounted.current && currentActor.current === actor;
    setRefreshing(true); setMessage(''); setFailed(false);
    try {
      const values = await readSavedPreferences(actor, controller.signal);
      if (!current()) return;
      cache(actor, values); setUnknown(false);
      setMessage('Saved preferences refreshed.' + (dirty.current ? ' Your language draft is unchanged.' : ''));
    } catch {
      if (!current()) return;
      setFailed(true); setMessage('Could not refresh saved preferences. Your edits are still here.');
    } finally {
      if (current()) { busy.current = false; request.current = null; setRefreshing(false); }
    }
  };
  const pending = saving !== null || refreshing;
  const edit = (value: string, setter: (value: string) => void) => {
    dirty.current = true; setter(value);
    if (!unknown) { setMessage(''); setFailed(false); }
  };

  return (
    <section aria-label="Preferences" className="min-w-0 bg-card rounded-lg border border-border p-4 sm:p-6">
      <h2 className="text-base font-bold mb-1">Preferences</h2>
      <p className="text-muted text-xs mb-4">Personalise how the app behaves for your account.</p>
      {!ready && <p role={account.isPending ? 'status' : 'alert'} className="text-sm mb-3">
        {account.isPending ? 'Loading preferences...' : 'Preferences are unavailable until your account loads.'}
      </p>}
      <div className="space-y-3">
        <button type="button" role="switch" aria-label="Open library items in Jellyfin" aria-checked={clickJellyfin}
          aria-describedby="library-preference-help" onClick={() => save('library')} disabled={!ready || pending || unknown}
          className="flex min-h-11 w-full items-start gap-3 text-left disabled:opacity-50">
          <span aria-hidden="true" className={`mt-0.5 flex h-5 w-10 shrink-0 items-center rounded-full px-0.5 ${clickJellyfin ? 'bg-accent' : 'bg-border'}`}>
            <span className={`h-4 w-4 rounded-full bg-white shadow transition-transform ${clickJellyfin ? 'translate-x-5' : 'translate-x-0'}`} />
          </span>
          <span className="min-w-0">
            <span className="block text-sm font-medium">Open library items in Jellyfin</span>
            <span id="library-preference-help" className="block text-xs text-muted mt-0.5">
              Clicking a poster in the Library tab opens the item in Jellyfin web instead of showing the detail modal.
              {!session?.jellyfin_url && <span className="text-yellow-400 ml-1">(Jellyfin URL not configured)</span>}
            </span>
          </span>
        </button>
        <form onSubmit={event => { event.preventDefault(); save('languages'); }} className="border-t border-border pt-3">
          <div className="text-sm font-medium mb-1">Discover language filter</div>
          <p className="text-xs text-muted mb-2">Comma-separated ISO 639-1 codes (e.g. <code>en,nl</code>). Only-include takes priority over exclude. Leave both empty to show everything.</p>
          <div className="flex min-w-0 flex-col gap-3 sm:flex-row sm:items-end">
            <label className="min-w-0 flex-1 text-xs text-muted">Only show languages
              <input type="text" value={includeLang} onChange={event => edit(event.target.value, setIncludeLang)}
                disabled={!ready || pending} placeholder="Only show (e.g. en,nl)"
                className="mt-1 min-h-11 min-w-0 w-full bg-bg border border-border rounded-lg px-3 py-2 text-base placeholder:text-muted focus:outline-none focus:border-accent" />
            </label>
            <label className="min-w-0 flex-1 text-xs text-muted">Hide languages
              <input type="text" value={excludeLang} onChange={event => edit(event.target.value, setExcludeLang)}
                disabled={!ready || pending} placeholder="Hide (e.g. ru,hi)"
                className="mt-1 min-h-11 min-w-0 w-full bg-bg border border-border rounded-lg px-3 py-2 text-base placeholder:text-muted focus:outline-none focus:border-accent" />
            </label>
            <button type="submit" disabled={!ready || pending || unknown}
              className="min-h-11 shrink-0 rounded bg-accent px-3 py-2 text-sm font-semibold disabled:opacity-50">Save language preferences</button>
          </div>
        </form>
        {pending && <p role="status" className="text-sm text-muted">{refreshing ? 'Refreshing saved preferences...' : 'Saving preference...'}</p>}
        {message && <p role={failed ? 'alert' : 'status'} className={`text-sm ${failed ? 'text-amber-300' : 'text-muted'}`}>{message}</p>}
        <button type="button" disabled={!ready || pending} onClick={refresh}
          className="min-h-11 rounded-lg border border-border px-3 py-2 text-sm disabled:opacity-50">Refresh saved preferences</button>
      </div>
    </section>
  );
}


function NotificationsCard() {
  const qc = useQueryClient();
  const { data } = useQuery({ queryKey: ['settings'], queryFn: api.settings });
  const group = data?.groups.find((g) => g.id === 'notifications');
  const valueOf = (key: string) => group?.items.find((i) => i.key === key)?.value;

  const [notifySuccess, setNotifySuccess] = useState(true);
  const [notifyFailure, setNotifyFailure] = useState(true);
  const [discordUrl, setDiscordUrl] = useState('');
  const [telegramToken, setTelegramToken] = useState('');
  const [telegramChat, setTelegramChat] = useState('');
  const [msg, setMsg] = useState('');

  useEffect(() => {
    if (!group) return;
    setNotifySuccess(!!valueOf('NOTIFY_ON_SUCCESS'));
    setNotifyFailure(!!valueOf('NOTIFY_ON_FAILURE'));
    setDiscordUrl(valueOf('DISCORD_WEBHOOK_URL') || '');
    setTelegramToken(valueOf('TELEGRAM_BOT_TOKEN') || '');
    setTelegramChat(valueOf('TELEGRAM_CHAT_ID') || '');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data]);

  const saveMutation = useMutation({
    mutationFn: () => api.setNotificationSettings({
      NOTIFY_ON_SUCCESS: notifySuccess,
      NOTIFY_ON_FAILURE: notifyFailure,
      DISCORD_WEBHOOK_URL: discordUrl,
      TELEGRAM_BOT_TOKEN: telegramToken,
      TELEGRAM_CHAT_ID: telegramChat,
    }),
    onSuccess: () => { setMsg('Saved.'); qc.invalidateQueries({ queryKey: ['settings'] }); },
    onError: (e: any) => setMsg(`Error: ${e.message}`),
  });

  return (
    <div className="bg-card rounded-lg border border-border p-6">
      <h2 className="text-base font-bold mb-1">Notifications</h2>
      <p className="text-muted text-xs mb-4">
        Discord and/or Telegram alerts when a request succeeds or fails. Admin-only.
      </p>
      <div className="space-y-3 max-w-lg">
        <div className="flex flex-wrap gap-4">
          <label className="flex items-center gap-2 text-sm cursor-pointer">
            <input type="checkbox" checked={notifySuccess} onChange={(e) => setNotifySuccess(e.target.checked)} />
            Notify on success
          </label>
          <label className="flex items-center gap-2 text-sm cursor-pointer">
            <input type="checkbox" checked={notifyFailure} onChange={(e) => setNotifyFailure(e.target.checked)} />
            Notify on failure
          </label>
        </div>
        <div>
          <label htmlFor="notification-discordUrl" className="block text-xs text-muted mb-1">Discord webhook URL</label>
          <input
            id="notification-discordUrl"
            type="text"
            value={discordUrl}
            onChange={(e) => setDiscordUrl(e.target.value)}
            placeholder="https://discord.com/api/webhooks/..."
            className="w-full bg-bg border border-border rounded-lg px-3 py-2 text-sm
                       placeholder:text-muted focus:outline-none focus:border-accent"
          />
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
          <div>
            <label htmlFor="notification-telegramToken" className="block text-xs text-muted mb-1">Telegram bot token</label>
            <input
              id="notification-telegramToken"
              type="text"
              value={telegramToken}
              onChange={(e) => setTelegramToken(e.target.value)}
              className="w-full bg-bg border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-accent"
            />
          </div>
          <div>
            <label htmlFor="notification-telegramChat" className="block text-xs text-muted mb-1">Telegram chat id</label>
            <input
              id="notification-telegramChat"
              type="text"
              value={telegramChat}
              onChange={(e) => setTelegramChat(e.target.value)}
              className="w-full bg-bg border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-accent"
            />
          </div>
        </div>
        <button
          onClick={() => saveMutation.mutate()}
          disabled={saveMutation.isPending}
          className="px-4 py-2 rounded-lg bg-accent hover:bg-accent/90 disabled:opacity-60 font-semibold text-sm"
        >
          {saveMutation.isPending ? 'Saving...' : 'Save'}
        </button>
        {msg && <p className="text-xs text-muted">{msg}</p>}
      </div>
    </div>
  );
}


function MDBListCard() {
  const qc = useQueryClient();
  const read = useMDBListRead();
  const { data: status } = read.statusRead;
  const { data: listsData } = read.listsRead;
  const [apiKey, setApiKey] = useState('');
  const selection = useMDBListSelection(read);
  const { selected } = selection;
  const [msg, setMsg] = useState('');

  const connectMutation = useMutation({
    mutationFn: () => api.mdblistConnect(apiKey),
    onSuccess: () => { setApiKey(''); qc.invalidateQueries({ queryKey: ['mdblist-status'] }); },
    onError: (e: any) => setMsg(`Error: ${e.message}`),
  });
  const disconnectMutation = useMutation({
    mutationFn: api.mdblistDisconnect,
    onSuccess: () => qc.invalidateQueries({ queryKey: ['mdblist-status'] }),
  });
  const syncMutation = useMutation({
    mutationFn: api.mdblistSync,
    onSuccess: (data) => setMsg(`Queued ${data.added} new item${data.added === 1 ? '' : 's'}.`),
    onError: (e: any) => setMsg(`Error: ${e.message}`),
  });

  return (
    <div className="bg-card rounded-lg border border-border p-6">
      <h2 className="text-base font-bold mb-1">MDBList</h2>
      <p className="text-muted text-xs mb-4">
        Connect your MDBList API key (from{' '}
        <a href="https://mdblist.com/preferences" target="_blank" rel="noreferrer" className="text-accent underline">
          mdblist.com/preferences
        </a>) to sync your lists and auto-request new items.
      </p>

      <div className="mb-4 space-y-2">
        {!read.ready && <p role={read.account.isError ? 'alert' : 'status'} className="text-sm text-muted">{read.account.isPending ? 'Loading account for MDBList...' : 'MDBList account is unavailable.'}</p>}
        {read.ready && read.statusRead.isFetching && <p role="status" className="text-sm text-muted">{status ? 'Refreshing' : 'Loading'} MDBList connection status...</p>}
        {read.ready && read.statusRead.isError && <p role="alert" className="text-sm text-red-400">Could not load MDBList connection status. {status ? 'Previously loaded connection details are shown and may be out of date.' : 'Connection state is unknown.'} Refresh to try again.</p>}
        {read.ready && status?.connected && read.listsRead.isFetching && <p role="status" className="text-sm text-muted">{listsData ? 'Refreshing' : 'Loading'} MDBList lists...</p>}
        {read.ready && status?.connected && read.listsRead.isError && <p role="alert" className="text-sm text-red-400">Could not load MDBList lists. {listsData ? 'Previously loaded lists are shown and may be out of date.' : 'Lists are unavailable, not confirmed empty.'} Refresh to try again.</p>}
        {read.refreshed && read.statusKnown && (!status?.connected || read.listsKnown) && <p role="status" className="text-sm text-muted">MDBList refreshed.</p>}
        <button type="button" onClick={selection.refresh} disabled={!read.ready || selection.pending || read.statusRead.isFetching || read.listsRead.isFetching}
          className="min-h-[44px] rounded border border-border px-3 py-2 text-sm text-muted hover:text-white disabled:opacity-50">Refresh MDBList</button>
      </div>
      {selection.saving && <p role="status" className="text-sm text-muted mb-3">Saving list selection...</p>}
      {selection.refreshing && <p role="status" className="text-sm text-muted mb-3">Refreshing saved list selection...</p>}
      {selection.message && <p role={selection.failed ? 'alert' : 'status'} className={`text-sm mb-3 ${selection.failed ? 'text-amber-300' : 'text-muted'}`}>{selection.message}</p>}
      {read.ready && status?.connected ? (
        <div className="space-y-3">
          {listsData?.lists && listsData.lists.length > 0 && (
            <div className="space-y-1">
              <p className="text-xs text-muted mb-1">Lists to sync:</p>
              {listsData.lists.map((l) => (
                <label key={l.id} className="flex min-h-11 items-center gap-2 text-sm cursor-pointer">
                  <input
                    type="checkbox"
                    disabled={selection.blocked}
                    checked={selected.includes(String(l.id))}
                    onChange={() => selection.toggle(String(l.id))}
                  />
                  {l.name}
                </label>
              ))}
            </div>
          )}
          {read.listsKnown && listsData?.lists.length === 0 && <p className="text-sm text-muted">No lists returned. Refresh to check again.</p>}
          <div className="flex flex-wrap gap-2">
            <button
              onClick={() => syncMutation.mutate()}
              disabled={syncMutation.isPending || selection.blocked || !listsData?.lists.length}
              className="px-3 py-1.5 rounded bg-accent text-sm font-semibold disabled:opacity-50"
            >
              {syncMutation.isPending ? 'Syncing...' : 'Sync now'}
            </button>
            <button
              onClick={() => disconnectMutation.mutate()}
              disabled={disconnectMutation.isPending || !read.statusKnown || selection.pending || selection.unknown}
              className="px-3 py-1.5 rounded border border-danger text-danger text-sm font-medium disabled:opacity-50"
            >
              Disconnect
            </button>
          </div>
          {msg && <p className="text-xs text-muted">{msg}</p>}
        </div>
      ) : read.statusKnown && status?.connected === false ? (
        <div className="space-y-2">
        <p className="text-sm text-muted">Not connected.</p>
        <div className="flex flex-col sm:flex-row sm:items-end gap-2">
          <div className="min-w-0 flex-1">
          <label htmlFor="mdblist-api-key" className="block text-xs text-muted mb-1">MDBList API key</label>
          <input
            id="mdblist-api-key"
            type="text"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder="MDBList API key"
            className="w-full min-w-0 bg-bg border border-border rounded-lg px-3 py-2 text-sm
                       placeholder:text-muted focus:outline-none focus:border-accent"
          />
          </div>
          <button
            onClick={() => connectMutation.mutate()}
            disabled={connectMutation.isPending || !apiKey}
            className="px-4 py-2 rounded-lg bg-accent hover:bg-accent/90 disabled:opacity-60 font-semibold text-sm whitespace-nowrap"
          >
            {connectMutation.isPending ? 'Connecting...' : 'Connect'}
          </button>
        </div>
        </div>
      ) : null}
    </div>
  );
}


function ChangePasswordCard() {
  const [current, setCurrent] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [error, setError] = useState('');
  const [success, setSuccess] = useState(false);
  const [errorField, setErrorField] = useState<'password' | 'confirm' | null>(null);
  const passwordInput = useRef<HTMLInputElement>(null);
  const confirmInput = useRef<HTMLInputElement>(null);
  const edit = () => { setError(''); setErrorField(null); setSuccess(false); };

  const mutation = useMutation({
    mutationFn: () => api.changePassword(current, password),
    onSuccess: () => {
      setSuccess(true);
      setCurrent(''); setPassword(''); setConfirm('');
      setTimeout(() => setSuccess(false), 3000);
    },
    onError: (e: any) => setError(e.message || 'Failed to change password'),
  });

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    setError(''); setErrorField(null);
    if (password.length < 6) { setError('At least 6 characters required'); setErrorField('password'); passwordInput.current?.focus(); return; }
    if (password !== confirm) { setError('Passwords do not match'); setErrorField('confirm'); confirmInput.current?.focus(); return; }
    mutation.mutate();
  };

  return (
    <div className="bg-card rounded-lg border border-border p-6">
      <h2 className="text-base font-bold mb-4">Change password</h2>
      {success && <p role="status" className="text-ok text-sm mb-3">Password changed successfully.</p>}
      <form aria-label="Change password" onSubmit={submit} className="space-y-3 max-w-sm">
        <div>
          <label htmlFor="settings-password-current" className="block text-xs text-muted mb-1">Current password</label>
          <input id="settings-password-current" type="password" value={current} onChange={e => { setCurrent(e.target.value); edit(); }}
            className="w-full bg-bg border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-accent" />
        </div>
        <div>
          <label htmlFor="settings-password-password" className="block text-xs text-muted mb-1">New password</label>
          <input id="settings-password-password" ref={passwordInput} aria-invalid={errorField === 'password'} aria-describedby={errorField === 'password' ? 'password-form-error' : undefined} aria-errormessage={errorField === 'password' ? 'password-form-error' : undefined} type="password" value={password} onChange={e => { setPassword(e.target.value); edit(); }}
            className="w-full bg-bg border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-accent" />
        </div>
        <div>
          <label htmlFor="settings-password-confirm" className="block text-xs text-muted mb-1">Confirm new password</label>
          <input id="settings-password-confirm" ref={confirmInput} aria-invalid={errorField === 'confirm'} aria-describedby={errorField === 'confirm' ? 'password-form-error' : undefined} aria-errormessage={errorField === 'confirm' ? 'password-form-error' : undefined} type="password" value={confirm} onChange={e => { setConfirm(e.target.value); edit(); }}
            className="w-full bg-bg border border-border rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-accent" />
        </div>
        {error && <p id="password-form-error" role="alert" className="text-danger text-xs">{error}</p>}
        <button type="submit" disabled={mutation.isPending}
          className="px-4 py-2 rounded-lg bg-accent hover:bg-accent/90 disabled:opacity-60 font-semibold text-sm">
          {mutation.isPending ? 'Saving...' : 'Change password'}
        </button>
      </form>
    </div>
  );
}
