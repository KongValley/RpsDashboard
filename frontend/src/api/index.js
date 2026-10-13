/**
 * API 层。
 *
 * 统一使用原生 fetch 而非 axios：本机实测 axios 在浏览器里有请求**长期
 * pending** 的情况（排行榜整页停在「加载中」，status 停在「连接中」），
 * 而同一地址用 fetch / 浏览器直接访问都正常。fetch 无此问题，故全量改用。
 *
 * 这里是全前端唯一的网络出口，因此性能埋点（perf.js）也挂在这三个函数上，
 * 一处覆盖全部接口。
 */

import { record } from '../perf'

/** 服务端处理耗时（后端中间件下发的响应头）；拿不到返回 undefined */
function serverMs(res) {
  const v = Number(res?.headers?.get('x-perf-ms'))
  return Number.isFinite(v) ? v : undefined
}

/** 毫秒取一位小数：监测数据不需要更多精度 */
const since = (t0) => Math.round((performance.now() - t0) * 10) / 10

/** 网络层失败（fetch reject）与 HTTP 错误的区分，供埋点标注 */
const errLabel = (e) => (e?.name === 'TypeError' ? 'network' : e?.message || String(e))

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
  const t0 = performance.now()
  let res
  let error
  try {
    res = await fetch(url, { headers: { Accept: 'application/json' } })
    if (!res.ok) throw new Error(`HTTP ${res.status} ${url}`)
    return await res.json()
  } catch (e) {
    error = errLabel(e)
    throw e
  } finally {
    record({
      path: url, ms: since(t0), ok: res?.ok ?? false, status: res?.status ?? 0,
      server_ms: serverMs(res), error,
    })
  }
}

const get = (path, params) => request(path, params)

/** 触发浏览器下载（导出 CSV 等） */
async function download(path, params, fallbackName = 'export.csv') {
  const url = `/api${path}${qs(params)}`
  const t0 = performance.now()
  let res
  let error
  let size
  try {
    res = await fetch(url, { headers: { Accept: 'text/csv' } })
    if (!res.ok) throw new Error(`HTTP ${res.status} ${url}`)
    const blob = await res.blob()
    size = blob.size
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = fallbackName
    a.click()
    URL.revokeObjectURL(a.href)
  } catch (e) {
    error = errLabel(e)
    throw e
  } finally {
    record({
      path: url, ms: since(t0), ok: res?.ok ?? false, status: res?.status ?? 0,
      server_ms: serverMs(res), bytes: size, error,
    })
  }
}

async function post(path, body) {
  const url = `/api${path}`
  const t0 = performance.now()
  let res
  let error
  try {
    res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    if (!res.ok) throw new Error(`HTTP ${res.status} ${path}`)
    return await res.json()
  } catch (e) {
    error = errLabel(e)
    throw e
  } finally {
    record({
      path: url, ms: since(t0), ok: res?.ok ?? false, status: res?.status ?? 0,
      server_ms: serverMs(res), error,
    })
  }
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
  patterns: (symbol, days = 250) => get(`/stock/${symbol}/patterns`, { days }),
}

export const backtestApi = {
  run: (params) => get('/backtest/run', params),
}

export const adminApi = {
  update: () => post('/admin/update', {}),
  updateStatus: () => get('/admin/update/status'),
}

export default { get, post, download }
