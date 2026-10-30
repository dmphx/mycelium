import type { usePlugins } from '../hooks/usePlugins';

export default function PluginMetadataStatus({ metadata, empty, emptyMessage }: {
  metadata: ReturnType<typeof usePlugins>; empty: boolean; emptyMessage: string;
}) {
  return <section aria-label="Plugin metadata" className="space-y-2 mb-4 min-w-0">
    {metadata.isFetching && <p role="status" className="text-sm text-muted">{metadata.hasData ? 'Refreshing' : 'Loading'} plugin metadata...</p>}
    {metadata.isError && <p role="alert" className="text-sm text-red-400">Could not load plugin metadata. {metadata.hasData ? 'Previously loaded metadata is shown and may be out of date.' : 'Plugin availability is unknown.'} Refresh to try again.</p>}
    {!metadata.isFetching && !metadata.isError && metadata.refreshed && <p role="status" className="text-sm text-muted">Plugin metadata refreshed.</p>}
    {!metadata.isFetching && !metadata.isError && metadata.hasData && empty && <p className="text-sm text-muted">{emptyMessage}</p>}
    <button type="button" disabled={metadata.isFetching} onClick={metadata.refresh}
      className="min-h-[44px] rounded border border-border px-3 py-2 text-sm text-muted hover:text-white disabled:opacity-50">
      Refresh plugin metadata
    </button>
  </section>;
}
