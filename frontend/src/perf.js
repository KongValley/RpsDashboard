/**
 * 前端性能埋点。
 *
 * 只在 api/index.js 的网络出口计时（全前端唯一出口）并批量上报后端，
 * 与后端中间件的数据在 GET /api/meta/perf 里对照：
 *   后端在响应头给了 x-perf-ms（服务端处理耗时）→ net_ms = ms - server_ms
 *   就是网络 + 解析那一段。
 *
 * 刻意不做 UI：这是盯盘工具，界面里不该出现调试数字。
 *   查看本页数据：window.__perf()
 *   查看前后端对照：await window.__perf.server()
 *   逐请求打点：    window.__perf.enable() 后刷新（localStorage 开关，默认静默）
 */

const LOCAL_MAX = 200
const FLUSH_EVERY = 20
const FLUSH_MS = 10_000
const DEBUG_KEY = 'rpsboard.perf.debug'
const ENDPOINT = '/api/meta/perf/client'

/** 全部记录（截断后），供 __perf() 查看 */
const local = []
/** 尚未上报成功的记录，成功后才移除 */
const pending = []
let lastFlush = 0
let installed = false

function debugOn() {
  try {
    return localStorage.getItem(DEBUG_KEY) === '1'
  } catch {
    return false
  }
}

function setDebug(on) {
  try {
    if (on) localStorage.setItem(DEBUG_KEY, '1')
    else localStorage.removeItem(DEBUG_KEY)
  } catch { /* 隐私模式/配额满：只是开关不生效 */ }
  console.info(on ? '[perf] 已开启逐请求打点，刷新页面后每请求一行 console.debug'
    : '[perf] 已关闭逐请求打点')
}

function trim(arr) {
  if (arr.length > LOCAL_MAX) arr.splice(0, arr.length - LOCAL_MAX)
}

export function record(entry) {
  const e = { ts: Date.now() / 1000, ...entry }
  local.push(e)
  trim(local)
  pending.push(e)
  trim(pending)
  if (debugOn()) {
    console.debug('[perf]', e.path, `${e.ms}ms`, e.server_ms === undefined ? '-' : `server ${e.server_ms}ms`,
      e.ok ? '' : (e.error || e.status))
  }
  if (pending.length >= FLUSH_EVERY || Date.now() - lastFlush > FLUSH_MS) flush()
}

/** 上报一批（最多 FLUSH_EVERY 条）。失败保留 pending 下次再试，绝不抛出。 */
export function flush({ beacon = false } = {}) {
  if (!pending.length) return
  const batch = pending.slice(0, FLUSH_EVERY)
  const body = JSON.stringify({ entries: batch })

  if (beacon) {
    let queued = false
    try {
      queued = navigator.sendBeacon(ENDPOINT, new Blob([body], { type: 'application/json' }))
    } catch {
      queued = false
    }
    if (queued) drop(batch)
    return
  }

  lastFlush = Date.now()
  fetch(ENDPOINT, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body,
    keepalive: true,
  })
    .then((res) => { if (res.ok) drop(batch) })
    .catch(() => { /* 网络抖动：留着下次一起报 */ })
}

/** 按引用移除已上报条目——期间规则可能又追加/截断，按下标删会错位 */
function drop(batch) {
  for (const e of batch) {
    const i = pending.indexOf(e)
    if (i >= 0) pending.splice(i, 1)
  }
}

export async function report() {
  const res = await fetch('/api/meta/perf', { headers: { Accept: 'application/json' } })
  if (!res.ok) throw new Error(`HTTP ${res.status} /api/meta/perf`)
  return res.json()
}

export function install() {
  if (installed) return
  installed = true

  const perf = async (opts = {}) => {
    flush()
    console.table(local.slice(-50))
    if (opts.server) {
      const d = await report()
      console.table(d.server.paths)
      console.table(d.client.recent)
    }
    return { local: local.length, pending: pending.length }
  }
  perf.server = async () => {
    const d = await report()
    console.table(d.server.paths)
    console.table(d.server.recent_slow)
    console.table(d.client.recent)
    return d
  }
  perf.enable = () => setDebug(true)
  perf.disable = () => setDebug(false)
  perf.raw = () => local
  window.__perf = perf

  // 关页/切后台时把没报完的一起报出去（beacon 才能在卸载期发请求）
  window.addEventListener('pagehide', () => flush({ beacon: true }))
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) flush({ beacon: true })
  })
}
