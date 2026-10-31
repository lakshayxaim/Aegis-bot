export function formatBytes(value: number | null | undefined): string {
  if (!value) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let amount = value
  let unit = 0
  while (amount >= 1024 && unit < units.length - 1) {
    amount /= 1024
    unit += 1
  }
  return `${amount >= 100 || unit === 0 ? amount.toFixed(0) : amount.toFixed(1)} ${units[unit]}`
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms == null) return ''
  if (ms < 1000) return `${Math.max(1, Math.round(ms))} ms`
  const seconds = ms / 1000
  if (seconds < 60) return `${seconds.toFixed(1)} s`
  return `${Math.floor(seconds / 60)} min ${Math.round(seconds % 60)} s`
}

export function clock(timestamp: number): string {
  return new Date(timestamp * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

export function dayLabel(timestamp: number): string {
  const date = new Date(timestamp * 1000)
  const startOfToday = new Date()
  startOfToday.setHours(0, 0, 0, 0)
  const days = Math.floor((startOfToday.getTime() - new Date(date).setHours(0, 0, 0, 0)) / 86400000)
  if (days <= 0) return 'Today'
  if (days === 1) return 'Yesterday'
  if (days < 7) return 'This week'
  if (days < 30) return 'This month'
  return 'Earlier'
}

export function when(timestamp: number): string {
  const date = new Date(timestamp * 1000)
  const now = new Date()
  const time = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  if (date.toDateString() === now.toDateString()) return `Today, ${time}`
  const tomorrow = new Date(now)
  tomorrow.setDate(now.getDate() + 1)
  if (date.toDateString() === tomorrow.toDateString()) return `Tomorrow, ${time}`
  const yesterday = new Date(now)
  yesterday.setDate(now.getDate() - 1)
  if (date.toDateString() === yesterday.toDateString()) return `Yesterday, ${time}`
  return `${date.toLocaleDateString([], { day: 'numeric', month: 'short' })}, ${time}`
}

export function greeting(): string {
  const hour = new Date().getHours()
  if (hour < 5) return 'Up late'
  if (hour < 12) return 'Good morning'
  if (hour < 17) return 'Good afternoon'
  return 'Good evening'
}

export function shortPath(path: string, folders: Record<string, string>): string {
  const normalized = path.replace(/\//g, '\\')
  let best = ''
  let label = ''
  for (const [name, root] of Object.entries(folders)) {
    const base = root.replace(/\//g, '\\')
    if (normalized.toLowerCase().startsWith(base.toLowerCase()) && base.length > best.length) {
      best = base
      label = name === 'home' ? 'Home' : name[0].toUpperCase() + name.slice(1)
    }
  }
  return best ? `${label}${normalized.slice(best.length)}` : normalized
}
