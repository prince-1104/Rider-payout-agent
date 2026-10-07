import { useCallback, useEffect, useMemo, useState } from 'react'
import { Activity, ArrowDown, ArrowLeft, Check, ChevronRight, Clock3, FileText, LayoutDashboard, MessageSquareText, RefreshCw, ShieldAlert, X } from 'lucide-react'

const api = async (path, options) => {
  const response = await fetch(`/api${path}`, { headers: { 'Content-Type': 'application/json' }, ...options })
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(body.detail || `Request failed (${response.status})`)
  }
  return response.status === 204 ? null : response.json()
}
const money = n => n == null ? '—' : new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR', maximumFractionDigits: 0 }).format(n)
const dateTime = value => value ? new Date(value).toLocaleString('en-IN', { dateStyle: 'medium', timeStyle: 'short' }) : '—'

function App() {
  const [items, setItems] = useState([])
  const [disputes, setDisputes] = useState([])
  const [page, setPage] = useState('approvals')
  const [selected, setSelected] = useState(null)
  const [detail, setDetail] = useState(null)
  const [trace, setTrace] = useState([])
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const load = useCallback(async () => {
    setBusy(true); setNotice('')
    try {
      const [queue, allDisputes] = await Promise.all([api('/ops/pending'), api('/ops/disputes')])
      setItems(queue); setDisputes(allDisputes)
    } catch (error) { setNotice(error.message) } finally { setBusy(false) }
  }, [])
  useEffect(() => { load() }, [load])
  const approvals = items.filter(x => x.type === 'approval')
  const escalations = items.filter(x => x.type === 'escalation')
  const openConversation = async (item) => {
    const dispute = disputes.find(d => d.dispute_id === item.dispute_id) || (item.dispute_id ? await api(`/ops/disputes/${item.dispute_id}`).catch(() => null) : null)
    const conversationId = dispute?.conversation_id || item.conversation_id
    if (!conversationId) { setNotice('Conversation details are not available for this queue item.'); return }
    setBusy(true)
    try {
      const [conversation, timeline] = await Promise.all([
        api(`/ops/conversations/${conversationId}`), api(`/trace/${encodeURIComponent(item.rider_id)}`),
      ])
      setSelected(item); setDetail(conversation); setTrace(timeline); setPage('conversation')
    } catch (error) { setNotice(error.message) } finally { setBusy(false) }
  }
  const chooseDispute = async (dispute) => openConversation({ rider_id: dispute.rider_id, dispute_id: dispute.dispute_id, type: 'review' })
  const decide = async (item, action) => {
    let reason = ''
    if (action === 'reject') { reason = window.prompt('Reason for rejection (required):') || ''; if (!reason.trim()) return }
    setBusy(true); setNotice('')
    try {
      await api(`/ops/disputes/${encodeURIComponent(item.id)}/${action}`, action === 'reject'
        ? { method: 'POST', body: JSON.stringify({ reason }) } : { method: 'POST' })
      setNotice(action === 'approve' ? 'Approval processed by the server.' : 'Item rejected and recorded.')
      await load()
    } catch (error) { setNotice(error.message) } finally { setBusy(false) }
  }
  const nav = [
    { id: 'approvals', label: 'Pending approvals', count: approvals.length, icon: Check },
    { id: 'escalations', label: 'Escalations', count: escalations.length, icon: ShieldAlert },
    { id: 'disputes', label: 'All disputes', count: disputes.length, icon: FileText },
  ]
  return <div className="shell">
    <aside className="sidebar">
      <div className="brand"><div className="brand-mark">Q</div><div><strong>QuickDrop</strong><span>Operations desk</span></div></div>
      <div className="nav-caption">WORKSPACE</div>
      <nav>{nav.map(({ id, label, count, icon: Icon }) => <button key={id} className={`nav-item ${page === id ? 'active' : ''}`} onClick={() => setPage(id)}><Icon size={18}/><span>{label}</span><b>{count}</b></button>)}</nav>
      <div className="sidebar-foot"><span className="online-dot"/> Internal operations <span className="env-pill">LIVE</span></div>
    </aside>
    <main className="main">
      <header className="topbar"><div><span className="eyebrow">QUICKDROP / OPERATIONS</span><h1>{page === 'conversation' ? 'Conversation detail' : nav.find(n => n.id === page)?.label || 'Operations'}</h1></div><button className="icon-button" onClick={load} title="Refresh" disabled={busy}><RefreshCw size={17} className={busy ? 'spin' : ''}/></button></header>
      <div className="content">
        {notice && <div className="notice" role="status">{notice}<button onClick={() => setNotice('')}><X size={15}/></button></div>}
        {page === 'conversation' && detail ? <Conversation detail={detail} trace={trace} selected={selected} onBack={() => setPage(selected?.type === 'escalation' ? 'escalations' : 'approvals')} /> :
          page === 'disputes' ? <section><div className="section-head"><div><h2>Disputes</h2><p>Review evidence, calculations, and decisions.</p></div></div><div className="table-wrap"><table><thead><tr><th>RIDER</th><th>DISPUTE</th><th>STATUS</th><th>AMOUNT OWED</th><th>CREATED</th><th/></tr></thead><tbody>{disputes.map(d => <tr key={d.dispute_id}><td><strong>{d.rider_id}</strong><small>{d.conversation_id.slice(0, 8)}</small></td><td>{d.dispute_type}</td><td><span className={`status ${d.status}`}>{d.status.replaceAll('_', ' ')}</span></td><td>{money(d.amount_owed)}</td><td>{dateTime(d.created_at)}</td><td><button className="text-button" onClick={() => chooseDispute(d)}>Details <ChevronRight size={15}/></button></td></tr>)}</tbody></table>{disputes.length === 0 && <Empty title="No disputes yet" text="Disputes will appear here as rider messages are reviewed."/>}</div></section> :
          <section><div className="section-head"><div><h2>{page === 'approvals' ? 'Needs your approval' : 'Needs investigation'}</h2><p>{page === 'approvals' ? 'Review verified payout requests before they are processed.' : 'Escalated cases that need an operations review.'}</p></div><span className="count-chip">{page === 'approvals' ? approvals.length : escalations.length} open</span></div>
            <div className="cards">{(page === 'approvals' ? approvals : escalations).map(item => <QueueCard key={item.id} item={item} onOpen={() => openConversation(item)} onDecide={action => decide(item, action)}/>)}</div>
            {(page === 'approvals' ? approvals : escalations).length === 0 && <Empty title={page === 'approvals' ? 'All caught up' : 'No escalations'} text={page === 'approvals' ? 'There are no pending approvals right now.' : 'There are no open escalations right now.'}/>}</section>}
      </div>
    </main>
  </div>
}

