<template>
  <div class="sectors">
    <div class="panel">
      <div class="head flex items-center gap-3">
        <h3 style="margin:0">行业 × 周期 RPS 热力图</h3>
        <span class="muted">
          数据日期 {{ dataDate || '—' }} · 行按
          <b class="sort-hint">RPS{{ period }} 降序</b>独立排列
          （四个周期各自排序）· 点击格子查看该{{ mode === 'concept' ? '概念' : '行业' }}榜单
        </span>
        <div class="spacer" />
        <div class="group">
          <label>口径</label>
          <button class="btn" :class="{ active: mode === 'industry' }"
                  @click="setMode('industry')">证监会行业</button>
          <button class="btn" :class="{ active: mode === 'concept' }"
                  :disabled="!hasConcepts" @click="setMode('concept')">概念题材</button>
        </div>
        <div class="group">
          <label>数据日期</label>
          <select v-model="date" style="min-width:118px">
            <option value="">最新</option>
            <option v-for="d in availableDates" :key="d" :value="d">{{ d }}</option>
          </select>
        </div>
        <div class="period-tabs">
          <button
            v-for="p in periods" :key="p"
            class="btn" :class="{ active: period === p }"
            @click="period = p; loadSector()"
          >{{ p }}日</button>
        </div>
      </div>
      <VChart class="chart" :style="{ height: chartHeight + 'px' }" :option="heatmapOption" autoresize />
    </div>

    <div class="panel" style="margin-top:12px">
      <h3 style="margin-top:0">板块排行 · RPS{{ period }}
        <span class="muted" style="font-weight:400;font-size:12px">
          （{{ mode === 'concept' ? '概念题材' : '证监会行业' }}）
        </span>
      </h3>
      <div class="scroll-y">
      <table class="rps-table resizable">
        <thead>
          <tr>
            <th v-for="c in sectorCols" :key="c.key" :style="sectorColStyle(c.key)"
                :class="{ num: c.num }">
              <span class="th-label">{{ c.label }}</span>
              <span class="rz" @mousedown="onSectorResize(c.key, $event)"></span>
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="!sectorRows.length">
            <td colspan="7" class="empty muted">暂无板块数据，请先执行 compute-rps</td>
          </tr>
          <tr v-for="(r, i) in sectorRows" :key="r.industry" @click="goIndustry(r.industry)">
            <td class="muted mono">{{ i + 1 }}</td>
            <td :title="r.industry">{{ shortName(r.industry) }}</td>
            <td>
              <span class="rps-bar" :style="{ width: rpsWidth(r.rps), background: rpsColor(r.rps) }"></span>
              <span class="mono">{{ r.rps?.toFixed(2) ?? '—' }}</span>
            </td>
            <td class="num" :class="r.period_return >= 0 ? 'up' : 'down'">
              {{ r.period_return != null ? (r.period_return * 100).toFixed(2) + '%' : '—' }}
            </td>
            <td class="num muted">{{ r.member_count ?? '—' }}</td>
            <td class="mono muted">{{ r.leader_symbol || '—' }}</td>
            <td>{{ r.leader_name || '—' }}</td>
          </tr>
        </tbody>
      </table>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { rpsApi, metaApi } from '../api'
import { useResizableColumns } from '../composables/useResizableColumns'

const router = useRouter()
const route = useRoute()
const periods = [20, 60, 120, 250]
const period = ref(60)
const dataDate = ref(null)
/** 'industry' = 证监会行业（83 个）；'concept' = 概念题材 */
const mode = ref('industry')
const hasConcepts = ref(false)
/** 空 = 最新交易日 */
const date = ref('')
const availableDates = ref([])
const industries = ref([])
const matrixData = ref([])
const sectorRows = ref([])

async function loadHeatmap() {
  try {
    const isConcept = mode.value === 'concept'
    const d = isConcept
      ? await rpsApi.conceptHeatmap({ date: date.value || undefined })
      : await rpsApi.heatmap({ date: date.value || undefined })
    if (d.error) throw new Error(d.error)
    dataDate.value = d.data_date
    // 行业接口字段叫 industries，概念接口叫 concepts，两者在下游是同一个东西
    industries.value = (isConcept ? d.concepts : d.industries) || []
    // 概念热力图返回列名是 concept
    matrixData.value = (d.matrix || []).map((r) => ({
      industry: isConcept ? r.concept : r.industry,
      rps20: r.rps20, rps60: r.rps60, rps120: r.rps120, rps250: r.rps250,
    }))
  } catch (e) { console.error(e) }
}

async function loadSector() {
  try {
    const isConcept = mode.value === 'concept'
    const d = isConcept
      ? await rpsApi.conceptRanking({ period: period.value, date: date.value || undefined })
      : await rpsApi.sector({ period: period.value, date: date.value || undefined })
    if (d.error) throw new Error(d.error)
    // 概念接口列名是 concept
    sectorRows.value = (d.rows || []).map((r) =>
      isConcept ? { ...r, industry: r.concept } : r)
  } catch (e) { console.error(e) }
}

/** 证监会行业 / 概念题材切换。两套数据共用同一套渲染与排序逻辑。 */
function setMode(m) {
  if (mode.value === m) return
  mode.value = m
  loadHeatmap()
  loadSector()
}

watch(date, () => { loadHeatmap(); loadSector() })

/** 去掉证监会行业分类代码前缀，让行业名在轴上放得下 */
function shortName(s) {
  return String(s || '').replace(/^[A-Z]\d{2}/, '')
}

/**
 * 按当前选中周期对行业独立排序。
 *
 * 四个周期各自维护一套强弱次序：20 日强的行业未必 250 日也强，
 * 所以切换周期时热力图要整体重排，而不是固定按某个周期排。
 * 后端 /api/rps/heatmap 返回的是按 rps60 排好的，这里在前端按当前周期重排，
 * 避免每次切换都重新请求。
 */
