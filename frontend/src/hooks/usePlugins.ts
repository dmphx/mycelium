import { useQuery } from '@tanstack/react-query'
import { useCallback, useEffect, useRef, useState } from 'react'

interface ConfigGate {
  field:      string
  message:    string
  link?:      string
  link_label?: string
}

interface OAuthDeviceSpec {
  connected_field: string
  username_field?: string
  synced_field?:   string
  start_url:  string
  poll_url:   string
  revoke_url: string
}

interface ActionSpec {
  label:            string
  url:              string
  method?:          string
  show_if?:         string
  success_key?:     string
  success_template?: string
}

export interface PluginSettingsUi {
  status_url:   string
  config_gate?: ConfigGate
  oauth_device?: OAuthDeviceSpec
  actions?:     ActionSpec[]
}

export interface PluginMeta {
  name:              string
  label:             string
  version:           string
  description:       string
  user_fields:       string[]
  user_field_labels: Record<string, string>
  settings_ui?:      PluginSettingsUi
}

// These checks protect descriptor rendering; server-side field/role policy remains authoritative.
const record = (value: any): value is Record<string, any> => !!value && typeof value === 'object' && !Array.isArray(value)
const optionalStrings = (value: Record<string, any>, fields: string[]) =>
  fields.every(field => value[field] == null || typeof value[field] === 'string')
function validPlugin(plugin: any): boolean {
  if (!record(plugin) || typeof plugin.name !== 'string' || typeof plugin.label !== 'string'
    || !optionalStrings(plugin, ['version', 'description'])
    || (plugin.user_fields != null && (!Array.isArray(plugin.user_fields) || !plugin.user_fields.every((field: unknown) => typeof field === 'string')))
    || (plugin.user_field_labels != null && (!record(plugin.user_field_labels) || !Object.values(plugin.user_field_labels).every(label => typeof label === 'string')))) return false
  const ui = plugin.settings_ui
  if (ui == null) return true
  if (!record(ui) || typeof ui.status_url !== 'string') return false
  if (ui.config_gate != null && (!record(ui.config_gate) || typeof ui.config_gate.field !== 'string'
    || typeof ui.config_gate.message !== 'string' || !optionalStrings(ui.config_gate, ['link', 'link_label']))) return false
  if (ui.oauth_device != null && (!record(ui.oauth_device)
    || !['connected_field', 'start_url', 'poll_url', 'revoke_url'].every(field => typeof ui.oauth_device[field] === 'string')
    || !optionalStrings(ui.oauth_device, ['username_field', 'synced_field']))) return false
  return ui.actions == null || (Array.isArray(ui.actions) && ui.actions.every((action: any) => record(action)
    && typeof action.label === 'string' && typeof action.url === 'string'
    && optionalStrings(action, ['method', 'show_if', 'success_key', 'success_template'])))
}

async function readPlugins(signal: AbortSignal): Promise<{ plugins: PluginMeta[] }> {
  const controller = new AbortController()
  const abort = () => controller.abort()
  signal.addEventListener('abort', abort, { once: true })
  if (signal.aborted) controller.abort()
  const timer = setTimeout(abort, 15000)
  try {
    const response = await fetch('/ui/api/plugins', {
      signal: controller.signal, credentials: 'same-origin', redirect: 'error', headers: { Accept: 'application/json' },
    })
    if (controller.signal.aborted) throw new Error('Plugin metadata read ended')
    // Preserve this endpoint's existing failure behavior, including401: no auth action here.
    if (!response.ok) throw new Error('Plugin metadata unavailable')
    const body = await response.json()
    if (controller.signal.aborted) throw new Error('Plugin metadata read ended')
    if (!record(body) || !Array.isArray(body.plugins) || !body.plugins.every(validPlugin))
      throw new Error('Invalid plugin metadata')
    return body as { plugins: PluginMeta[] }
  } finally { clearTimeout(timer); signal.removeEventListener('abort', abort) }
}

function usePlugins() {
  const query = useQuery<{ plugins: PluginMeta[] }>({
    queryKey: ['plugins'], queryFn: ({ signal }) => readPlugins(signal),
    staleTime: Infinity, retry: false,
  })
  const busy = useRef(false), mounted = useRef(false)
  const [refreshed, setRefreshed] = useState(false)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const refresh = useCallback(async () => {
    if (busy.current || query.isFetching) return
    busy.current = true; setRefreshed(false)
    try {
      const result = await query.refetch()
      if (mounted.current) setRefreshed(result.isSuccess)
    } finally { busy.current = false }
  }, [query.isFetching, query.refetch])
  return {
    plugins: query.data?.plugins ?? [],
    isLoaded: (name: string) => query.data?.plugins.some(plugin => plugin.name === name) ?? false,
    hasData: query.data !== undefined, isFetching: query.isFetching, isError: query.isError, refresh, refreshed,
  }
}

export { usePlugins }