function QueueCard({ item, onOpen, onDecide }) {
  const [expanded, setExpanded] = useState(false)
  return <article className="queue-card">
    <div className="card-top"><span className={`type-pill ${item.type}`}>{item.type === 'approval' ? 'Approval request' : 'Escalation'}</span><span className="created"><Clock3 size={14}/>{dateTime(item.created_at)}</span></div>
    <div className="card-title-row"><div><h3>{item.dispute_type?.replaceAll('_', ' ') || 'Dispute review'}</h3><p>Rider <strong>{item.rider_id}</strong></p></div>{item.amount != null && <div className="amount">{money(item.amount)}<small>requested</small></div>}</div>
    <div className="reason"><span>REASON</span><p>{item.reason}</p></div>
    {!!Object.keys(item.evidence || {}).length && <div className="evidence"><button className="evidence-toggle" onClick={() => setExpanded(!expanded)}><FileText size={15}/>{expanded ? 'Hide evidence' : 'View evidence'}<ChevronRight size={15} className={expanded ? 'rotated' : ''}/></button>{expanded && <pre>{JSON.stringify(item.evidence, null, 2)}</pre>}</div>}
    <div className="card-actions"><button className="secondary-button" onClick={onOpen}><MessageSquareText size={16}/>Conversation details</button>{item.type === 'approval' && <><button className="reject-button" onClick={() => onDecide('reject')}>Reject</button><button className="approve-button" onClick={() => onDecide('approve')}><Check size={16}/>Approve</button></>}</div>
  </article>
}

