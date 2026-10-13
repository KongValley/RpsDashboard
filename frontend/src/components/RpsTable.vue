<template>
  <div class="rps-table-wrap">
    <table class="rps-table resizable" :class="{ dragging }">
      <thead>
        <tr>
          <th v-for="c in columns" :key="c.key" :style="colStyle(c.key)"
              :class="{ num: c.num }" :title="c.tip || ''">
            <span class="th-label">{{ c.label }}</span>
            <span class="rz" @mousedown="onResize(c.key, $event)"></span>
          </th>
        </tr>
      </thead>
      <tbody>
        <tr v-if="loading">
          <td :colspan="columns.length" class="empty muted">加载中…</td>
        </tr>
        <tr v-else-if="!rows.length">
          <td :colspan="columns.length" class="empty muted">
            没有符合条件的股票，试试放宽 RPS 阈值
          </td>
        </tr>
        <tr v-for="(r, i) in rows" :key="r.symbol" @click="$emit('select', r.symbol)">
          <td class="muted mono">{{ (page - 1) * pageSize + i + 1 }}</td>
          <td class="mono">{{ r.symbol }}</td>
          <td>
            <span v-if="r.is_st" class="badge st">ST</span>{{ r.name }}
          </td>
          <td class="muted" :title="r.industry">{{ shortName(r.industry) }}</td>
          <td>
            <span class="rps-bar" :style="{ width: rpsWidth(r.rps), background: rpsColor(r.rps) }"></span>
            <span class="mono">{{ fmtNum(r.rps) }}</span>
          </td>
          <td class="num" :class="cls(r.period_return)">{{ fmtPct(r.period_return) }}</td>
          <td class="num" :class="cls(r.pct_chg)">{{ fmtPct(r.pct_chg) }}</td>
          <td class="num muted">{{ fmtAmount(r.amount) }}</td>
          <td v-if="showDelta" class="num" :class="deltaCls(r.rank_delta)">
            {{ fmtDelta(r.rank_delta) }}
          </td>
          <td v-if="showPattern">
            <span v-if="r.sanxingdi_hit" class="badge pat"
                  :class="r.sanxingdi_active ? 'ok' : 'stale'"
                  :title="patternTitle(r)">
              {{ r.sanxingdi_active ? '三升底·有效' : '三升底·已失效' }}
            </span>
            <span v-else class="muted">—</span>
          </td>
          <td class="num muted">{{ r.rank_overall ?? '—' }}</td>
          <td class="num muted">{{ r.rank_in_industry ?? '—' }}</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import { useResizableColumns } from '../composables/useResizableColumns'

const props = defineProps({
  rows: { type: Array, default: () => [] },
  page: { type: Number, default: 1 },
  pageSize: { type: Number, default: 100 },
  loading: { type: Boolean, default: false },
  /** 是否展示「Δ名次」列（由 Rankings 的对比周期选择决定） */
  showDelta: { type: Boolean, default: false },
  /** 是否展示「三升底」列（由 Rankings 的形态筛选决定） */
  showPattern: { type: Boolean, default: false },
})
defineEmits(['select'])

const colSpan = computed(() => 9 + (props.showDelta ? 1 : 0) + (props.showPattern ? 1 : 0))

/**
 * 表头定义。列宽拖拽按 key 记住，所以每列必须有稳定的 key
 * （不能用 label：两个列表头文字可能相同，且改名会丢用户的列宽设置）。
 */
const columns = computed(() => [
  { key: 'no', label: '#', width: 52 },
  { key: 'symbol', label: '代码', width: 76 },
  { key: 'name', label: '名称', width: 168 },
  { key: 'industry', label: '行业', width: 150 },
  { key: 'rps', label: 'RPS', width: 132 },
  { key: 'ret', label: '区间涨幅', width: 96, num: true },
  { key: 'pct', label: '当日涨跌', width: 96, num: true },
  { key: 'amount', label: '成交额', width: 100, num: true },
  { key: 'delta', label: 'Δ名次', width: 96, num: true,
    tip: '相对所选交易日的名次变化，正数=名次前进' },
  { key: 'pattern', label: '三升底', width: 116,
    tip: '标准三升底：连续三根阳线、收盘逐根抬高、第一根实体不超过第三根一半、第三根收盘突破第一根最高价' },
  { key: 'rank', label: '全市场', width: 84, num: true },
  { key: 'rankIn', label: '行业内', width: 84, num: true },
].filter((c) => (c.key !== 'delta' || props.showDelta)
              && (c.key !== 'pattern' || props.showPattern)))

