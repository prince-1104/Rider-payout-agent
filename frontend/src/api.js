const BASE = '/api'

async function request(path, options = {}) {
  let response
  try {
    response = await fetch(`${BASE}${path}`, {
      ...options,
      headers: { 'Content-Type': 'application/json', ...options.headers },
    })
  } catch {
    throw new Error('The API is unavailable. Check that the project services are running and retry.')
  }
  let body = null
  try { body = await response.json() } catch { /* Empty response body. */ }
  if (!response.ok) {
    const message = typeof body?.detail === 'string' ? body.detail : `Request failed (${response.status}).`
    throw new Error(message)
  }
  return body
}

const id = value => encodeURIComponent(value)
export const getDashboardData = () => Promise.all([getPending(), getDisputes(), getConversations()])
export const getConversations = ({ riderId, status } = {}) => {
  const query = new URLSearchParams()
  if (riderId) query.set('rider_id', riderId)
  if (status) query.set('status', status)
  return request(`/ops/conversations${query.size ? `?${query}` : ''}`)
}
export const getConversation = conversationId => request(`/ops/conversations/${id(conversationId)}`)
export const getConversationByRider = async riderId => (await getConversations({ riderId }))[0] || null
export const getPending = () => request('/ops/pending')
export const getEscalations = async () => (await getPending()).filter(item => item.type === 'escalation')
export const getDisputes = () => request('/ops/disputes')
export const getDispute = disputeId => request(`/ops/disputes/${id(disputeId)}`)
export const getTrace = riderId => request(`/trace/${id(riderId)}`)
export const sendMessage = payload => request('/messages', { method: 'POST', body: JSON.stringify(payload) })
export const approveDispute = queueOrDisputeId => request(`/ops/disputes/${id(queueOrDisputeId)}/approve`, { method: 'POST' })
export const rejectDispute = (queueOrDisputeId, reason) => request(`/ops/disputes/${id(queueOrDisputeId)}/reject`, {
  method: 'POST', body: JSON.stringify({ reason }),
})
