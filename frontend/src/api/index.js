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

/**
 * 默认超时。浏览器 fetch **没有**超时：连接被中间设备吞掉时会一直 pending，
 * 页面就永久停在「加载中」。回测是同步算的（本机实测数秒，大库更久），
 * 单独放宽到 3 分钟。
 */
const TIMEOUT_MS = 30_000
const BACKTEST_TIMEOUT_MS = 180_000

/** AbortController + 定时器；调用方负责 clear()，否则定时器会拖住事件循环 */
function timeoutGuard(ms) {
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), ms)
  return { signal: ctrl.signal, clear: () => clearTimeout(timer) }
}

const timeoutError = (ms, url) =>
  new Error(`请求超时（${Math.round(ms / 1000)} 秒）：${url}`)

/** 构造查询串，跳过 undefined / null / 空串 */
function qs(params = {}) {
  const sp = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== '') sp.append(k, v)
  }
  const s = sp.toString()
  return s ? `?${s}` : ''
}

async function request(path, params, init, timeoutMs = TIMEOUT_MS) {
  const url = `/api${path}${qs(params)}`
  const t0 = performance.now()
  const guard = timeoutGuard(timeoutMs)
  let res
  let error
  try {
    res = await fetch(url, {
      headers: { Accept: 'application/json' },
      ...(init || {}),
      signal: guard.signal,
    })
    if (!res.ok) throw new Error(`HTTP ${res.status} ${url}`)
    return await res.json()
  } catch (e) {
    if (e?.name === 'AbortError') {
      error = `timeout ${timeoutMs}ms`
      throw timeoutError(timeoutMs, url)
    }
    error = errLabel(e)
    throw e
  } finally {
    guard.clear()
    record({
      path: url, ms: since(t0), ok: res?.ok ?? false, status: res?.status ?? 0,
      server_ms: serverMs(res), error,
    })
  }
}

const get = (path, params, timeoutMs) => request(path, params, null, timeoutMs)

/** 触发浏览器下载（导出 CSV 等） */
async function download(path, params, fallbackName = 'export.csv') {
  const url = `/api${path}${qs(params)}`
  const t0 = performance.now()
  const guard = timeoutGuard(TIMEOUT_MS)
  let res
  let error
  let size
  try {
    res = await fetch(url, { headers: { Accept: 'text/csv' }, signal: guard.signal })
    if (!res.ok) throw new Error(`HTTP ${res.status} ${url}`)
    const blob = await res.blob()
    size = blob.size
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = fallbackName
    a.click()
    URL.revokeObjectURL(a.href)
  } catch (e) {
    if (e?.name === 'AbortError') {
      error = `timeout ${TIMEOUT_MS}ms`
      throw timeoutError(TIMEOUT_MS, url)
    }
    error = errLabel(e)
    throw e
  } finally {
    guard.clear()
    record({
      path: url, ms: since(t0), ok: res?.ok ?? false, status: res?.status ?? 0,
      server_ms: serverMs(res), bytes: size, error,
    })
  }
}

async function post(path, body, timeoutMs = TIMEOUT_MS) {
  const url = `/api${path}`
  const t0 = performance.now()
  const guard = timeoutGuard(timeoutMs)
  let res
  let error
  try {
    res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: guard.signal,
    })
    if (!res.ok) throw new Error(`HTTP ${res.status} ${path}`)
    return await res.json()
  } catch (e) {
    if (e?.name === 'AbortError') {
      error = `timeout ${timeoutMs}ms`
      throw timeoutError(timeoutMs, url)
    }
    error = errLabel(e)
    throw e
  } finally {
    guard.clear()
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
  // 回测是同步算的（大库上可达数十秒），单独放宽超时；
  // 合成分还要多走一遍全市场因子矩阵，调用方可再放宽（见 Backtest.vue 的 signal）
  run: (params, timeoutMs) => get('/backtest/run', params, timeoutMs ?? BACKTEST_TIMEOUT_MS),
}

export const adminApi = {
  update: () => post('/admin/update', {}),
  updateStatus: () => get('/admin/update/status'),
}

/** 自选股池（全 API 唯一的写入面，见 backend/rpsboard/api/watchlist.py） */
export const watchApi = {
  list: () => get('/watchlist'),
  add: (symbol, note) => post('/watchlist', { symbol, note }),
  remove: (symbol) => request(`/watchlist/${symbol}`, undefined, { method: 'DELETE' }),
}

export const alertApi = {
  kinds: () => get('/alerts/kinds'),
  rules: () => get('/alerts/rules'),
  addRule: (rule) => post('/alerts/rules', rule),
  removeRule: (id) => request(`/alerts/rules/${id}`, undefined, { method: 'DELETE' }),
  hits: (params) => get('/alerts/hits', params),
}

/** QMT 交易（只读库 + 两个写入口；summary 不连客户端，见 api/trade.py） */
export const tradeApi = {
  summary: () => get('/trade/summary'),
  positions: () => get('/trade/positions'),
  orders: (params) => get('/trade/orders', params),
  fills: (params) => get('/trade/fills', params),
  plans: (params) => get('/trade/plans', params),
  setHalt: (on) => post('/trade/halt', { on }),
  // 重取快照会起子进程连 QMT，单独放宽超时
  snapshot: () => post('/trade/snapshot', {}, 120_000),
  snapshotStatus: () => get('/trade/snapshot/status'),
  // 自建模拟盘：净值曲线 + 持仓（只读库）
  paper: () => get('/trade/paper'),
}

/** 因子研究：最近一次 `cli factors` 的评估结果（只读库，见 api/factors.py） */
export const factorApi = {
  latest: () => get('/factors'),
}