const DEFAULTS = Object.fromEntries(
  [['no', 52], ['symbol', 76], ['name', 168], ['industry', 150], ['rps', 132],
   ['ret', 96], ['pct', 96], ['amount', 100], ['delta', 96], ['pattern', 116],
   ['rank', 84], ['rankIn', 84]],
)
const { colStyle, startResize } = useResizableColumns('ranking', DEFAULTS)
const dragging = ref(false)

function onResize(key, ev) {
  dragging.value = true
  startResize(key, ev)
  // 拖完再解除 body 上的 col-resize 光标，这里借 mouseup 的副作用收尾
  const clear = () => {
    dragging.value = false
    window.removeEventListener('mouseup', clear)
  }
  window.addEventListener('mouseup', clear)
}

/** 形态列的悬浮说明：形态确认日 + 当前是否仍有效 */
function patternTitle(r) {
  if (!r.sanxingdi_hit) return ''
  const head = `形态确认日 ${r.sanxingdi_date || '—'}`
  return r.sanxingdi_active
    ? `${head}｜形态仍有效（未跌破第一根最低价）`
    : `${head}｜已跌破第一根最低价，形态失效`
}

/** Δ名次：正数=名次前进（红涨），负数=名次后退（绿跌），与 A 股涨跌配色一致 */
function fmtDelta(v) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return '—'
  const n = Number(v)
  if (n === 0) return '—'
  return n > 0 ? `↑${n}` : `↓${Math.abs(n)}`
}
function deltaCls(v) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return 'muted'
  const n = Number(v)
  if (n === 0) return 'muted'
  return n > 0 ? 'up' : 'down'
}

function fmtNum(v) {
  return v === null || v === undefined ? '—' : Number(v).toFixed(2)
}
function fmtPct(v) {
  if (v === null || v === undefined) return '—'
  return `${Number(v).toFixed(2)}%`
}
function fmtAmount(v) {
  if (v === null || v === undefined) return '—'
  const n = Number(v)
  if (n >= 1e8) return `${(n / 1e8).toFixed(1)}亿`
  if (n >= 1e4) return `${(n / 1e4).toFixed(0)}万`
  return n.toFixed(0)
}
function cls(v) {
  if (v === null || v === undefined) return 'muted'
  return Number(v) >= 0 ? 'up' : 'down'
}
function rpsColor(v) {
  if (v === null || v === undefined) return '#2a323d'
  if (v >= 90) return '#f6465d'
  if (v >= 80) return '#ff8f43'
  if (v >= 70) return '#f0a020'
  if (v >= 50) return '#4c9aff'
  return '#2ebd85'
}
function rpsWidth(v) {
  if (v === null || v === undefined) return '0px'
  return `${Math.max(2, (v / 100) * 40)}px`
}
/** 去掉证监会行业分类代码前缀，让行业列放得下 */
function shortName(s) {
  const t = String(s || '').replace(/^[A-Z]\d{2}/, '')
  return t || '—'
}
</script>

<style scoped>
.rps-table-wrap { overflow: auto; max-height: calc(100vh - 250px); }
td.num, th.num { text-align: right; }
td.empty { text-align: center; padding: 40px; }
.badge.st {
  background: rgba(246, 70, 93, .2); color: var(--up);
  margin-right: 5px; font-size: 10px; padding: 1px 5px;
}
/* 三升底标记：有效=红（多头发力），失效=灰（形态已破坏） */
.badge.pat { font-size: 10px; padding: 1px 5px; white-space: nowrap; }
.badge.pat.ok { background: rgba(246, 70, 93, .18); color: var(--up); }
.badge.pat.stale {
  background: rgba(120, 132, 150, .16); color: var(--text-dim);
  text-decoration: line-through;
}
</style>
