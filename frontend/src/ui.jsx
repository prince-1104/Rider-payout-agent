import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Activity, AlertTriangle, ArrowDown, ArrowLeft, Check, ChevronRight, Clock3, FileText,
  LayoutDashboard, MessageSquareText, RefreshCw, Search, Send, ShieldAlert, X,
} from 'lucide-react'
import {
  approveDispute, getConversation, getConversations,
  getDashboardData, getTrace, rejectDispute, sendMessage,
} from './api.js'

const money = value => value == null ? '—' : new Intl.NumberFormat('en-IN', {
  style: 'currency', currency: 'INR', maximumFractionDigits: 0,
}).format(value)
const dateTime = value => value ? new Date(value).toLocaleString('en-IN', { dateStyle: 'medium', timeStyle: 'short' }) : '—'
const statusName = value => String(value || 'unknown').replaceAll('_', ' ')

function App() {
  const [page, setPage] = useState('dashboard')
  const [pending, setPending] = useState([])
  const [disputes, setDisputes] = useState([])
  const [conversations, setConversations] = useState([])
  const [conversation, setConversation] = useState(null)
  const [trace, setTrace] = useState([])
  const [traceRider, setTraceRider] = useState('')
  const [traceLoading, setTraceLoading] = useState(false)
  const [loading, setLoading] = useState({ core: true })
  const [error, setError] = useState('')
  const [toast, setToast] = useState(null)
  const [rejectTarget, setRejectTarget] = useState(null)
  const [busyItem, setBusyItem] = useState('')
  const [search, setSearch] = useState('')

  const notify = useCallback((kind, message) => {
    setToast({ kind, message })
    window.setTimeout(() => setToast(current => current?.message === message ? null : current), 4200)
  }, [])
  const refreshCore = useCallback(async ({ initial = false } = {}) => {
    setLoading(current => ({ ...current, core: true }))
    try {
      const [queue, allDisputes, allConversations] = await getDashboardData()
      setPending(queue); setDisputes(allDisputes); setConversations(allConversations); setError('')
      return { queue, allDisputes, allConversations }
    } catch (err) {
      setError(err.message || 'Unable to load operations data.')
      if (!initial) notify('error', err.message || 'Unable to refresh operations data.')
      return null
    } finally { setLoading(current => ({ ...current, core: false })) }
  }, [notify])
  useEffect(() => { refreshCore({ initial: true }) }, [refreshCore])

  const openCase = useCallback(async (conversationId, riderId) => {
    if (!conversationId) { notify('error', 'Conversation details are not available for this case.'); return }
    setLoading(current => ({ ...current, detail: true })); setError('')
    try {
      const [body, events] = await Promise.all([getConversation(conversationId), getTrace(riderId)])
      setConversation(body); setTrace(events); setTraceRider(riderId); setPage('conversation')
    } catch (err) { notify('error', err.message || 'Unable to load conversation.') }
    finally { setLoading(current => ({ ...current, detail: false })) }
  }, [notify])
  const openTrace = useCallback(async riderId => {
    setTraceRider(riderId || ''); setTraceLoading(true); setPage('trace')
    if (!riderId) { setTrace([]); setTraceLoading(false); return }
    try { setTrace(await getTrace(riderId)) }
    catch (err) { notify('error', err.message || 'Unable to load trace.'); setTrace([]) }
    finally { setTraceLoading(false) }
  }, [notify])
  const refreshDetail = useCallback(async (current = conversation) => {
    if (!current) return
    try {
      const [body, events] = await Promise.all([getConversation(current.conversation_id), getTrace(current.rider_id)])
      setConversation(body); setTrace(events); setTraceRider(current.rider_id)
    } catch (err) { notify('error', err.message || 'Unable to refresh the conversation.') }
  }, [conversation, notify])

  const performDecision = async (item, action, reason = '') => {
    setBusyItem(item.id)
    try {
      if (action === 'approve') await approveDispute(item.id)
      else await rejectDispute(item.id, reason)
      setRejectTarget(null)
      notify('success', action === 'approve' ? 'Payout approval processed.' : 'Payout request rejected.')
      await refreshCore()
      await refreshDetail()
    } catch (err) { notify('error', err.message || `Unable to ${action} this item.`) }
    finally { setBusyItem('') }
  }

  const nav = [
    { id: 'dashboard', label: 'Dashboard', icon: LayoutDashboard },
    { id: 'conversations', label: 'Conversations', count: conversations.length, icon: MessageSquareText },
    { id: 'approvals', label: 'Pending approvals', count: pending.filter(row => row.type === 'approval').length, icon: Check },
    { id: 'escalations', label: 'Escalations', count: pending.filter(row => row.type === 'escalation').length, icon: ShieldAlert },
    { id: 'disputes', label: 'All disputes', count: disputes.length, icon: FileText },
  ]
  const go = id => setPage(id)
  const showTraceFor = id => { const row = conversations.find(item => item.conversation_id === id); openTrace(row?.rider_id || conversation?.rider_id || traceRider) }

  return <div className="shell">
    <aside className="sidebar">
      <div className="brand"><div className="brand-mark">Q</div><div><strong>QuickDrop</strong><span>Operations desk</span></div></div>
      <div className="nav-caption">WORKSPACE</div>
      <nav>{nav.map(({ id, label, count, icon: Icon }) => <button key={id} className={`nav-item ${page === id ? 'active' : ''}`} onClick={() => go(id)}><Icon size={18}/><span>{label}</span>{count != null && <b>{count}</b>}</button>)}</nav>
      <div className="nav-caption nav-gap">OBSERVABILITY</div>
      <button className={`nav-item ${page === 'trace' ? 'active' : ''}`} onClick={() => openTrace(traceRider || conversation?.rider_id || '')}><Activity size={18}/><span>Agent trace</span></button>
      <div className="nav-caption nav-gap">TOOLS</div>
      <button className={`nav-item ${page === 'test-agent' ? 'active' : ''}`} onClick={() => go('test-agent')}><Send size={18}/><span>Test agent</span></button>
      <div className="sidebar-foot"><span className="online-dot"/> Internal operations <span className="env-pill">LIVE</span></div>
    </aside>
    <main className="main">
      <header className="topbar"><div><span className="eyebrow">QUICKDROP / OPERATIONS</span><h1>{pageTitle(page)}</h1></div><button className="icon-button" aria-label="Refresh operations data" onClick={() => refreshCore()} disabled={loading.core}><RefreshCw size={17} className={loading.core ? 'spin' : ''}/></button></header>
      <div className="content">
        {error && <div className="notice error-notice" role="alert"><span><AlertTriangle size={16}/>{error}</span><button className="text-button" onClick={() => refreshCore()}>Retry</button><button aria-label="Dismiss error" onClick={() => setError('')}><X size={15}/></button></div>}
        {page === 'dashboard' && <Dashboard pending={pending} disputes={disputes} conversations={conversations} loading={loading.core} openCase={openCase} go={go}/>}
        {page === 'conversations' && <ConversationsPage rows={conversations} loading={loading.core} search={search} setSearch={setSearch} openCase={openCase} go={go}/>}
        {page === 'approvals' && <QueuePage kind="approval" items={pending.filter(row => row.type === 'approval')} loading={loading.core} openCase={openCase} onApprove={item => performDecision(item, 'approve')} onReject={item => setRejectTarget(item)} busyItem={busyItem} go={go}/>}
        {page === 'escalations' && <QueuePage kind="escalation" items={pending.filter(row => row.type === 'escalation')} loading={loading.core} openCase={openCase} busyItem={busyItem} go={go}/>}
        {page === 'disputes' && <DisputesPage rows={disputes} loading={loading.core} openCase={openCase}/>}
        {page === 'conversation' && (loading.detail ? <Loading text="Loading conversation…"/> : conversation && <ConversationPage data={conversation} events={trace} pending={pending} openTrace={showTraceFor} onBack={() => go('conversations')}/>)}
        {page === 'trace' && <TracePage riderId={traceRider} setRiderId={setTraceRider} events={trace} loading={traceLoading} onLoad={() => openTrace(traceRider.trim())}/>}
        {page === 'test-agent' && <TestAgentPage onSent={async riderId => {
          const list = await getConversations({ riderId })
          const row = list[0]
          await refreshCore()
          return row || null
        }} notify={notify} openTrace={openTrace} openCase={openCase}/>}
      </div>
    </main>
    {rejectTarget && <RejectModal item={rejectTarget} busy={busyItem === rejectTarget.id} onCancel={() => setRejectTarget(null)} onSubmit={reason => performDecision(rejectTarget, 'reject', reason)}/>}
    {toast && <div className={`toast ${toast.kind}`} role="status"><span>{toast.kind === 'success' ? <Check size={17}/> : <AlertTriangle size={17}/>} {toast.message}</span><button aria-label="Dismiss notification" onClick={() => setToast(null)}><X size={15}/></button></div>}
  </div>
}

