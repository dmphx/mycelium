export type NativeDispatch = Readonly<{
  state: 'ready' | 'preparing' | 'pre_effect_failed' | 'unknown' | 'accepted' | 'unavailable';
  attempt: number | null;
}>;

const unavailable: NativeDispatch = Object.freeze({ state: 'unavailable', attempt: null });
const labels = {
  ready: { label: 'Waiting for handoff', detail: 'No handoff has been recorded. Processing is not confirmed.' },
  preparing: { label: 'Preparation recorded', detail: 'A preparation attempt was recorded. Its current progress is not confirmed.' },
  pre_effect_failed: { label: 'Handoff not started', detail: 'Preparation failed before handoff. No automatic retry will be made.' },
  unknown: { label: 'Handoff unknown', detail: 'The handoff outcome could not be confirmed. No automatic retry will be made.' },
  accepted: { label: 'Handoff accepted', detail: 'This does not confirm processing, scope coverage or completion.' },
  unavailable: { label: 'Dispatch unavailable', detail: 'The handoff state is not recorded or could not be verified. Approval does not confirm processing.' },
} as const;

export function parseNativeDispatch(value: unknown): NativeDispatch {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return unavailable;
  const raw = value as Record<string, unknown>;
  if (typeof raw.state !== 'string' || !Object.prototype.hasOwnProperty.call(labels, raw.state)
    || typeof raw.can_retry !== 'boolean' || !Number.isSafeInteger(raw.attempt)
    || (raw.attempt as number) < 0 || (raw.attempt as number) > 3) return unavailable;
  const state = raw.state as NativeDispatch['state'], attempt = raw.attempt as number;
  if ((state === 'ready' && attempt !== 0)
    || (!['ready', 'unavailable'].includes(state) && attempt < 1)) return unavailable;
  const expectedRetry = state === 'ready' || (state === 'pre_effect_failed' && attempt < 3);
  if (raw.can_retry !== expectedRetry) return unavailable;
  const expectedError = state === 'pre_effect_failed' ? 'preparation_failed'
    : state === 'unknown' ? 'handoff_failed' : state === 'unavailable' ? 'identity_changed' : null;
  if (raw.error_code != null && raw.error_code !== expectedError) return unavailable;
  // can_retry is bookkeeping, never a browser permission or a mutation trigger.
  return { state, attempt };
}

export default function RequestDispatchStatus({ info }: { info: NativeDispatch }) {
  const presentation = labels[info.state];
  return <div className="min-w-0 space-y-1 [overflow-wrap:anywhere]">
    <p className="text-sm font-semibold">{presentation.label}</p>
    <p className="text-xs text-muted">{presentation.detail}</p>
    {info.attempt !== null && <p className="text-xs text-muted">Recorded attempt: {info.attempt}</p>}
  </div>;
}
