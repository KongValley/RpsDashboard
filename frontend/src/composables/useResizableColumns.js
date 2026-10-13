/**
 * 表格列宽拖拽。
 *
 * 用 mousedown/mousemove/mouseup 而不是 HTML5 drag&drop：后者在表格里
 * 需要 draggable 属性、拖拽幽灵图与放置指示器，实现繁琐且各浏览器表现不一；
 * 拖边框的手感反而更接近 Excel / 证券终端这类专业表格。
 *
 * 宽度落到 localStorage：这是个人盯盘工具，反复调一次列宽再被重置很恼人。
 *
 * 关键约束：必须配合 `table-layout: fixed`。默认的 auto 布局会「善意地」
 * 按内容重新分配列宽，拖拽刚设好的像素值会被立刻冲掉。
 */

import { onBeforeUnmount, reactive, watch } from 'vue'

const MIN_W = 52
const MAX_W = 900
const KEY_PREFIX = 'rpsboard.colwidth.'

function readStore(key) {
  try {
    const raw = localStorage.getItem(KEY_PREFIX + key)
    return raw ? JSON.parse(raw) : {}
  } catch {
    return {}
  }
}

/**
 * @param {string} storageKey 存储标识（不同表格用不同 key）
 * @param {Record<string, number>} defaults 各列默认宽度（px）
 */
export function useResizableColumns(storageKey, defaults = {}) {
  const widths = reactive({ ...defaults, ...readStore(storageKey) })

  watch(
    widths,
    (v) => {
      try {
        localStorage.setItem(KEY_PREFIX + storageKey, JSON.stringify(v))
      } catch { /* 隐私模式/超额时忽略 */ }
    },
    { deep: true },
  )

  let drag = null

  function onMove(ev) {
    if (!drag) return
    const next = drag.startW + (ev.clientX - drag.startX)
    widths[drag.key] = Math.min(MAX_W, Math.max(MIN_W, Math.round(next)))
  }

  function onUp() {
    if (!drag) return
    drag = null
    document.removeEventListener('mousemove', onMove)
    document.removeEventListener('mouseup', onUp)
    document.body.style.userSelect = ''
    document.body.style.cursor = ''
  }

  /** 绑在表头的拖拽把手上 */
  function startResize(key, ev) {
    // 表头本身有 click（排序等），必须阻止冒泡，否则拖完会误触发
    ev.preventDefault()
    ev.stopPropagation()
    const th = ev.currentTarget?.closest?.('th')
    drag = {
      key,
      startX: ev.clientX,
      startW: widths[key] || th?.offsetWidth || 140,
    }
    document.addEventListener('mousemove', onMove)
    document.addEventListener('mouseup', onUp)
    document.body.style.userSelect = 'none'
    document.body.style.cursor = 'col-resize'
  }

  function colStyle(key) {
    const w = widths[key]
    return w ? { width: `${w}px`, minWidth: `${w}px` } : {}
  }

  onBeforeUnmount(onUp)

  return { widths, colStyle, startResize }
}