function pageTitle(page) {
  return ({ dashboard: 'Operations overview', conversations: 'Conversations', approvals: 'Pending approvals',
    escalations: 'Escalations', disputes: 'All disputes', conversation: 'Conversation detail', trace: 'Agent trace',
    'test-agent': 'Test agent' })[page] || 'Operations overview'
}

function Dashboard({ pending, disputes, conversations, loading, openCase, go }) {
  const approvals = pending.filter(row => row.type === 'approval').length
  const escalations = pending.filter(row => row.type === 'escalation').length
  const open = disputes.filter(row => row.is_active).length
  const resolved = disputes.filter(row => !row.is_active).length
  const activity = useMemo(() => [
    ...disputes.map(d => ({ id: d.dispute_id, rider_id: d.rider_id, dispute: d.dispute_type,
      status: d.status, amount: d.amount_owed, created_at: d.created_at, conversation_id: d.conversation_id })),
    ...pending.map(item => ({ id: item.id, rider_id: item.rider_id, dispute: item.dispute_type || item.type,
      status: item.type === 'approval' ? 'awaiting_approval' : 'escalation', amount: item.amount,
      created_at: item.created_at, conversation_id: item.conversation_id })),
  ].sort((a, b) => new Date(b.created_at) - new Date(a.created_at)).slice(0, 8), [disputes, pending])
  return <section>
    <div className="section-head"><div><h2>Operations overview</h2><p>Live view of rider payout cases and agent activity.</p></div><button className="approve-button" onClick={() => go('test-agent')}><Send size={15}/>Test agent</button></div>
    {loading ? <Loading text="Loading dashboard…"/> : <>
      <div className="metrics"><Metric label="OPEN DISPUTES" value={open}/><Metric label="PENDING APPROVALS" value={approvals}/><Metric label="ESCALATIONS" value={escalations}/><Metric label="RESOLVED" value={resolved}/></div>
      <div className="section-head recent-head"><div><h2>Recent activity</h2><p>Latest disputes and operations items.</p></div></div>
      {activity.length ? <DataTable headers={['RIDER', 'DISPUTE', 'STATUS', 'AMOUNT', 'CREATED', 'ACTION']} rows={activity.map(row => <tr key={`${row.id}-${row.status}`} onClick={() => openCase(row.conversation_id, row.rider_id)}><td><strong>{row.rider_id}</strong></td><td className="capitalize">{statusName(row.dispute)}</td><td><Badge value={row.status}/></td><td>{money(row.amount)}</td><td>{dateTime(row.created_at)}</td><td><button className="text-button" onClick={event => { event.stopPropagation(); openCase(row.conversation_id, row.rider_id) }}>View case <ChevronRight size={14}/></button></td></tr>)}/> : <Empty title="No activity yet" text="Test the agent with a real rider message to see the workflow here." action="Test agent" onAction={() => go('test-agent')}/>}
      {!!conversations.length && <p className="data-note">Showing {conversations.length} conversation{conversations.length === 1 ? '' : 's'} across the operations desk.</p>}
    </>}
  </section>
}
function Metric({ label, value }) { return <article className="metric-card"><span>{label}</span><strong>{value ?? '—'}</strong></article> }

