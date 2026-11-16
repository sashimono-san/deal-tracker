import { useEffect, useId, useRef, useState } from 'react'
import type { FormEvent, ReactNode } from 'react'
import { ArrowUpRight, GitBranch, Save, ScanSearch, Store, Tag, X } from 'lucide-react'
import { displayError, editableSpec, newSchedule, salePeriods, weekdays } from './api'
import type { CheckSchedule, Mode, SalePeriod, Watch, WatchSpec } from './api'

export function Modal({ title, children, onClose }: { title: string; children: ReactNode; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const titleId = useId()
  useEffect(() => {
    const element = dialog.current
    element?.showModal()
    return () => element?.close()
  }, [])
  return <dialog ref={dialog} aria-labelledby={titleId} onCancel={onClose} onClick={event => {
    if (event.target === event.currentTarget) onClose()
  }}>
    <div className="dialog-header"><h2 id={titleId}>{title}</h2><button className="icon-button" type="button" title="Close" aria-label="Close" onClick={onClose}><X size={20} /></button></div>
    {children}
  </dialog>
}

export const modeNames: Record<Mode, string> = { exact: 'Exact product', sale: 'Shop sale', discovery: 'Discovery' }
export const modeIcons = { exact: Tag, sale: Store, discovery: ScanSearch }

export function WatchEditor({ initial, source, editing, promoting, onSave, onClose }: {
  initial: WatchSpec
  source?: Watch
  editing?: boolean
  promoting?: boolean
  onSave: (spec: WatchSpec) => Promise<void>
  onClose: () => void
}) {
  const [spec, setSpec] = useState(() => editableSpec(initial))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const set = <Key extends keyof WatchSpec>(key: Key, value: WatchSpec[Key]) => setSpec(current => ({ ...current, [key]: value }))
  const schedule = spec.schedule
  const setSchedule = (change: Partial<CheckSchedule>) => set('schedule', { ...(schedule || newSchedule()), ...change })
  function submit(event: FormEvent) {
    event.preventDefault()
    if (schedule?.repeat === 'weekly' && !schedule.weekdays.length) { setError('Select at least one weekday'); return }
    setBusy(true)
    setError('')
    void onSave(spec)
      .catch(error => setError(displayError(error)))
      .finally(() => setBusy(false))
  }
  return <Modal title={editing ? 'Edit search' : promoting ? 'Track this product' : source ? 'Expand search' : 'New search'} onClose={() => { if (!busy) onClose() }}>
    <form onSubmit={submit}>
      <div className="dialog-body">
        {source && <div className="source-reference"><GitBranch size={18} /><div><span className="eyebrow">Source search / v{source.version}</span><strong>{source.title}</strong><p>{source.description || 'No source description'}</p><span className="muted">{source.sku ? `Reference SKU: ${source.sku}` : modeNames[source.mode]}</span></div></div>}
        {error && <div className="error" role="alert">{error}</div>}
        <fieldset disabled={busy}>
          <legend className="sr-only">Search details</legend>
          <div className="segmented mode-picker" role="group" aria-label="Search mode">
            {(Object.keys(modeNames) as Mode[]).map(mode => {
              const Icon = modeIcons[mode]
              return <button type="button" key={mode} aria-pressed={spec.mode === mode} disabled={Boolean(editing || promoting) && spec.mode !== mode} className={spec.mode === mode ? 'selected' : ''} onClick={() => {
                if (spec.mode !== mode) setSpec(current => ({ ...current, mode, sku: '', product_url: '', size: '', color: '' }))
              }}><Icon size={16} />{modeNames[mode]}</button>
            })}
          </div>
          <label>Search name<input autoFocus required maxLength={160} value={spec.title} onChange={event => set('title', event.target.value)} /></label>
          <label>Description<textarea rows={3} maxLength={5000} value={spec.description} onChange={event => set('description', event.target.value)} /></label>
          <div className="form-grid">
            <label>Shop URL<input type="url" maxLength={2000} placeholder="https://" value={spec.shop_url} onChange={event => {
              setSpec(current => ({ ...current, shop_url: event.target.value, ...(source ? { sku: '', product_url: '', size: '', color: '' } : {}) }))
            }} /></label>
            <label>Shopping for<input maxLength={100} value={spec.audience} onChange={event => {
              setSpec(current => ({ ...current, audience: event.target.value, ...(source ? { size: '', color: '' } : {}) }))
            }} /></label>
            {spec.mode === 'exact' && <><label>Product URL<input type="url" maxLength={2000} placeholder="https://" value={spec.product_url} onChange={event => set('product_url', event.target.value)} /></label><label>SKU<input maxLength={100} value={spec.sku} onChange={event => set('sku', event.target.value)} /></label></>}
            {spec.mode !== 'sale' && <><label>Size<input maxLength={100} value={spec.size} onChange={event => set('size', event.target.value)} /></label><label>Color<input maxLength={100} value={spec.color} onChange={event => set('color', event.target.value)} /></label></>}
            <label>Delivery country<input maxLength={80} value={spec.country} onChange={event => set('country', event.target.value)} /></label>
            <label>Currency<input maxLength={3} pattern="[A-Za-z]{3}|" placeholder="EUR, USD..." value={spec.currency} onChange={event => set('currency', event.target.value.toUpperCase())} /></label>
            <label>Item-price target<input type="number" min="0.01" max="1000000" step="0.01" value={spec.target_price ?? ''} onChange={event => set('target_price', event.target.value === '' ? null : Number(event.target.value))} /></label>
          </div>
          <label>{spec.mode === 'sale' ? 'Categories and sale conditions' : 'Additional requirements'}<textarea rows={2} maxLength={3000} value={spec.criteria} onChange={event => set('criteria', event.target.value)} /></label>
          <section className="schedule-fields" aria-label="Check schedule"><h3>Schedule</h3>
            <div className="form-grid">
              <label>Repeat<select value={schedule?.repeat || 'manual'} onChange={event => {
                const repeat = event.target.value
                setSpec(current => ({ ...current, interval_hours: null, schedule: repeat === 'manual' ? null : { ...(current.schedule || newSchedule()), repeat: repeat as CheckSchedule['repeat'] } }))
              }}><option value="manual">Manual only</option><option value="daily">Daily</option><option value="weekly">Weekly</option></select></label>
              {schedule && <label>Repeat every<div className="repeat-interval"><input aria-label="Repeat interval" type="number" min="1" max="52" step="1" required value={schedule.interval} onChange={event => setSchedule({ interval: Number(event.target.value) })} /><span>{schedule.repeat === 'weekly' ? 'weeks' : 'days'}</span></div></label>}
            </div>
            {schedule && <>
              {schedule.repeat === 'weekly' && <fieldset className="weekday-picker"><legend>On</legend><div>{weekdays.map((name, index) => <label key={name} title={name}><input type="checkbox" aria-label={name} checked={schedule.weekdays.includes(index)} onChange={event => setSchedule({ weekdays: event.target.checked ? [...schedule.weekdays, index].sort((first, second) => first - second) : schedule.weekdays.filter(day => day !== index) })} /><span>{name.slice(0, 3)}</span></label>)}</div></fieldset>}
              <div className="form-grid"><label>Starts<input type="date" min="2000-01-01" max="2100-12-31" required value={schedule.start_date} onChange={event => setSchedule({ start_date: event.target.value })} /></label><label>Time zone<select value={schedule.timezone} onChange={event => setSchedule({ timezone: event.target.value })}>{Array.from(new Set([schedule.timezone, 'UTC', ...Intl.supportedValuesOf('timeZone')])).map(zone => <option key={zone}>{zone}</option>)}</select></label></div>
              <fieldset className="sale-period-picker"><legend>Daily checks during</legend>{(Object.entries(salePeriods) as [SalePeriod, string][]).map(([period, label]) => <label key={period} className="checkbox-label"><input type="checkbox" checked={schedule.sale_periods.includes(period)} onChange={event => setSchedule({ sale_periods: event.target.checked ? [...schedule.sale_periods, period] : schedule.sale_periods.filter(value => value !== period) })} />{label}</label>)}</fieldset>
            </>}
          </section>
          <label className="checkbox-label"><input type="checkbox" checked={spec.notify_review} onChange={event => set('notify_review', event.target.checked)} />Email new candidates that need review</label>
        </fieldset>
      </div>
      <footer className="dialog-footer"><span className="muted">{editing ? 'Editing pauses active monitoring' : 'New searches are paused'}</span><div className="actions"><button type="button" className="quiet" disabled={busy} onClick={onClose}>Cancel</button><button type="submit" className="primary" disabled={busy}>{source ? <ArrowUpRight size={17} /> : <Save size={17} />}{busy ? 'Saving...' : editing ? 'Save changes' : 'Save search'}</button></div></footer>
    </form>
  </Modal>
}