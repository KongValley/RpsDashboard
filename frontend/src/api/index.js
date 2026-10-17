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

/** 从响应里取出后端给的错误说明（Pydantic 422 的 detail 可能是数组）。 */
async function errorDetail(res, url) {
  let body = null
  try {
    body = await res.json()
  } catch {
    return `HTTP ${res.status} ${url}`
  }
  const d = body?.detail
  if (typeof d === 'string' && d) return d
  if (Array.isArray(d) && d.length) {
    // Pydantic 校验错误：[{loc:[...], msg}] → 拼成一句人话
    return d.map((x) => `${(x.loc || []).slice(1).join('.') || '参数'}: ${x.msg}`)
      .join('；')
  }
  return `HTTP ${res.status} ${url}`
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
    // 后端把原因写在 detail 里（业务冲突 409 / 校验 422 都是）；
    // 只显示「HTTP 409」让用户无从下手。
    if (!res.ok) throw new Error(await errorDetail(res, url))
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

async function post(path, body, timeoutMs = TIMEOUT_MS, extraInit = null) {
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
      ...(extraInit || {}),
      signal: guard.signal,
    })
    if (!res.ok) throw new Error(await errorDetail(res, path))
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
  // date 可空：给了就回看历史某天（后端就近回退到有 RPS 的交易日）
  detail: (symbol, date) => get(`/stock/${symbol}`, date ? { date } : undefined),
  curve: (symbol, days = 250, date) =>
    get(`/stock/${symbol}/curve`, date ? { days, date } : { days }),
  patterns: (symbol, days = 250, date) =>
    get(`/stock/${symbol}/patterns`, date ? { days, date } : { days }),
}

/** 已保存的筛选条件（排行榜看板）；参数白名单见 backend/rpsboard/api/screens.py */
export const screenApi = {
  list: () => get('/screens'),
  save: (name, params) => post('/screens', { name, params }),
  remove: (id) => request(`/screens/${id}`, undefined, { method: 'DELETE' }),
}

export const backtestApi = {
  // 回测是同步算的（大库上可达数十秒），单独放宽超时；
  // 合成分还要多走一遍全市场因子矩阵，调用方可再放宽（见 Backtest.vue 的 signal）
  run: (params, timeoutMs) => get('/backtest/run', params, timeoutMs ?? BACKTEST_TIMEOUT_MS),
  // 存库（跨会话保留）：参数放 query 上，与 /run 同一套签名
  save: (params, label) =>
    post(`/backtest/save${qs({ ...params, label })}`, {}, BACKTEST_TIMEOUT_MS),
  // 列表不带 payload（对比表只要参数与指标）；详情才回完整结果
  saved: (params) => get('/backtest/saved', params),
  savedDetail: (id) => get(`/backtest/saved/${id}`),
  removeSaved: (id) =>
    request(`/backtest/saved/${id}`, undefined, { method: 'DELETE' }),
}

export const adminApi = {
  update: () => post('/admin/update', {}),
  updateStatus: () => get('/admin/update/status'),
}

/** 自选股池（全 API 唯一的写入面，见 backend/rpsboard/api/watchlist.py） */
export const watchApi = {
  list: (params) => get('/watchlist', params),
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

/** QMT 交易接入（只读库 + 两个写入口；自建模拟盘已迁到 portfolioApi） */
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
}

/**
 * 自建模拟盘（多账户、原价撮合、独立流水）。
 *
 * 写操作超时放宽到 45 秒（服务端最多等 25 秒 SQLite 写锁），推进放宽到 180 秒：
 * 推进要逐日结算，大区间会跑一会儿。超时后重试是安全的 —— 服务端按
 * 「已结算日绝不重算」处理。
 */
export const portfolioApi = {
  config: () => get('/portfolio/config'),
  accounts: () => get('/portfolio/accounts'),
  createAccount: (body) => post('/portfolio/accounts', body, 45_000),
  archiveAccount: (id) => post(`/portfolio/accounts/${id}/archive`, {}, 45_000),
  detail: (id, params) => get(`/portfolio/accounts/${id}`, params),
  dates: (id) => get(`/portfolio/accounts/${id}/dates`),
  orders: (id, params) => get(`/portfolio/accounts/${id}/orders`, params),
  fills: (id, params) => get(`/portfolio/accounts/${id}/fills`, params),
  submitOrder: (id, body) => post(`/portfolio/accounts/${id}/orders`, body, 45_000),
  cancelOrder: (id, orderId) =>
    post(`/portfolio/accounts/${id}/orders/${orderId}/cancel`, {}, 45_000),
  reviewOrder: (id, orderId, reviewNote) =>
    post(`/portfolio/accounts/${id}/orders/${orderId}/review`,
      { review_note: reviewNote }, 45_000),
  advance: (id, toDate) =>
    post(`/portfolio/accounts/${id}/advance`, { to_date: toDate || null }, 180_000),
  actions: (id) => get(`/portfolio/accounts/${id}/actions`),
  registerAction: (id, body) => post(`/portfolio/accounts/${id}/actions`, body, 45_000),
  risk: (id, params) => get(`/portfolio/accounts/${id}/risk`, params),
  archives: (params) => get('/portfolio/archives', params),
  exportCsv: (id, kind, name) =>
    download(`/portfolio/accounts/${id}/export.csv`, { kind }, name),
  confirmSuspension: (body) => post('/portfolio/suspensions', body, 45_000),
}

/**
 * 交易工具：仓位预算 / 往返成本 / 持仓压力测试。
 *
 * 前两个是**纯计算**（不查证券表、不读行情），所以空库也能用；
 * 压力测试要账户快照。
 */
export const toolsApi = {
  positionSize: (body) => post('/tools/position-size', body),
  roundTrip: (body) => post('/tools/round-trip', body),
  stress: (body) => post('/tools/stress', body),
}

/** 当日复盘：市场宽度 + 涨跌停清单 + 行业当日涨跌 + 信号名单（只读库） */
export const reviewApi = {
  day: (params) => get('/review/day', params),
  // 市场笔记（symbol=''）与个股笔记共用；日期必须是有效交易日，否则后端 422
  note: (params) => get('/review/note', params),
  saveNote: (body) => post('/review/note', body),
}

/** 因子研究：最近一次 `cli factors` 的评估结果（只读库，见 api/factors.py） */
export const factorApi = {
  latest: () => get('/factors'),
}