function ConversationsPage({ rows, loading, search, setSearch, openCase, go }) {
  const [status, setStatus] = useState('')
  const statuses = [...new Set(rows.map(row => row.state?.phase).filter(Boolean))].sort()
  const filtered = rows.filter(row => (!status || row.state?.phase === status) && `${row.rider_id} ${row.latest_message_preview || ''} ${row.state?.phase || ''}`.toLowerCase().includes(search.toLowerCase()))
  return <section><div className="section-head"><div><h2>Rider conversations</h2><p>Operational conversations processed by the agent.</p></div><button className="approve-button" onClick={() => go('test-agent')}><Send size={15}/>Test agent</button></div>
    <div className="conversation-controls"><label className="search-box"><Search size={16}/><input value={search} onChange={event => setSearch(event.target.value)} placeholder="Search rider or latest message" aria-label="Search conversations"/></label><label className="filter-label">Status<select value={status} onChange={event => setStatus(event.target.value)}><option value="">All statuses</option>{statuses.map(value => <option key={value} value={value}>{statusName(value)}</option>)}</select></label></div>
    {loading ? <Loading text="Loading conversations…"/> : filtered.length ? <div className="conversation-grid">{filtered.map(row => <button className="conversation-card" key={row.conversation_id} onClick={() => openCase(row.conversation_id, row.rider_id)}>
      <div className="card-top"><strong>Rider {row.rider_id}</strong><Badge value={row.state?.phase}/></div><p>{row.latest_message_preview || 'No messages recorded yet.'}</p><div className="conversation-meta"><span>{row.active_dispute_count} active dispute{row.active_dispute_count === 1 ? '' : 's'}</span><time>{dateTime(row.updated_at)}</time></div>
    </button>)}</div> : <Empty title="No conversations yet" text="Test the agent to create the first conversation." action="Test agent" onAction={() => go('test-agent')}/>}
  </section>
}