const sortedByPeriod = computed(() => {
  const key = `rps${period.value}`
  return [...matrixData.value].sort((a, b) => {
    const va = a[key]
    const vb = b[key]
    // 缺失值永远沉底，强（数值大）在前
    if (va === null || va === undefined) return 1
    if (vb === null || vb === undefined) return -1
    return vb - va
  })
})

const heatmapOption = computed(() => {
  const cols = periods.map((p) => `RPS${p}`)
  const activeX = periods.indexOf(period.value)
  const rows = sortedByPeriod.value.map((r) => shortName(r.industry))
  const data = []
  sortedByPeriod.value.forEach((r, y) => {
    periods.forEach((p, x) => {
      const v = r[`rps${p}`]
      data.push([x, y, v === null || v === undefined ? '-' : v, r.industry, x === activeX])
    })
  })
  return {
    tooltip: {
      formatter: (p) => {
        const [x, , v, name, isActive] = p.data
        const extra = isActive ? '<br/><b>（当前排序依据）</b>' : ''
        return `${name}<br/>${cols[x]}：${v === '-' ? '无数据' : v}${extra}`
      },
    },
    grid: { left: 8, right: 16, top: 12, bottom: 58, containLabel: true },
    xAxis: {
      type: 'category', data: cols, position: 'top', splitArea: { show: false },
      axisLabel: {
        color: (val) => (val === `RPS${period.value}` ? '#4c9aff' : '#8b98a9'),
        fontSize: 11,
        fontWeight: (val) => (val === `RPS${period.value}` ? 'bold' : 'normal'),
        formatter: (val) => (val === `RPS${period.value}` ? `${val} ↓` : val),
      },
      axisLine: { lineStyle: { color: '#2a323d' } },
    },
    yAxis: {
      type: 'category', data: rows, inverse: true,
      axisLabel: { color: '#8b98a9', fontSize: 10, width: 92, overflow: 'truncate' },
      axisLine: { show: false }, axisTick: { show: false },
    },
    visualMap: {
      // 必须显式指定 dimension=2（数值那一维）。
      // 数据是 [x, y, value, industryName, isActiveSortColumn] 五元组，
      // echarts 默认取最后一个维度做映射，而那一维是布尔值 → 映射失败，格子全无色。
      dimension: 2,
      min: 0, max: 100, calculable: true, orient: 'horizontal',
      left: 'center', bottom: 0, itemWidth: 12, itemHeight: 140,
      text: ['强 100', '弱 0'],
      textStyle: { color: '#8b98a9', fontSize: 10 },
      inRange: { color: ['#2ebd85', '#1f6f52', '#4c9aff', '#f0a020', '#f6465d'] },
    },
    series: [{
      type: 'heatmap', data,
      itemStyle: {
        borderColor: '#171c24', borderWidth: 1,
      },
      label: {
        show: true, fontSize: 10,
        formatter: (p) => (p.value[2] === '-' ? '' : Math.round(p.value[2])),
      },
      emphasis: { itemStyle: { borderColor: '#fff', borderWidth: 1 } },
    }],
  }
})

// 行数随排序结果变化，高度跟着走
const chartHeight = computed(() => Math.max(420, sortedByPeriod.value.length * 19 + 70))

/** 板块排行表：列宽同样可拖拽，与排行榜各自记住（不同 storageKey） */
const sectorCols = [
  { key: 'no', label: '#' },
  { key: 'name', label: '行业' },
  { key: 'rps', label: 'RPS' },
  { key: 'ret', label: '区间涨幅', num: true },
  { key: 'cnt', label: '成分数', num: true },
  { key: 'leadSym', label: '领涨股代码' },
  { key: 'leadName', label: '领涨股' },
]
const { colStyle: sectorColStyle, startResize: startSectorResize } = useResizableColumns(
  'sector',
  { no: 52, name: 210, rps: 140, ret: 100, cnt: 90, leadSym: 110, leadName: 180 },
)
function onSectorResize(key, ev) { startSectorResize(key, ev) }

function rpsColor(v) {
  if (v == null) return '#2a323d'
  if (v >= 90) return '#f6465d'
  if (v >= 80) return '#ff8f43'
  if (v >= 70) return '#f0a020'
  if (v >= 50) return '#4c9aff'
  return '#2ebd85'
}
/** 去掉证监会行业分类代码前缀："C39计算机、通信…" → "计算机、通信…" */
function rpsWidth(v) {
  return v == null ? '0px' : `${Math.max(2, (v / 100) * 40)}px`
}
function goIndustry(name) {
  router.push({ path: '/', query: { industries: name } })
}

onMounted(() => {
  loadHeatmap(); loadSector()
  metaApi.dates(400)
    .then((ds) => { availableDates.value = ds || [] })
    .catch((e) => console.error('交易日历加载失败', e))
  rpsApi.concepts()
    .then((c) => { hasConcepts.value = (c || []).length > 0 })
    .catch(() => { hasConcepts.value = false })
  if (route.query.date) date.value = String(route.query.date)
})
</script>

<style scoped>
.head { margin-bottom: 12px; }
.spacer { flex: 1; }
.sort-hint { color: var(--accent); }
.period-tabs { display: flex; gap: 4px; }
.chart { height: 620px; }
.period-tabs { display: flex; gap: 4px; }
.rps-table-wrap { max-height: 500px; overflow: auto; }
.scroll-y { max-height: 560px; overflow-y: auto; }
td.num, th.num { text-align: right; }
td.empty { text-align: center; padding: 40px; }
.rps-table-wrap { max-height: 500px; overflow: auto; }
</style>
