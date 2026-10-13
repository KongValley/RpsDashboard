/**
 * API 层。
 *
 * 统一使用原生 fetch 而非 axios：本机实测 axios 在浏览器里有请求**长期
 * pending** 的情况（排行榜整页停在「加载中」，status 停在「连接中」），
 * 而同一地址用 fetch / 浏览器直接访问都正常。fetch 无此问题，故全量改用。
 */

/** 构造查询串，跳过 undefined / null / 空串 */
function qs(params = {}) {
  const sp = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== '') sp.append(k, v)
  }
  const s = sp.toString()
  return s ? `?${s}` : ''
}

async function request(path, params) {
  const url = `/api${path}${qs(params)}`
  const res = await fetch(url, { headers: { Accept: 'application/json' } })
  if (!res.ok) throw new Error(`HTTP ${res.status} ${url}`)
  return res.json()
}

const get = (path, params) => request(path, params)

/** 触发浏览器下载（导出 CSV 等） */
async function download(path, params, fallbackName = 'export.csv') {
  const url = `/api${path}${qs(params)}`
  const res = await fetch(url, { headers: { Accept: 'text/csv' } })
  if (!res.ok) throw new Error(`HTTP ${res.status} ${url}`)
  const blob = await res.blob()
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = fallbackName
  a.click()
  URL.revokeObjectURL(a.href)
}

async function post(path, body) {
  const res = await fetch(`/api${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error(`HTTP ${res.status} ${path}`)
  return res.json()
}

export const metaApi = {
  status: () => get('/meta/status'),
  dates: () => get('/meta/dates'),
  industries: () => get('/meta/industries'),
  periods: () => get('/meta/periods'),
}

export const rpsApi = {
  ranking: (params) => get('/rps/ranking', params),
  exportRanking: (params, name) => download('/rps/ranking/export', params, name),
  sector: (params) => get('/rps/sector', params),
  heatmap: (params) => get('/rps/heatmap', params),
  concepts: () => get('/rps/concepts'),
  conceptRanking: (params) => get('/rps/concept-ranking', params),
  conceptHeatmap: (params) => get('/rps/concept-heatmap', params),
}
export const stockApi = {
  detail: (symbol) => get(`/stock/${symbol}`),
  curve: (symbol, days = 250) => get(`/stock/${symbol}/curve`, { days }),
}

export const backtestApi = {
  run: (params) => get('/backtest/run', params),
}

export const adminApi = {
  update: () => post('/admin/update', {}),
  updateStatus: () => get('/admin/update/status'),
}

export default { get, post, download }