function QueuePage({ kind, items, loading, openCase, onApprove, onReject, busyItem, go }) {
  const approval = kind === 'approval'
  return <section><div className="section-head"><div><h2>{approval ? 'Needs your approval' : 'Escalated cases'}</h2><p>{approval ? 'Review verified payout requests before they are processed.' : 'Cases that need further operations review.'}</p></div><span className="count-chip">{items.length} open</span></div>
    {loading ? <Loading text={`Loading ${approval ? 'approvals' : 'escalations'}…`}/> : items.length ? <div className="cards">{items.map(item => <QueueCard key={item.id} item={item} busy={busyItem === item.id} openCase={() => openCase(item.conversation_id, item.rider_id)} onApprove={() => onApprove(item)} onReject={() => onReject(item)}/>)}</div> : <Empty title={approval ? 'All caught up' : 'No open escalations'} text={approval ? 'There are no pending approvals.' : 'There are no open escalations.'} action={approval ? undefined : 'Test agent'} onAction={approval ? undefined : () => go('test-agent')}/>}
  </section>
}
function QueueCard({ item, openCase, onApprove, onReject, busy }) {
  const [expanded, setExpanded] = useState(false)
  const evidence = item.evidence || {}
  return <article className="queue-card"><div className="card-top"><Badge value={item.type}/><span className="created"><Clock3 size={14}/>{dateTime(item.created_at)}</span></div>
    <div className="card-title-row"><div><h3>{statusName(item.dispute_type || item.type)}</h3><p>Rider <strong>{item.rider_id}</strong></p></div>{item.amount != null && <div className="amount">{money(item.amount)}<small>requested</small></div>}</div>
    <div className="reason"><span>REASON</span><p>{item.reason}</p></div>
    {Object.keys(evidence).length > 0 && <div className="evidence"><button className="evidence-toggle" onClick={() => setExpanded(!expanded)}><FileText size={15}/>{expanded ? 'Hide evidence' : 'View evidence'}<ChevronRight size={15} className={expanded ? 'rotated' : ''}/></button>{expanded && <Evidence data={evidence}/>}</div>}
    <div className="card-actions"><button className="secondary-button" onClick={openCase}>View case</button>{item.type === 'approval' && <><button className="reject-button" disabled={busy} onClick={onReject}>Reject</button><button className="approve-button" disabled={busy} onClick={onApprove}>{busy ? 'Approving…' : <><Check size={15}/>Approve</>}</button></>}</div>
  </article>
}

