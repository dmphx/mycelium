import { useQuery } from '@tanstack/react-query'

interface PluginMeta {
  name:        string
  label:       string
  version:     string
  description: string
  user_fields: string[]
}

function usePlugins() {
  const { data } = useQuery<{ plugins: PluginMeta[] }>({
    queryKey: ['plugins'],
    queryFn:  () => fetch('/ui/api/plugins').then(r => r.json()),
    staleTime: Infinity,  // plugins don't change at runtime
  })
  return {
    plugins:  data?.plugins ?? [],
    isLoaded: (name: string) => data?.plugins.some(p => p.name === name) ?? false,
  }
}

export { usePlugins, type PluginMeta }
