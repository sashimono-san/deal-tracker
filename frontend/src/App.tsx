import { useDeferredValue, useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import {
  Activity, Archive, ArrowLeft, ArrowUpRight, Check, ChevronRight, CirclePause,
  Download, ExternalLink, FileJson, GitBranch, LayoutList, Mail, Pencil, Plus,
  Clock3, Play, Radar, RotateCcw, Search, Settings, SlidersHorizontal, ThumbsDown, ThumbsUp, Upload, X,
} from 'lucide-react'
import { blankSpec, dateTime, displayError, money, request, salePeriods, scheduleLabel, shopName, sourceName, specOf } from './api'
import type { Candidate, Delivery, Detail, Expansion, Health, Mode, Watch, WatchSpec } from './api'
import { Modal, WatchEditor, modeIcons, modeNames } from './WatchEditor'
import './App.css'

type Editor = { initial: WatchSpec; source?: Watch; watch?: Watch; candidateId?: string }
type Tab = 'overview' | 'candidates' | 'activity'

function exportJson(filename: string, value: unknown) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], { type: 'application/json' }))
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  link.click()
  URL.revokeObjectURL(url)
}

function ImportDialog({ watch, onSave, onClose }: { watch: Watch; onSave: (body: string) => Promise<void>; onClose: () => void }) {
  const [body, setBody] = useState(JSON.stringify({ schema_version: 1, watch_version: watch.version, request_id: crypto.randomUUID(), outcome: 'no_change', diagnostics: '', observations: [] }, null, 2))
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  async function submit(event: FormEvent) {
    event.preventDefault()
    setError('')
    try {
      JSON.parse(body)
      if (new Blob([body]).size > 256_000) throw new Error('Result exceeds 256 KB')
      setBusy(true)
      await onSave(body)
    } catch (error) { setError(displayError(error)); setBusy(false) }
  }
  return <Modal title="Import collection result" onClose={() => { if (!busy) onClose() }}><form onSubmit={event => { void submit(event) }}>
    <div className="dialog-body"><p className="source-label"><FileJson size={16} />Manual import / {watch.title} / v{watch.version}</p>
      {error && <div role="alert" className="error">{error}</div>}
      <label>JSON file<input type="file" accept="application/json,.json" disabled={busy} onChange={event => {
        const file = event.target.files?.[0]
        if (!file) return
        if (file.size > 256_000) { setError('Result exceeds 256 KB'); return }
        void file.text().then(setBody).catch(error => setError(displayError(error)))
      }} /></label>
      <label>Result JSON<textarea className="json-input" rows={14} spellCheck={false} value={body} disabled={busy} onChange={event => setBody(event.target.value)} /></label>
    </div><footer className="dialog-footer"><span className="muted">No live collection or email</span><button className="primary" disabled={busy}><Upload size={17} />{busy ? 'Importing...' : 'Import result'}</button></footer>
  </form></Modal>
}

function FeedbackDialog({ candidate, onSave, onClose }: { candidate: Candidate; onSave: (feedback: Candidate['feedback'], reason: string) => Promise<void>; onClose: () => void }) {
  const [feedback, setFeedback] = useState(candidate.feedback)
  const [reason, setReason] = useState(candidate.feedback_reason)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  return <Modal title="Review candidate" onClose={() => { if (!busy) onClose() }}><form onSubmit={event => {
    event.preventDefault(); setBusy(true); setError('')
    void onSave(feedback, reason).catch(error => { setError(displayError(error)); setBusy(false) })
  }}><div className="dialog-body"><h3>{candidate.title}</h3>{error && <div className="error" role="alert">{error}</div>}
    <label>Verdict<select value={feedback} disabled={busy} onChange={event => setFeedback(event.target.value as Candidate['feedback'])}><option value="unreviewed">Unreviewed</option><option value="useful">Useful</option><option value="not_useful">Not useful</option></select></label>
    <label>Reason<textarea rows={3} maxLength={1000} disabled={busy} value={reason} onChange={event => setReason(event.target.value)} /></label>
    </div><footer className="dialog-footer"><button className="primary" disabled={busy}><Check size={17} />{busy ? 'Saving...' : 'Save review'}</button></footer></form></Modal>
}

