/**
 * 个股链接与返回链接的**唯一真源**。
 *
 * 两件事必须钉在一起，否则「回看某天 → 点个股 → 返回」就会丢日期：
 * 1. 去个股时带上**生效交易日**与来源；
 * 2. 返回时按**固定白名单**映射回原页面 —— 不接受 URL 里的任意返回地址
 *    （那是一个开放重定向面：攻击者构造 `?from=https://evil` 就能把用户带走）。
 */

/** 来源 → 返回目标。不在表里的来源一律回带日期的排行榜。 */
export const BACK_TARGETS = {
  review: { path: '/review', label: '每日复盘' },
  rankings: { path: '/', label: '排行榜' },
  watchlist: { path: '/watchlist', label: '自选预警' },
  sectors: { path: '/sectors', label: '板块强弱' },
  portfolio: { path: '/portfolio', label: '模拟持仓' },
}

/** 去个股页的 query：日期 + 来源（两者都可选，但顺序固定）。 */
export function stockQuery(date, from) {
  const q = {}
  if (typeof date === 'string' && date) q.date = date
  if (typeof from === 'string' && BACK_TARGETS[from]) q.from = from
  return q
}

/** 去个股页的链接。 */
export function stockLink(symbol, date, from) {
  return { path: `/stock/${symbol}`, query: stockQuery(date, from) }
}

/** 返回链接（固定白名单 + 带上日期）。 */
export function backLink(from, date, extra = {}) {
  const target = BACK_TARGETS[from]
  const q = {}
  if (typeof date === 'string' && date) q.date = date
  for (const [k, v] of Object.entries(extra)) {
    if (typeof v === 'string' && v) q[k] = v
  }
  return { path: target ? target.path : '/', query: q }
}

/** 返回按钮上的文案。 */
export function backLabel(from) {
  return (BACK_TARGETS[from]?.label) || '排行榜'
}