function DisputesPage({ rows, loading, openCase }) {
  return <section><div className="section-head"><div><h2>All disputes</h2><p>Evidence-backed rider payout claims and their current status.</p></div></div>
    {loading ? <Loading text="Loading disputes…"/> : rows.length ? <DataTable headers={['RIDER', 'DISPUTE', 'STATUS', 'EXPECTED', 'ACTUAL', 'OWED', 'CREATED', 'ACTION']} rows={rows.map(row => <tr key={row.dispute_id} onClick={() => openCase(row.conversation_id, row.rider_id)}><td><strong>{row.rider_id}</strong></td><td className="capitalize">{statusName(row.dispute_type)}</td><td><Badge value={row.status}/></td><td>{money(row.expected_amount)}</td><td>{money(row.actual_amount)}</td><td><strong>{money(row.amount_owed)}</strong></td><td>{dateTime(row.created_at)}</td><td><button className="text-button" onClick={event => { event.stopPropagation(); openCase(row.conversation_id, row.rider_id) }}>Details <ChevronRight size={14}/></button></td></tr>)}/> : <Empty title="No disputes yet" text="Test the agent with an actual rider message to create the first case." action="Test agent"/>}
  </section>
}

function ConversationPage({ data, events, pending, openTrace, onBack }) {
  const decisions = events.filter(event => event.name === 'deterministic_policy_engine')
  const decisionValues = decisions.flatMap(event => Array.isArray(event.output) ? event.output : event.output ? [event.output] : [])
  const caseItems = pending.filter(item => item.conversation_id === data.conversation_id)
  return <div className="conversation-page"><button className="back-button" onClick={onBack}><ArrowLeft size={16}/>All conversations</button>
    <div className="detail-heading"><div><span className="eyebrow">CONVERSATION</span><h2>Rider {data.rider_id}</h2><p>Conversation ID: {data.conversation_id}</p></div><Badge value={data.state?.phase}/></div>
    <div className="case-toolbar"><button className="secondary-button" onClick={() => openTrace(data.rider_id)}><Activity size={15}/>View agent trace</button></div>
    <div className="flow-list">
      <FlowStep icon={<MessageSquareText size={17}/>} title="Conversation">{data.messages?.length ? data.messages.map((message, index) => <div className={`message ${message.role}`} key={`${message.sequence}-${index}`}><div><strong>{message.role === 'rider' ? 'Rider' : 'Assistant'}</strong><time>{dateTime(message.at)}</time></div><p>{message.text}</p></div>) : <p className="muted">No messages recorded.</p>}</FlowStep>
      <FlowStep icon={<FileText size={17}/>} title="Detected disputes">{data.disputes?.length ? data.disputes.map(dispute => <div className="dispute-panel" key={dispute.dispute_id}><div className="dispute-panel-head"><strong className="capitalize">{statusName(dispute.dispute_type)}</strong><Badge value={dispute.status}/></div><p>{dispute.explanation || 'No explanation recorded.'}</p><div className="detail-kpis"><Info label="Amount owed" value={money(dispute.amount_owed)}/><Info label="Expected" value={money(dispute.expected_amount)}/><Info label="Actual" value={money(dispute.actual_amount)}/></div>
          <h4>Investigation evidence</h4><Evidence data={dispute.evidence}/><h4>Calculation</h4><Calculation expected={dispute.expected_amount} actual={dispute.actual_amount} owed={dispute.amount_owed}/>
        </div>) : <p className="muted">No dispute has been recorded for this conversation.</p>}</FlowStep>
      <FlowStep icon={<ShieldAlert size={17}/>} title="Deterministic policy decision">{decisionValues.length ? decisionValues.map((decision, index) => <div className="decision-row" key={index}><Badge value={decision.action || decision.decision || decision.status}/><p>{decision.reason || 'Reason recorded in backend policy output.'}</p>{decision.amount != null && <strong>{money(decision.amount)}</strong>}</div>) : <p className="muted">No policy decision is recorded in the available trace events.</p>}<small className="authority-note">Financial authorization and amounts are determined by the backend policy engine.</small></FlowStep>
      <FlowStep icon={<Activity size={17}/>} title="Payment / approval / escalation">{caseItems.length ? caseItems.map(item => <div className="decision-row" key={item.id}><Badge value={item.type === 'approval' ? 'awaiting_approval' : 'escalation'}/><p>{item.reason}</p>{item.amount != null && <strong>{money(item.amount)}</strong>}</div>) : <ResolutionSummary events={events} disputes={data.disputes || []}/>}</FlowStep>
      <FlowStep icon={<Activity size={17}/>} title="Agent trace"><TraceTimeline events={events}/></FlowStep>
    </div>
  </div>
}
function ResolutionSummary({ events, disputes }) {
  const payments = events.filter(event => /payswift|payment|payout/i.test(event.name))
  if (payments.length) return <TraceTimeline events={payments}/>
  if (disputes.some(dispute => !dispute.is_active)) return <p className="step-body">This dispute is no longer active. See the trace for its recorded resolution events.</p>
  return <p className="muted">No payment, approval, or escalation outcome is recorded for this conversation yet.</p>
}
function Calculation({ expected, actual, owed }) { return <div className="calculation"><div><span>Expected payout</span><strong>{money(expected)}</strong></div><div><span>Actual payout</span><strong>{money(actual)}</strong></div><div className="owed"><span>Amount owed</span><strong>{money(owed)}</strong></div></div> }
function Evidence({ data }) {
  const entries = Object.entries(data || {})
  return entries.length ? <div className="evidence-view"><dl>{entries.map(([key, value]) => <div key={key}><dt>{key.replaceAll('_', ' ')}</dt><dd>{typeof value === 'object' && value !== null ? JSON.stringify(value) : String(value)}</dd></div>)}</dl><details><summary>Raw evidence</summary><pre className="json-view">{JSON.stringify(data, null, 2)}</pre></details></div> : <p className="muted">No evidence fields were returned.</p>
}