function Conversation({ detail, trace, selected, onBack }) {
  const flow = useMemo(() => {
    const items = []
    for (const dispute of detail.disputes || []) {
      items.push({ kind: 'dispute', title: `Detected dispute · ${dispute.dispute_type.replaceAll('_', ' ')}`, body: dispute.explanation, data: dispute })
      items.push({ kind: 'evidence', title: 'Investigation evidence', data: dispute.evidence })
      items.push({ kind: 'calculation', title: 'Calculation', body: `${money(dispute.expected_amount)} expected − ${money(dispute.actual_amount)} paid = ${money(dispute.amount_owed)} owed`, data: { status: dispute.status, amount_owed: dispute.amount_owed } })
      items.push({ kind: 'policy', title: 'Policy decision', body: dispute.status.replaceAll('_', ' '), data: dispute })
    }
    if (selected) items.push({ kind: 'action', title: 'Payment / approval / escalation', body: `${selected.type} · ${selected.reason}`, data: selected })
    return items
  }, [detail, selected])
  return <div className="conversation-page"><button className="back-button" onClick={onBack}><ArrowLeft size={16}/>Back to queue</button>
    <div className="detail-heading"><div><span className="eyebrow">CONVERSATION</span><h2>Rider {detail.rider_id}</h2><p>Conversation {detail.conversation_id}</p></div><span className="state-pill">{detail.state?.phase?.replaceAll('_', ' ') || 'open'}</span></div>
    <div className="flow-list">
      <FlowStep icon={<LayoutDashboard size={17}/>} title="Rider"><div className="info-grid"><Info label="Rider ID" value={detail.rider_id}/><Info label="Conversation ID" value={detail.conversation_id}/><Info label="Current state" value={detail.state?.phase || '—'}/></div></FlowStep>
      <FlowStep icon={<MessageSquareText size={17}/>} title="Messages">{(detail.messages || []).map((message, i) => <div className={`message ${message.role}`} key={`${message.sequence}-${i}`}><div><strong>{message.role === 'rider' ? 'Rider' : 'Assistant'}</strong><time>{dateTime(message.at)}</time></div><p>{message.text}</p></div>)}{!detail.messages?.length && <p className="muted">No messages recorded.</p>}</FlowStep>
      {flow.map((step, i) => <FlowStep key={`${step.kind}-${i}`} icon={step.kind === 'action' ? <Activity size={17}/> : <FileText size={17}/>} title={step.title}>
        {step.body && <p className="step-body">{step.body}</p>}{step.kind === 'evidence' ? <JsonView value={step.data}/> : step.kind === 'calculation' ? <JsonView value={step.data}/> : null}
      </FlowStep>)}
      <FlowStep icon={<Activity size={17}/>} title="Trace timeline"><TraceTimeline events={trace}/></FlowStep>
    </div>
  </div>
}
function Info({ label, value }) { return <div><span>{label}</span><strong>{value}</strong></div> }
function FlowStep({ icon, title, children }) { return <section className="flow-step"><div className="flow-marker">{icon}</div><div className="flow-content"><h3>{title}</h3>{children}</div><div className="flow-arrow"><ArrowDown size={15}/></div></section> }
function JsonView({ value }) { return <pre className="json-view">{JSON.stringify(value || {}, null, 2)}</pre> }
function TraceTimeline({ events }) { return <div className="trace-list">{events?.length ? events.map((event, i) => <article className="trace-event" key={`${event.at}-${i}`}><span className="trace-dot"/><div className="trace-head"><strong>{event.name.replaceAll('_', ' ')}</strong><time>{dateTime(event.at)}</time></div><span className={`trace-type ${event.type}`}>{event.type.replaceAll('_', ' ')}</span>{(event.input || event.output) && <details><summary>Event data</summary><div className="trace-payload"><JsonView value={{ input: event.input, output: event.output }}/></div></details>}</article>) : <p className="muted">No trace events recorded.</p>}</div> }
function Empty({ title, text }) { return <div className="empty"><div><Check size={21}/></div><h3>{title}</h3><p>{text}</p></div> }

export default App
