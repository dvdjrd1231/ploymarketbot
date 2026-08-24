/** Thin REST client. Used for *actions* only — all reads arrive over the socket. */

export class ApiError extends Error {}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(`/api${path}`, {
      headers: { 'Content-Type': 'application/json' },
      ...init,
    })
  } catch {
    throw new ApiError('Cannot reach the local server. Is it still running?')
  }
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`
    try {
      const body = await response.json()
      if (body?.detail) {
        detail = Array.isArray(body.detail)
          ? body.detail.map((d: any) => d.msg ?? String(d)).join(' ')
          : String(body.detail)
      }
    } catch { /* keep the status line */ }
    throw new ApiError(detail)
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: 'POST', body: JSON.stringify(body ?? {}) })

export const api = {
  snapshot: () => request<any>('/snapshot'),
  status: () => request<any>('/status'),

  saveSettings: (patch: Record<string, unknown>) =>
    request<any>('/settings', { method: 'PUT', body: JSON.stringify(patch) }),

  saveKey: (privateKey: string, funderHint = '') =>
    post<any>('/credentials', { private_key: privateKey, funder_hint: funderHint }),
  clearKey: () => request<any>('/credentials', { method: 'DELETE' }),
  connect: (privateKey = '', funderHint = '') =>
    post<any>('/connect', { private_key: privateKey, funder_hint: funderHint }),

  start: () => post<any>('/engine/start'),
  stop: () => post<any>('/engine/stop'),
  restart: () => post<any>('/engine/restart'),

  closePosition: (id: number) => post<any>('/positions/close', { id }),
  cancelOrder: (orderId: string) => post<any>('/orders/cancel', { order_id: orderId }),

  resetSession: () => post<any>('/session/reset'),
  clearLogs: () => request<any>('/logs', { method: 'DELETE' }),
  targetActivity: (limit = 40) =>
    request<any[]>(`/target/activity?limit=${limit}`),
}
