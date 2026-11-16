import assert from 'node:assert/strict'
import test from 'node:test'
import { blankSpec, dateTime, editableSpec, newSchedule, request, scheduleLabel } from '../src/api.ts'

test('saves JSON with a bounded request signal', async context => {
  context.mock.method(globalThis, 'fetch', async (path: string, options: RequestInit) => {
    assert.equal(path, '/api/watches')
    assert.equal(options.method, 'POST')
    assert.equal(options.body, '{"title":"A shirt"}')
    assert.ok(options.signal instanceof AbortSignal)
    return Response.json({ id: 'saved-watch' })
  })
  assert.deepEqual(await request('/api/watches', { method: 'POST', body: '{"title":"A shirt"}' }), { id: 'saved-watch' })
})

test('a stalled save rejects after ten seconds without retrying', async context => {
  context.mock.timers.enable({ apis: ['setTimeout'] })
  const fetchMock = context.mock.method(globalThis, 'fetch', (_path: string, options: RequestInit) => new Promise((_resolve, reject) => {
    options.signal!.addEventListener('abort', () => reject(options.signal!.reason), { once: true })
  }))
  const saving = request('/api/watches', { method: 'POST', body: '{}' })
  const rejected = assert.rejects(saving, /did not respond within 10 seconds.*Saving was not confirmed/)
  context.mock.timers.tick(10_000)
  await rejected
  assert.equal(fetchMock.mock.callCount(), 1)
})

test('the timeout also bounds a stalled JSON response body', async context => {
  context.mock.timers.enable({ apis: ['setTimeout'] })
  context.mock.method(globalThis, 'fetch', async (_path: string, options: RequestInit) => ({
    ok: true,
    json: () => new Promise((_resolve, reject) => {
      options.signal!.addEventListener('abort', () => reject(options.signal!.reason), { once: true })
    }),
  }))
  const saving = request('/api/watches', { method: 'POST', body: '{}' })
  const rejected = assert.rejects(saving, /Saving was not confirmed/)
  await Promise.resolve()
  context.mock.timers.tick(10_000)
  await rejected
})

test('an unavailable API returns an actionable error', async context => {
  context.mock.method(globalThis, 'fetch', async () => { throw new TypeError('Failed to fetch') })
  await assert.rejects(request('/api/watches', { method: 'POST', body: '{}' }), /Cannot reach the local API.*Check the watchlist before retrying/)
})

test('caller cancellation is preserved instead of reported as a timeout', async context => {
  context.mock.method(globalThis, 'fetch', (_path: string, options: RequestInit) => new Promise((_resolve, reject) => {
    options.signal!.addEventListener('abort', () => reject(options.signal!.reason), { once: true })
  }))
  const controller = new AbortController()
  const loading = request('/api/watches', { signal: controller.signal })
  const rejected = assert.rejects(loading, { name: 'AbortError' })
  controller.abort()
  await rejected
})

test('API validation errors retain their field names', async context => {
  context.mock.method(globalThis, 'fetch', async () => Response.json({
    detail: [{ loc: ['body', 'shop_url'], msg: 'Use a public shop URL' }],
  }, { status: 422 }))
  await assert.rejects(request('/api/watches', { method: 'POST', body: '{}' }), /shop_url: Use a public shop URL/)
})

test('calendar labels use weekdays and day/week intervals', () => {
  const schedule = { ...newSchedule(), weekdays: [0, 2] }
  assert.equal(scheduleLabel({ ...blankSpec, schedule }), 'Weekly on Monday, Wednesday')
  assert.equal(scheduleLabel({ ...blankSpec, schedule: { ...schedule, interval: 2 } }), 'Every 2 weeks on Monday, Wednesday')
  assert.equal(scheduleLabel({ ...blankSpec, schedule: { ...schedule, repeat: 'daily', interval: 3 } }), 'Every 3 days')
  assert.equal(scheduleLabel(blankSpec), 'Manual only')
})

test('schedule defaults keep the local calendar date and sale boosts off', () => {
  const schedule = newSchedule(new Date(2026, 10, 15, 23, 30))
  assert.equal(schedule.start_date, '2026-11-15')
  assert.deepEqual(schedule.sale_periods, [])
  assert.deepEqual(schedule.weekdays, [0])
})

test('editing legacy intervals converts to daily units without mutating the original', () => {
  const original = { ...blankSpec, interval_hours: 72 }
  const editable = editableSpec(original)
  assert.equal(editable.interval_hours, null)
  assert.equal(editable.schedule?.repeat, 'daily')
  assert.equal(editable.schedule?.interval, 3)
  assert.equal(original.interval_hours, 72)
  assert.equal(editableSpec(blankSpec).schedule, null)
})

test('due-date display respects the watch time zone rather than the browser zone', () => {
  const due = '2026-11-15T10:00:00+00:00'
  assert.equal(dateTime(due, 'Pacific/Kiritimati'), new Date(due).toLocaleString(undefined, {
    dateStyle: 'medium', timeStyle: 'short', timeZone: 'Pacific/Kiritimati',
  }))
  assert.notEqual(dateTime(due, 'Pacific/Kiritimati'), dateTime(due, 'UTC'))
})