function TracePage({ riderId, setRiderId, events, loading, onLoad }) {
  return <section><div className="section-head"><div><h2>Agent execution trace</h2><p>Operational events recorded for a rider. Private reasoning and prompts are not shown.</p></div></div>
    <form className="trace-search" onSubmit={event => { event.preventDefault(); onLoad() }}><label htmlFor="trace-rider">Rider ID</label><input id="trace-rider" value={riderId} onChange={event => setRiderId(event.target.value)} placeholder="e.g. R003"/><button className="approve-button" type="submit" disabled={!riderId.trim() || loading}>{loading ? 'Loading…' : 'Load trace'}</button></form>
    {loading ? <Loading text="Loading trace…"/> : riderId ? <TraceTimeline events={events}/> : <Empty title="Select a rider" text="Enter a rider ID to inspect recorded agent operations."/>}
  </section>
}

function TestAgentPage({ onSent, notify, openTrace, openCase }) {
  const [riderId, setRiderId] = useState('R003')
  const [text, setText] = useState('')
  const [reply, setReply] = useState('')
  const [sentRider, setSentRider] = useState('')
  const [sentConversation, setSentConversation] = useState(null)
  const [sending, setSending] = useState(false)
  const [receivedAt, setReceivedAt] = useState(() => new Date().toISOString().slice(0, 16))
  const scenarios = [
    ['Valid payout dispute', 'Bhai payout galat aaya hai'],
    ['Missing surge', '20 tarikh ke order ka surge nahi mila'],
    ['Large dispute', 'Mera payout bahut kam aaya hai'],
  ]
  const submit = async event => {
    event.preventDefault()
    const rider = riderId.trim().toUpperCase()
    if (!rider || !text.trim()) return
    setSending(true); setReply(''); setSentConversation(null)
    try {
      const result = await sendMessage({ message_id: crypto.randomUUID(), rider_id: rider, text: text.trim(), received_at: new Date(receivedAt).toISOString() })
      setReply(result.reply); setSentRider(rider); notify('success', 'Message processed by the backend agent workflow.')
      setSentConversation(await onSent(rider))
    } catch (err) { notify('error', err.message || 'Message could not be processed.') }
    finally { setSending(false) }
  }
  return <section className="test-agent-layout"><div className="test-agent-intro"><span className="eyebrow">INTERNAL DEMO TOOL</span><h2>Test the payout agent</h2><p>Send a real message through <code>POST /messages</code>. The response and investigation are produced by the running backend.</p></div>
    <form className="test-form" onSubmit={submit}><label htmlFor="rider-id">Rider ID</label><input id="rider-id" value={riderId} onChange={event => setRiderId(event.target.value)} maxLength={32} required/>
      <label>Suggested message</label><div className="scenario-list">{scenarios.map(([label, example]) => <button type="button" key={label} className="scenario" onClick={() => setText(example)}><strong>{label}</strong><span>{example}</span></button>)}</div>
      <label htmlFor="message-text">Message</label><textarea id="message-text" rows={4} value={text} onChange={event => setText(event.target.value)} maxLength={4096} placeholder="Type a rider message…" required/>
      <label htmlFor="received-at">Received at</label><input id="received-at" type="datetime-local" value={receivedAt} onChange={event => setReceivedAt(event.target.value)} required/>
      <div className="form-actions"><span>Uses real agent, evidence, policy and payment services.</span><button type="submit" className="approve-button" disabled={sending || !text.trim()}>{sending ? 'Agent is investigating…' : <><Send size={15}/>Send message</>}</button></div>
    </form>
    {reply && <article className="agent-reply"><span className="eyebrow">AGENT RESPONSE</span><p>{reply}</p><div>{sentConversation && <button className="secondary-button" onClick={() => openCase(sentConversation.conversation_id, sentRider)}>View conversation</button>}<button className="secondary-button" onClick={() => openTrace(sentRider)}><Activity size={15}/>View trace</button></div></article>}
  </section>
}

