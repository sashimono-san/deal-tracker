export type Mode = 'exact' | 'sale' | 'discovery'
export type SalePeriod = 'black_friday' | 'christmas' | 'new_year' | 'winter_sales'
export type CheckSchedule = {
  repeat: 'daily' | 'weekly'
  interval: number
  weekdays: number[]
  start_date: string
  timezone: string
  sale_periods: SalePeriod[]
}

export const weekdays = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
export const salePeriods: Record<SalePeriod, string> = {
  black_friday: 'Black Friday week through Cyber Monday',
  christmas: 'Christmas: 15-26 December',
  new_year: 'New Year: 27 December-7 January',
  winter_sales: 'Year start: 8-31 January',
}

export type WatchSpec = {
  title: string
  mode: Mode
  description: string
  shop_url: string
  product_url: string
  sku: string
  audience: string
  size: string
  color: string
  country: string
  currency: string
  target_price: number | null
  criteria: string
  interval_hours: number | null
  schedule: CheckSchedule | null
  notify_review: boolean
}

export type Observation = {
  title: string
  kind: 'product' | 'sale'
  url: string
  evidence: string
  observed_at: string
  reason: string
  sku: string
  audience: string
  size: string
  color: string
  country: string
  currency: string
  price: number | null
  shipping: number | null
  availability: string
  sale_scope: string
  valid_until: string | null
}

export type Candidate = Observation & {
  id: string
  source: string
  feedback: 'unreviewed' | 'useful' | 'not_useful'
  feedback_reason: string
}

export type Watch = WatchSpec & {
  id: string
  version: number
  status: 'active' | 'paused' | 'archived'
  parent_id: string | null
  source_candidate_id: string | null
  source_snapshot: {
    watch_id: string
    version: number
    captured_at: string
    spec: WatchSpec
    candidate?: Observation
  } | null
  created_at: string
  updated_at: string
}

export type Preview = {
  dry_run: boolean
  sent: number
  source: string
  decisions: { title: string; url: string; decision: string; reason: string }[]
}

export type Run = {
  id: string
  created_at: string
  source: string
  result: { outcome: string; diagnostics: string; observations: Observation[] }
  preview: Preview | null
}

export type Detail = {
  watch: Watch
  parent: Watch | null
  children: Watch[]
  candidates: Candidate[]
  runs: Run[]
  activation_gates: string[]
  check_gates: string[]
  jobs: { id: string; outcome: string; started_at: string; diagnostics: string }[]
  deliveries: Delivery[]
  monitoring: {
    last_started_at: string | null
    last_finished_at: string | null
    last_outcome: string | null
    last_error: string
  }
  last_live_check: string | null
  next_scheduled_check: string | null
  shop_research: {
    outcome: 'found' | 'not_found'
    summary: string
    url: string
    evidence: string
    checked_at: string
  } | null
}

export type Expansion = {
  spec: WatchSpec
  version: number
  source: Watch
  candidate: Candidate | null
}

export type Delivery = {
  id: string
  status: string
  decision: string
  data: Observation
  recipient: string
  attempts: number
  error: string
  updated_at: string
}

export type Health = {
  status: string
  collector: string
  scheduler: string
  email: string
  collector_issues: string[]
  email_issues: string[]
  pi_provider: string
  pi_model: string
  pi_thinking: string
  browser: 'playwright_mcp' | 'http'
  browser_headless: boolean
  worker_last_seen: string | null
  checks_today: number
}

export const blankSpec: WatchSpec = {
  title: '', mode: 'exact', description: '', shop_url: '', product_url: '', sku: '',
  audience: '', size: '', color: '', country: '', currency: '', target_price: null,
  criteria: '', interval_hours: null, schedule: null, notify_review: false,
}

export function newSchedule(today = new Date()): CheckSchedule {
  return {
    repeat: 'weekly', interval: 1, weekdays: [0],
    start_date: [today.getFullYear(), String(today.getMonth() + 1).padStart(2, '0'), String(today.getDate()).padStart(2, '0')].join('-'),
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC', sale_periods: [],
  }
}

export function editableSpec(spec: WatchSpec): WatchSpec {
  if (spec.schedule || !spec.interval_hours) return spec
  return { ...spec, interval_hours: null, schedule: { ...newSchedule(), repeat: 'daily', interval: Math.max(1, Math.ceil(spec.interval_hours / 24)) } }
}

export function scheduleLabel(spec: WatchSpec): string {
  const schedule = spec.schedule
  if (!schedule) return spec.interval_hours ? (spec.interval_hours === 24 ? 'Daily' : spec.interval_hours % 24 === 0 ? `Every ${spec.interval_hours / 24} days` : 'Legacy schedule') : 'Manual only'
  if (schedule.repeat === 'daily') return schedule.interval === 1 ? 'Daily' : `Every ${schedule.interval} days`
  return `${schedule.interval === 1 ? 'Weekly' : `Every ${schedule.interval} weeks`} on ${schedule.weekdays.map(day => weekdays[day]).join(', ')}`
}

export function specOf(watch: Watch): WatchSpec {
  return Object.fromEntries(Object.keys(blankSpec).map(key => [key, watch[key as keyof WatchSpec]])) as WatchSpec
}

export async function request<Value>(path: string, options: RequestInit = {}): Promise<Value> {
  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), 10_000)
  const signal = options.signal
    ? AbortSignal.any([options.signal, controller.signal])
    : controller.signal
  try {
    const response = await fetch(path, {
      ...options,
      signal,
      headers: { 'Content-Type': 'application/json', ...options.headers },
    })
    if (!response.ok) {
      const body = await response.json().catch(() => ({})) as {
        detail?: string | { loc?: string[]; msg: string }[]
      }
      const message = Array.isArray(body.detail)
        ? body.detail.map(error => `${error.loc?.slice(1).join('.') || 'Request'}: ${error.msg}`).join('; ')
        : body.detail || `Request failed (${response.status})`
      throw new Error(message)
    }
    return await response.json() as Value
  } catch (error) {
    if (options.signal?.aborted) throw error
    const uncertainSave = options.method && !['GET', 'HEAD'].includes(options.method.toUpperCase())
      ? ' Saving was not confirmed. Check the watchlist before retrying.'
      : ''
    if (controller.signal.aborted) {
      throw new Error(`The local API did not respond within 10 seconds.${uncertainSave}`)
    }
    if (error instanceof TypeError) {
      throw new Error(`Cannot reach the local API. Check that the server is running.${uncertainSave}`)
    }
    throw error
  } finally {
    clearTimeout(timeout)
  }
}

export function displayError(error: unknown): string {
  return error instanceof Error ? error.message : 'Something went wrong'
}

export function money(value: number | null, currency: string): string {
  if (value === null) return 'Price unknown'
  try {
    return new Intl.NumberFormat(undefined, { style: 'currency', currency }).format(value)
  } catch {
    return `${value.toFixed(2)} ${currency || '(currency unknown)'}`
  }
}

export function dateTime(value: string, timeZone?: string): string {
  return new Date(value).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short', timeZone })
}

export function shopName(url: string): string {
  try { return new URL(url).hostname.replace(/^www\./, '') }
  catch { return 'No shop selected' }
}

export function sourceName(value: string): string {
  return value === 'pi' ? 'pi collector' : value === 'manual_import' ? 'Manual import' : value === 'local_command' ? 'Local command' : value
}