function App() {
  const [watches, setWatches] = useState<Watch[]>([])
  const [selectedId, setSelectedId] = useState<string | null>(() => window.location.hash.slice(1) || null)
  const [detail, setDetail] = useState<Detail | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [health, setHealth] = useState<Health | null>(null)
  const [healthReadAt, setHealthReadAt] = useState(0)
  const [modeFilter, setModeFilter] = useState<Mode | 'all'>('all')
  const [statusFilter, setStatusFilter] = useState('current')
  const [query, setQuery] = useState('')
  const deferredQuery = useDeferredValue(query)
  const [tab, setTab] = useState<Tab>('overview')
  const [editor, setEditor] = useState<Editor | null>(null)
  const [importing, setImporting] = useState(false)
  const [reviewing, setReviewing] = useState<Candidate | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [connections, setConnections] = useState(false)

  useEffect(() => {
    const controller = new AbortController()
    void Promise.all([
      request<Watch[]>('/api/watches', { signal: controller.signal }),
      request<Health>('/api/health', { signal: controller.signal }),
    ]).then(([records, state]) => {
      setWatches(records); setHealth(state); setHealthReadAt(Date.now()); setLoaded(true)
      setSelectedId(current => current || records.find(watch => watch.status !== 'archived')?.id || null)
    }).catch(error => { if (!controller.signal.aborted) { setError(displayError(error)); setLoaded(true) } })
    return () => controller.abort()
  }, [])

  useEffect(() => {
    const update = () => setSelectedId(window.location.hash.slice(1) || null)
    window.addEventListener('hashchange', update)
    return () => window.removeEventListener('hashchange', update)
  }, [])

  useEffect(() => {
    if (!selectedId) return
    const controller = new AbortController()
    void request<Detail>(`/api/watches/${selectedId}`, { signal: controller.signal }).then(setDetail)
      .catch(error => { if (!controller.signal.aborted) setError(displayError(error)) })
    return () => controller.abort()
  }, [selectedId])

  useEffect(() => {
    if (!loaded) return
    const controller = new AbortController()
    let pending = false
    const poll = async () => {
      if (document.hidden || pending) return
      pending = true
      try {
        const [records, state, selected] = await Promise.all([
          request<Watch[]>('/api/watches', { signal: controller.signal }),
          request<Health>('/api/health', { signal: controller.signal }),
          selectedId ? request<Detail>(`/api/watches/${selectedId}`, { signal: controller.signal }) : null,
        ])
        if (!controller.signal.aborted) {
          setWatches(records); setHealth(state); setHealthReadAt(Date.now())
          if (selected) setDetail(selected)
        }
      } catch (error) { if (!controller.signal.aborted) setError(displayError(error)) }
      finally { pending = false }
    }
    const interval = window.setInterval(() => { void poll() }, 5000)
    return () => { controller.abort(); window.clearInterval(interval) }
  }, [loaded, selectedId])

  const current = detail?.watch.id === selectedId ? detail : null
  const visible = watches.filter(watch =>
    (modeFilter === 'all' || watch.mode === modeFilter) &&
    (statusFilter === 'all' || (statusFilter === 'archived' ? watch.status === 'archived' : watch.status !== 'archived')) &&
    `${watch.title} ${watch.description} ${watch.sku} ${watch.audience} ${watch.shop_url}`.toLowerCase().includes(deferredQuery.toLowerCase()),
  )
  const currentWatches = watches.filter(watch => watch.status !== 'archived')

  function select(watchId: string) {
    setSelectedId(watchId); setTab('overview'); setError('')
    window.location.hash = watchId
  }

  async function refresh(watchId: string | null = selectedId) {
    setWatches(await request<Watch[]>('/api/watches'))
    setHealth(await request<Health>('/api/health'))
    setHealthReadAt(Date.now())
    if (watchId) {
      const selected = await request<Detail>(`/api/watches/${watchId}`)
      const hash = window.location.hash.slice(1)
      if (hash === watchId || (!hash && watchId === selectedId)) setDetail(selected)
    }
  }

  async function action(work: () => Promise<void>) {
    setError(''); setNotice(''); setBusy(true)
    try { await work() } catch (error) { setError(displayError(error)) }
    finally { setBusy(false) }
  }

  async function expand(candidateId?: string) {
    if (!current) return
    await action(async () => {
      const draft = await request<Expansion>(`/api/watches/${current.watch.id}/expansion${candidateId ? `?candidate_id=${candidateId}` : ''}`)
      setEditor({ initial: draft.spec, source: draft.source, candidateId })
    })
  }

  async function save(spec: WatchSpec) {
    if (!editor) return
    let result: Watch
    if (editor.watch) {
      result = await request<Watch>(`/api/watches/${editor.watch.id}`, { method: 'PUT', body: JSON.stringify({ version: editor.watch.version, spec }) })
    } else if (editor.source) {
      result = await request<Watch>(`/api/watches/${editor.source.id}/derive`, { method: 'POST', body: JSON.stringify({ version: editor.source.version, spec, candidate_id: editor.candidateId || null }) })
    } else result = await request<Watch>('/api/watches', { method: 'POST', body: JSON.stringify(spec) })
    setEditor(null); setModeFilter('all'); setStatusFilter('current'); setQuery('')
    select(result.id)
    await refresh(result.id)
    setNotice('Search saved')
  }

  async function changeStatus(status: Watch['status']) {
    if (!current) return
    await action(async () => {
      await request(`/api/watches/${current.watch.id}/status`, {
        method: 'PATCH', body: JSON.stringify({ version: current.watch.version, status }),
      })
      await refresh()
      setNotice(status === 'active' ? 'Monitoring activated' : 'Monitoring paused')
    })
  }

  async function resolveMail(delivery: Delivery, resolution: 'retry' | 'mark_sent' | 'dismiss') {
    if (!current) return
    const uncertainRetry = resolution === 'retry' && delivery.status === 'uncertain'
    if (uncertainRetry && !window.confirm('Delivery may already have happened. Have you checked the inbox and accepted the risk of a duplicate email?')) return
    await action(async () => {
      await request(`/api/watches/${current.watch.id}/deliveries/${delivery.id}`, {
        method: 'PATCH', body: JSON.stringify({ action: resolution, acknowledge_duplicate: uncertainRetry }),
      })
      await refresh()
      setNotice(resolution === 'retry' ? 'Delivery queued for retry' : 'Delivery status updated')
    })
  }

  const pendingCheck = current?.jobs?.find(job => ['queued', 'running'].includes(job.outcome))
  const workerRecent = Boolean(health?.worker_last_seen && healthReadAt - Date.parse(health.worker_last_seen) < 36 * 60 * 60_000)

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><span className="brand-mark"><Radar size={25} /></span><div>Deal tracker<span>PERSONAL WORKSPACE</span></div></div>
      <nav aria-label="Search modes"><span className="nav-heading">WATCHLIST</span>
        <button className={modeFilter === 'all' ? 'nav-item selected' : 'nav-item'} onClick={() => setModeFilter('all')}><LayoutList size={18} /><span>All searches</span><span className="nav-count">{currentWatches.length}</span></button>
        {(Object.keys(modeNames) as Mode[]).map(mode => { const Icon = modeIcons[mode]; return <button key={mode} className={modeFilter === mode ? 'nav-item selected' : 'nav-item'} onClick={() => setModeFilter(mode)}><Icon size={18} /><span>{modeNames[mode]}</span><span className="nav-count">{currentWatches.filter(watch => watch.mode === mode).length}</span></button> })}
      </nav>
      <div className="workspace-status"><span className="nav-heading">CONNECTIONS</span><div><span className={`status-dot ${health?.collector === 'pi' ? '' : 'muted-dot'}`} /><span>pi collector</span><strong>{health?.collector === 'pi' ? 'Configured' : 'Disabled'}</strong></div><div><Clock3 size={13} /><span>Worker</span><strong>{workerRecent ? 'Seen recently' : 'Not seen'}</strong></div><div><Mail size={13} /><span>Email</span><strong>{health?.email === 'smtp' ? 'SMTP enabled' : 'Preview only'}</strong></div><button className="quiet small" onClick={() => setConnections(true)}><Settings size={14} />Connection status</button></div>
      <div className="sidebar-footer"><span className={`status-dot ${health?.status === 'ok' ? '' : 'muted-dot'}`} />{health?.status === 'ok' ? 'Local storage connected' : 'Connecting to local storage'}</div>
    </aside>

    <main>
      <header className="page-header"><div><span className="eyebrow">BUYING INTENTIONS</span><h1>Your watchlist</h1><p>{currentWatches.length} {currentWatches.length === 1 ? 'search' : 'searches'}<span className="separator">/</span>{currentWatches.filter(watch => watch.parent_id).length} linked searches</p></div><button className="primary" title="New search" aria-label="New search" onClick={() => setEditor({ initial: { ...blankSpec } })}><Plus size={18} />New search</button></header>
      {error && <div className="error global-message" role="alert"><span>{error}</span><button className="icon-button" aria-label="Dismiss error" onClick={() => setError('')}><X size={17} /></button></div>}
      {notice && <div className="notice global-message" role="status"><Check size={16} /><span>{notice}</span><button className="icon-button" aria-label="Dismiss notification" onClick={() => setNotice('')}><X size={17} /></button></div>}
      <div className="workspace-toolbar"><label className="search-box"><Search size={18} /><input aria-label="Search watchlist" placeholder="Search names, shops, SKUs..." value={query} onChange={event => setQuery(event.target.value)} /></label><div className="filter-select"><SlidersHorizontal size={16} /><select aria-label="Search status" value={statusFilter} onChange={event => setStatusFilter(event.target.value)}><option value="current">Current searches</option><option value="archived">Archived</option><option value="all">All statuses</option></select></div><button className="icon-button" title="Refresh searches" aria-label="Refresh searches" disabled={busy} onClick={() => { void action(() => refresh()) }}><RotateCcw size={18} /></button><button className="icon-button" title="Connection status" aria-label="Connection status" onClick={() => setConnections(true)}><Settings size={18} /></button></div>
      <div className="workspace-grid">
        <section className="watch-list" aria-label="Searches">
          <div className="list-heading"><span>{modeFilter === 'all' ? 'All searches' : modeNames[modeFilter]}</span><span>{visible.length.toString().padStart(2, '0')}</span></div>
          {!loaded ? <div className="empty compact" role="status">Loading searches...</div> : visible.length === 0 ? <div className="empty compact"><Search size={28} /><h2>{watches.length ? 'No matching searches' : 'No searches yet'}</h2>{watches.length ? <button className="quiet" onClick={() => { setQuery(''); setModeFilter('all'); setStatusFilter('all') }}>Clear filters</button> : <button className="quiet" onClick={() => setEditor({ initial: { ...blankSpec } })}><Plus size={16} />New search</button>}</div> : visible.map(watch => {
            const Icon = modeIcons[watch.mode]
            return <button className={`watch-row ${selectedId === watch.id ? 'selected' : ''}`} key={watch.id} onClick={() => select(watch.id)} aria-pressed={selectedId === watch.id}>
              <span className={`mode-icon ${watch.mode}`}><Icon size={20} /></span><span className="watch-summary"><span className="watch-title">{watch.title}</span><span className="watch-shop">{shopName(watch.shop_url)}</span><span className="watch-meta">{watch.audience || modeNames[watch.mode]}{watch.parent_id && <><GitBranch size={12} />Linked</>}</span></span><span className="row-end"><span className="state-label">{watch.status}</span><ChevronRight size={16} /></span>
            </button>
          })}
        </section>

        <section className="detail-pane" aria-label="Selected search">
          {!current ? <div className="empty detail-empty"><span className="empty-symbol"><GitBranch size={38} strokeWidth={1.4} /></span><h2>{selectedId ? 'Loading search...' : 'Your next good find'}</h2>{!selectedId && <button className="quiet" onClick={() => setEditor({ initial: { ...blankSpec } })}><Plus size={17} />New search</button>}</div> : <>
            <header className="detail-header"><div className="detail-labels"><span className={`mode-label ${current.watch.mode}`}>{modeNames[current.watch.mode]}</span><span className="state-label"><CirclePause size={12} />{current.watch.status}</span></div><h2>{current.watch.title}</h2><div className="detail-subline"><span>{shopName(current.watch.shop_url)}</span>{current.watch.audience && <><span className="separator">/</span><span>{current.watch.audience}</span></>}</div>
              <div className="detail-actions"><button className="primary secondary" disabled={busy} onClick={() => { void expand() }}><GitBranch size={17} />Expand search</button><div className="actions"><button className="icon-button" title="Edit search" aria-label="Edit search" onClick={() => setEditor({ initial: specOf(current.watch), watch: current.watch })}><Pencil size={17} /></button><button className="icon-button" title="Export search" aria-label="Export search" onClick={() => exportJson(`watch-${current.watch.id}.json`, current.watch)}><Download size={17} /></button><button className="icon-button" title={current.watch.status === 'archived' ? 'Restore search' : 'Archive search'} aria-label={current.watch.status === 'archived' ? 'Restore search' : 'Archive search'} disabled={busy} onClick={() => { void action(async () => {
                await request(`/api/watches/${current.watch.id}/status`, { method: 'PATCH', body: JSON.stringify({ version: current.watch.version, status: current.watch.status === 'archived' ? 'paused' : 'archived' }) }); await refresh(); setNotice(current.watch.status === 'archived' ? 'Search restored, paused' : 'Search archived')
              }) }}>{current.watch.status === 'archived' ? <RotateCcw size={17} /> : <Archive size={17} />}</button></div></div>
            </header>
            <section className="monitoring-band" aria-label="Monitoring">
              <div className="monitoring-controls">
                <button className="quiet small" disabled={busy || current.watch.status === 'archived' || (current.watch.status !== 'active' && current.activation_gates.length > 0)} title={current.watch.status === 'active' ? 'Pause monitoring' : current.activation_gates.join('\n') || 'Activate monitoring'} onClick={() => { void changeStatus(current.watch.status === 'active' ? 'paused' : 'active') }}>{current.watch.status === 'active' ? <CirclePause size={16} /> : <Play size={16} />}{current.watch.status === 'active' ? 'Pause monitoring' : 'Activate monitoring'}</button>
                <button className="quiet small" disabled={busy || Boolean(pendingCheck) || current.watch.status === 'archived' || Boolean(current.check_gates?.length)} title={current.check_gates?.join('\n') || 'Queue a check'} onClick={() => { void action(async () => {
                  await request(`/api/watches/${current.watch.id}/checks`, { method: 'POST', body: JSON.stringify({ version: current.watch.version }) })
                  await refresh(); setNotice('Check queued')
                }) }}><Radar size={16} />Request check</button>
                {pendingCheck && <span className="check-progress" role="status"><Clock3 size={14} />{pendingCheck.outcome === 'queued' ? 'Queued for daily batch' : 'Checking...'}</span>}
              </div>
              <div className="monitoring-meta"><span>Last attempt: {current.monitoring?.last_finished_at ? dateTime(current.monitoring.last_finished_at) : 'Never'}</span>{current.monitoring?.last_outcome && <span className={`outcome ${current.monitoring.last_outcome}`}>{current.monitoring.last_outcome.replaceAll('_', ' ')}</span>}</div>
              {current.monitoring?.last_error && <p className="monitoring-error">{current.monitoring.last_error}</p>}
              {health?.scheduler === 'external' && !workerRecent && <p className="monitoring-error">No worker batch recorded in the last 36 hours</p>}
            </section>
            <div className="tabs" role="tablist" aria-label="Search details">{(['overview', 'candidates', 'activity'] as Tab[]).map((value, index) => <button key={value} role="tab" id={`tab-${value}`} aria-controls={`panel-${value}`} aria-selected={tab === value} tabIndex={tab === value ? 0 : -1} className={tab === value ? 'selected' : ''} onClick={() => setTab(value)} onKeyDown={event => {
              if (event.key === 'ArrowRight' || event.key === 'ArrowLeft') { event.preventDefault(); const next = (index + (event.key === 'ArrowRight' ? 1 : 2)) % 3; const name = (['overview', 'candidates', 'activity'] as Tab[])[next]; setTab(name); document.getElementById(`tab-${name}`)?.focus() }
            }}>{value === 'overview' ? 'Overview' : value === 'candidates' ? <>Candidates <span>{current.candidates.length}</span></> : <>Activity <span>{current.runs.length}</span></>}</button>)}</div>
            <div className="tab-content" role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`}>
              {tab === 'overview' && <>
                {current.watch.source_snapshot && <section className="lineage-source"><span className="eyebrow"><GitBranch size={14} />DERIVED FROM</span><button className="text-link" onClick={() => select(current.watch.parent_id!)}><ArrowLeft size={15} />{current.watch.source_snapshot.spec.title}</button><details><summary>Source snapshot / v{current.watch.source_snapshot.version}</summary><p>{current.watch.source_snapshot.spec.description || 'No description recorded'}</p><dl><div><dt>SKU</dt><dd>{current.watch.source_snapshot.spec.sku || 'Not set'}</dd></div><div><dt>Captured</dt><dd>{dateTime(current.watch.source_snapshot.captured_at)}</dd></div></dl>{current.watch.source_snapshot.candidate && <p>Candidate: {current.watch.source_snapshot.candidate.title}</p>}</details></section>}
                <section className="description-section"><span className="eyebrow">WHAT YOU'RE LOOKING FOR</span><p className={current.watch.description ? 'description' : 'muted'}>{current.watch.description || 'No description added'}</p></section>
                <dl className="facts"><div><dt>Item-price target</dt><dd className="price">{current.watch.target_price === null ? 'Not set' : money(current.watch.target_price, current.watch.currency)}</dd></div><div><dt>Delivery market</dt><dd>{current.watch.country || 'Not set'}{current.watch.currency && ` / ${current.watch.currency}`}</dd></div><div><dt>SKU</dt><dd>{current.watch.sku || 'Not set'}</dd></div><div><dt>Size / color</dt><dd>{[current.watch.size, current.watch.color].filter(Boolean).join(' / ') || 'Not set'}</dd></div><div><dt>Schedule</dt><dd>{scheduleLabel(current.watch)}{current.watch.schedule && <small className="schedule-zone">{current.watch.schedule.timezone}</small>}</dd></div><div><dt>Next check due</dt><dd>{current.next_scheduled_check ? dateTime(current.next_scheduled_check, current.watch.schedule?.timezone) : 'Not scheduled'}</dd></div><div><dt>Review emails</dt><dd>{current.watch.notify_review ? 'Opted in' : 'Off'}</dd></div></dl>
                {Boolean(current.watch.schedule?.sale_periods.length) && <section className="section-band"><h3>Daily sale-period checks</h3><ul>{current.watch.schedule!.sale_periods.map(period => <li key={period}>{salePeriods[period]}</li>)}</ul></section>}
                {current.watch.product_url && <a className="external-link" href={current.watch.product_url} target="_blank" rel="noreferrer">Product page<ExternalLink size={14} /></a>}
                {current.watch.criteria && <section className="section-band"><h3>Additional requirements</h3><p>{current.watch.criteria}</p></section>}
                <section className="section-band" aria-label="Shop sale notes"><h3>Shop sale notes</h3>{current.shop_research ? <>
                  <span className="source-label">Checked {dateTime(current.shop_research.checked_at)}</span>
                  <p>{current.shop_research.summary}</p>
                  {current.shop_research.evidence && <blockquote>{current.shop_research.evidence}</blockquote>}
                  {current.shop_research.url && <a className="external-link" href={current.shop_research.url} target="_blank" rel="noreferrer">Published source<ExternalLink size={14} /></a>}
                </> : <p className="muted">Not researched</p>}</section>
                <section className="section-band"><div className="section-heading"><h3>Linked searches</h3><span className="count">{current.children.length}</span></div>{current.children.length ? <div className="children-list">{current.children.map(child => <button key={child.id} onClick={() => select(child.id)}><GitBranch size={16} /><span>{child.title}<small>{modeNames[child.mode]}{child.audience && ` / ${child.audience}`}</small></span><ArrowUpRight size={16} /></button>)}</div> : <p className="muted">No derived searches</p>}</section>
                {current.activation_gates.length > 0 && <details className="activation"><summary><CirclePause size={15} />Activation pending<span>{current.activation_gates.length}</span></summary><ul>{current.activation_gates.map(gate => <li key={gate}>{gate}</li>)}</ul></details>}
              </>}
              {tab === 'candidates' && <><div className="section-heading"><h3>Candidate review</h3><button className="quiet small" onClick={() => setImporting(true)}><Upload size={15} />Import result</button></div>{current.candidates.length === 0 ? <div className="empty"><Search size={30} /><h3>No candidates yet</h3></div> : <div className="candidate-list">{current.candidates.map(candidate => <article className="candidate" key={candidate.id}><div className="candidate-heading"><h3>{candidate.title}</h3><strong>{money(candidate.price, candidate.currency)}</strong></div><div className="candidate-meta"><span>{sourceName(candidate.source)}</span><span>{dateTime(candidate.observed_at)}</span></div><p>{candidate.evidence}</p>{candidate.reason && <p className="muted">Match rationale: {candidate.reason}</p>}<div className="candidate-facts"><span>{candidate.availability.replaceAll('_', ' ')}</span><span>{[candidate.sku, candidate.size, candidate.color].filter(Boolean).join(' / ')}</span><span>{candidate.shipping === null ? 'Shipping unknown' : `Shipping ${money(candidate.shipping, candidate.currency)}`}</span></div><a className="external-link" href={candidate.url} target="_blank" rel="noreferrer">Source<ExternalLink size={13} /></a>{candidate.feedback_reason && <p className="feedback-note">Review: {candidate.feedback_reason}</p>}<footer><button className="quiet small" onClick={() => setReviewing(candidate)}>{candidate.feedback === 'not_useful' ? <ThumbsDown size={15} /> : <ThumbsUp size={15} />}{candidate.feedback === 'unreviewed' ? 'Review' : candidate.feedback === 'useful' ? 'Useful' : 'Not useful'}</button><button className="quiet small" disabled={busy} onClick={() => { void expand(candidate.id) }}><ArrowUpRight size={15} />Track exact product</button></footer></article>)}</div>}</>}
              {tab === 'activity' && <><div className="activity-tools"><button className="quiet" onClick={() => setImporting(true)}><Upload size={16} />Import result</button><button className="quiet" disabled={busy || current.runs.length === 0} onClick={() => { void action(async () => {
                await request(`/api/watches/${current.watch.id}/preview`, { method: 'POST', body: '{}' }); await refresh(); setNotice('Alert preview saved. No email sent.')
              }) }}><Mail size={16} />Preview alerts</button></div>{current.runs.length === 0 ? <div className="empty"><Activity size={30} /><h3>No collection runs</h3></div> : <div className="run-list">{current.runs.map(run => <article className="run" key={run.id}><div className="run-heading"><span className={`outcome ${run.result.outcome}`}><Activity size={14} />{run.result.outcome.replaceAll('_', ' ')}</span><time>{dateTime(run.created_at)}</time></div><span className="source-label">{sourceName(run.source)} / {run.result.observations.length} observations</span>{run.result.diagnostics && <p>{run.result.diagnostics}</p>}{run.result.observations.filter(observation => observation.kind === 'sale').map((observation, index) => <div className="sale-evidence" key={`${observation.url}-${index}`}><h4>{observation.title}</h4><p>{observation.evidence}</p><a href={observation.url} target="_blank" rel="noreferrer" className="external-link">Source<ExternalLink size={13} /></a></div>)}{run.preview && <div className="preview-results"><h4><Mail size={14} />Dry-run alert preview / {run.preview.sent} sent</h4>{run.preview.decisions.length ? run.preview.decisions.map((decision, index) => <div className="preview-decision" key={`${decision.url}-${index}`}><strong>{decision.decision}</strong><span>{decision.reason}</span></div>) : <p className="muted">No qualifying observations</p>}</div>}</article>)}</div>}</>}
              {tab === 'activity' && <>
                {Boolean(current.jobs?.length) && <section className="section-band"><h3>Check attempts</h3>{current.jobs.map(job => <article className="run" key={job.id}><div className="run-heading"><span className={`outcome ${job.outcome}`}><Clock3 size={14} />{job.outcome.replaceAll('_', ' ')}</span><time>{dateTime(job.started_at)}</time></div>{job.diagnostics && <p>{job.diagnostics}</p>}</article>)}</section>}
                <section className="section-band" aria-label="Email deliveries"><div className="section-heading"><h3>Email deliveries</h3><Mail size={16} /></div>{!current.deliveries?.length ? <p className="muted">No email deliveries</p> : current.deliveries.map(delivery => <article className="run" key={delivery.id}>
                  <div className="run-heading"><span className={`outcome ${delivery.status}`}>{delivery.status}</span><time>{dateTime(delivery.updated_at)}</time></div><h4>{delivery.data.title}</h4><p className="muted">{delivery.recipient} / {delivery.attempts} attempts</p>{delivery.error && <p className="monitoring-error">{delivery.error}</p>}
                  {['failed', 'uncertain'].includes(delivery.status) && <div className="delivery-actions"><button className="quiet small" disabled={busy || health?.email !== 'smtp'} onClick={() => { void resolveMail(delivery, 'retry') }}><RotateCcw size={14} />Retry delivery</button><button className="quiet small" disabled={busy} onClick={() => { void resolveMail(delivery, 'mark_sent') }}><Check size={14} />Mark received</button><button className="icon-button" title="Dismiss delivery" aria-label="Dismiss delivery" disabled={busy} onClick={() => { void resolveMail(delivery, 'dismiss') }}><X size={15} /></button></div>}
                </article>)}</section>
              </>}
            </div>
            <footer className="detail-footer"><span>Updated {dateTime(current.watch.updated_at)}</span><span>v{current.watch.version}</span></footer>
          </>}
        </section>
      </div>
    </main>
    {editor && <WatchEditor initial={editor.initial} source={editor.source} editing={Boolean(editor.watch)} promoting={Boolean(editor.candidateId)} onSave={save} onClose={() => setEditor(null)} />}
    {importing && current && <ImportDialog watch={current.watch} onClose={() => setImporting(false)} onSave={async body => {
      await request(`/api/watches/${current.watch.id}/imports`, { method: 'POST', body }); setImporting(false); setTab('activity'); await refresh(); setNotice('Evidence imported. No email sent.')
    }} />}
    {reviewing && current && <FeedbackDialog candidate={reviewing} onClose={() => setReviewing(null)} onSave={async (feedback, reason) => {
      await request(`/api/watches/${current.watch.id}/candidates/${reviewing.id}`, { method: 'PATCH', body: JSON.stringify({ feedback, reason }) }); setReviewing(null); await refresh(); setNotice('Candidate review saved')
    }} />}
    {connections && <Modal title="Connection status" onClose={() => setConnections(false)}><div className="dialog-body">
      <p className="source-label">{health?.browser === 'playwright_mcp' ? `Playwright MCP / ${health.browser_headless ? 'headless' : 'visible browser'}` : 'HTTP-only collector'}</p>
      <dl className="facts"><div><dt>Harness</dt><dd>pi</dd></div><div><dt>Provider / model</dt><dd>{[health?.pi_provider, health?.pi_model].filter(Boolean).join(' / ') || 'Not configured'}</dd></div><div><dt>Reasoning effort</dt><dd>{health?.pi_thinking || 'Not configured'}</dd></div><div><dt>Worker mode</dt><dd>{health?.scheduler === 'external' ? 'Daily batch / CLI' : 'Disabled'}</dd></div><div><dt>Last worker check-in</dt><dd>{health?.worker_last_seen ? dateTime(health.worker_last_seen) : 'Never'}</dd></div><div><dt>Checks today (UTC)</dt><dd>{health?.checks_today ?? 0}</dd></div><div><dt>Email</dt><dd>{health?.email === 'smtp' ? 'SMTP enabled' : 'Preview only'}</dd></div></dl>
      {Boolean(health?.collector_issues?.length) && <section className="connection-issues"><h3>Collector</h3><ul>{health?.collector_issues.map(issue => <li key={issue}>{issue}</li>)}</ul></section>}
      {Boolean(health?.email_issues?.length) && <section className="connection-issues"><h3>Email</h3><ul>{health?.email_issues.map(issue => <li key={issue}>{issue}</li>)}</ul></section>}
    </div></Modal>}
  </div>
}

export default App