function RejectModal({ item, busy, onCancel, onSubmit }) {
  const [reason, setReason] = useState('')
  const [error, setError] = useState('')
  return <div className="modal-backdrop" role="presentation"><form className="modal" role="dialog" aria-modal="true" aria-labelledby="reject-title" onSubmit={event => { event.preventDefault(); if (!reason.trim()) { setError('Enter a reason for rejection.'); return }; onSubmit(reason.trim()) }}>
    <button className="modal-close" type="button" onClick={onCancel} aria-label="Close"><X size={17}/></button><span className="eyebrow">OPS DECISION</span><h2 id="reject-title">Reject payout request</h2><p>Rider {item.rider_id} · {item.dispute_type || 'dispute'} · {money(item.amount)}</p><label htmlFor="reject-reason">Reason for rejection</label><textarea id="reject-reason" rows={4} maxLength={1000} value={reason} onChange={event => { setReason(event.target.value); setError('') }} required/>{error && <span className="field-error">{error}</span>}
    <div className="modal-actions"><button className="secondary-button" type="button" onClick={onCancel} disabled={busy}>Cancel</button><button className="reject-button" type="submit" disabled={busy}>{busy ? 'Rejecting…' : 'Reject payout'}</button></div>
  </form></div>
}

function DataTable({ headers, rows }) { return <div className="table-wrap"><table><thead><tr>{headers.map(header => <th key={header}>{header}</th>)}</tr></thead><tbody>{rows}</tbody></table></div> }
function Badge({ value }) { return <span className={`status ${String(value || 'unknown').toLowerCase().replaceAll('_', '-')}`}>{statusName(value)}</span> }
function Empty({ title, text, action, onAction }) { return <div className="empty"><div><Check size={21}/></div><h3>{title}</h3><p>{text}</p>{action && <button className="secondary-button" onClick={onAction}>{action}</button>}</div> }
function Loading({ text }) { return <div className="loading-state"><span className="spinner"/>{text}</div> }
function Info({ label, value }) { return <div><span>{label}</span><strong>{value ?? '—'}</strong></div> }
function FlowStep({ icon, title, children }) { return <section className="flow-step"><div className="flow-marker">{icon}</div><div className="flow-content"><h3>{title}</h3>{children}</div><div className="flow-arrow"><ArrowDown size={15}/></div></section> }
function TraceTimeline({ events }) { return <div className="trace-list">{events?.length ? events.map((event, index) => <article className="trace-event" key={`${event.at}-${event.name}-${index}`}><span className="trace-dot"/><div className="trace-head"><strong>{statusName(event.name)}</strong><time>{dateTime(event.at)}</time></div><span className={`trace-type ${event.type}`}>{statusName(event.type)}</span>{(event.input || event.output) && <details><summary>Input and output</summary><div className="trace-payload"><pre className="json-view">{JSON.stringify({ input: event.input, output: event.output }, null, 2)}</pre></div></details>}</article>) : <p className="muted">No trace events recorded for this rider.</p>}</div> }

export default App
