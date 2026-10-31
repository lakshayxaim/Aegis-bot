const token = document.querySelector('meta[name="aegis-token"]')?.getAttribute('content') ?? ''

export async function api<T = any>(method: string, path: string, body?: unknown): Promise<T> {
  const response = await fetch(path, {
    method,
    headers: { 'Content-Type': 'application/json', 'X-Aegis-Token': token },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!response.ok) {
    let detail = `Request failed (${response.status})`
    try {
      const parsed = await response.json()
      if (parsed.detail) detail = String(parsed.detail)
    } catch {
      // keep the generic message
    }
    throw new Error(detail)
  }
  return response.json()
}

export const rawUrl = (path: string) => `/api/files/raw?path=${encodeURIComponent(path)}&token=${encodeURIComponent(token)}`

export function connectEvents(onEvent: (event: any) => void, onConnection: (connected: boolean) => void): () => void {
  const source = new EventSource(`/api/events?token=${encodeURIComponent(token)}`)
  source.onopen = () => onConnection(true)
  source.onerror = () => onConnection(false)
  source.onmessage = message => {
    try {
      onEvent(JSON.parse(message.data))
    } catch {
      // a malformed event should not stop the stream
    }
  }
  return () => source.close()
}
