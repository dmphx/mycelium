export async function jsonRequest(url: string, signal: AbortSignal, init: RequestInit = {}, timeoutMs = 15_000) {
  const controller = new AbortController()
  const abort = () => controller.abort()
  signal.addEventListener('abort', abort, { once: true })
  if (signal.aborted) controller.abort()
  const timer = setTimeout(abort, timeoutMs)
  try {
    const response = await fetch(url, { ...init, credentials: 'same-origin', redirect: 'error', signal: controller.signal })
    if (!response.ok) throw new Error(response.status === 401 || response.status === 403
      ? 'Your player session is unavailable. Sign in again.' : `Player request failed (HTTP ${response.status}).`)
    try { return await response.json() } catch { throw new Error('Invalid player response.') }
  } catch (error) {
    if (controller.signal.aborted && !signal.aborted) throw new Error('Player request timed out.')
    throw error
  } finally { clearTimeout(timer); signal.removeEventListener('abort', abort) }
}
