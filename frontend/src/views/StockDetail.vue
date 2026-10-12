<template>
  <div class="detail">
    <div v-if="!info" class="panel muted">加载中…</div>
    <template v-else>
      <div class="panel head">
        <div class="flex items-center gap-3">
          <router-link to="/" class="btn">← 返回</router-link>
          <h2 style="margin:0">{{ info.name }}</h2>
          <span class="mono muted">{{ info.symbol }} · {{ info.exchange?.toUpperCase() }}</span>
          <span v-if="info.is_st" class="badge st">ST</span>
          <span class="badge ind">{{ info.industry || '未分类' }}</span>
        </div>
        <div class="muted" style="margin-top:8px;font-size:12px">
          上市日 {{ info.ipo_date || '—' }}
          <span v-if="dataDate"> · 数据日期 {{ dataDate }}</span>
        </div>
      </div>

      <div class="cards">
        <div v-for="p in periods" :key="p" class="panel card">
          <div class="muted label">RPS{{ p }}</div>
          <div class="value mono" :style="{ color: rpsColor(cur[`rps${p}`]) }">
            {{ fmt(cur[`rps${p}`]) }}
          </div>
          <div class="sub">
            <span :class="cls(cur[`ret${p}`])">区间 {{ fmtPct(cur[`ret${p}`]) }}</span>
          </div>
          <div class="sub muted">
            跑赢全市场 <b>{{ fmt(pct[`rps${p}`]) }}%</b>
          </div>
          <div class="bar-track">
            <div class="bar-fill" :style="{ width: `${cur[`rps${p}`] ?? 0}%`, background: rpsColor(cur[`rps${p}`]) }"></div>
          </div>
        </div>
      </div>

      <div v-if="industryRps" class="panel" style="margin-top:12px">
        <h3 style="margin-top:0">所属行业强弱 · {{ info.industry }}</h3>
        <div class="flex gap-3" style="flex-wrap:wrap">
          <div v-for="p in periods" :key="p" class="ind-chip">
            <span class="muted">RPS{{ p }}</span>
            <b class="mono" :style="{ color: rpsColor(industryRps[`rps${p}`]) }">
              {{ fmt(industryRps[`rps${p}`]) }}
            </b>
          </div>
        </div>
      </div>

      <div class="panel" style="margin-top:12px">
        <div class="flex items-center gap-3" style="margin-bottom:10px">
          <h3 style="margin:0">RPS 曲线 vs 后复权价格</h3>
          <div class="spacer" />
          <label class="muted">回看
            <select v-model.number="days" @change="loadCurve" style="margin-left:6px">
              <option :value="120">120 日</option>
              <option :value="250">250 日</option>
              <option :value="400">全部</option>
            </select>
          </label>
        </div>
        <VChart class="chart" :option="curveOption" autoresize />
        <div class="legend-note muted">
          上方为多周期 RPS（0~100，右轴，80 以上为强势区）；
          下方为后复权收盘价（左轴）。RPS 通常**领先**于价格创新高，是欧奈尔体系的核心信号。
        </div>
      </div>
    </template>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { stockApi } from '../api'

const route = useRoute()
const periods = [20, 60, 120, 250]
const symbol = computed(() => route.params.symbol)

const info = ref(null)
const cur = ref({})
const pct = ref({})
const industryRps = ref(null)
const curve = ref({ dates: [], close: [], rps20: [], rps60: [], rps120: [], rps250: [] })
const days = ref(250)
const dataDate = ref(null)

async function loadDetail() {
  try {
    const d = await stockApi.detail(symbol.value)
    info.value = d.info
    cur.value = d.current || {}
    pct.value = d.market_percentile || {}
    industryRps.value = d.industry_rps
    dataDate.value = cur.value?.date
  } catch (e) { alert(e.message) }
}

async function loadCurve() {
  try {
    curve.value = await stockApi.curve(symbol.value, days.value)
  } catch (e) { console.error(e) }
}

const curveOption = computed(() => {
  const c = curve.value
  const dates = c.dates || []
  return {
    tooltip: { trigger: 'axis' },
    legend: { data: ['收盘价(后复权)', 'RPS20', 'RPS60', 'RPS120', 'RPS250'],
      textStyle: { color: '#8b98a9' }, top: 0 },
    grid: [
      { left: 60, right: 60, top: 34, height: '38%' },
      { left: 60, right: 60, top: '58%', height: '30%' },
    ],
    xAxis: [
      { type: 'category', data: dates, gridIndex: 0, axisLabel: { show: false },
        axisLine: { lineStyle: { color: '#2a323d' } } },
      { type: 'category', data: dates, gridIndex: 1, axisLabel: { color: '#8b98a9', fontSize: 10 },
        axisLine: { lineStyle: { color: '#2a323d' } } },
    ],
    yAxis: [
      { type: 'value', gridIndex: 0, min: 0, max: 100, name: 'RPS',
        nameTextStyle: { color: '#8b98a9' }, axisLabel: { color: '#8b98a9' },
        splitLine: { lineStyle: { color: 'rgba(42,50,61,.6)' } } },
      { type: 'value', gridIndex: 1, scale: true, axisLabel: { color: '#8b98a9' },
        splitLine: { lineStyle: { color: 'rgba(42,50,61,.6)' } } },
    ],
    series: [
      ...periods.map((p, i) => ({
        name: `RPS${p}`, type: 'line', xAxisIndex: 0, yAxisIndex: 0,
        data: c[`rps${p}`] || [], showSymbol: false,
        lineStyle: { width: i === 1 ? 2.2 : 1.1,
          color: ['#4c9aff', '#f6465d', '#f0a020', '#2ebd85'][i] },
        itemStyle: { color: ['#4c9aff', '#f6465d', '#f0a020', '#2ebd85'][i] },
        markLine: i === 1 ? {
          silent: true, symbol: 'none',
          data: [{ yAxis: 80 }, { yAxis: 90 }],
          lineStyle: { color: '#f6465d', type: 'dashed', width: 1, opacity: .5 },
          label: { formatter: '{c}', color: '#8b98a9', fontSize: 10 },
        } : undefined,
      })),
      {
        name: '收盘价(后复权)', type: 'line', xAxisIndex: 1, yAxisIndex: 1,
        data: c.close || [], showSymbol: false,
        lineStyle: { color: '#e6edf3', width: 1.6 },
        areaStyle: { color: 'rgba(76,154,255,.10)' },
      },
    ],
  }
})

function fmt(v) { return v == null ? '—' : Number(v).toFixed(2) }
function fmtPct(v) { return v == null ? '—' : `${(Number(v) * 100).toFixed(2)}%` }
function cls(v) { return v == null ? 'muted' : (v >= 0 ? 'up' : 'down') }
function rpsColor(v) {
  if (v == null) return '#8b98a9'
  if (v >= 90) return '#f6465d'
  if (v >= 80) return '#ff8f43'
  if (v >= 70) return '#f0a020'
  if (v >= 50) return '#4c9aff'
  return '#2ebd85'
}

watch(symbol, () => { loadDetail(); loadCurve() })
onMounted(() => { loadDetail(); loadCurve() })
</script>

<style scoped>
.head { margin-bottom: 12px; }
.spacer { flex: 1; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); gap: 12px; }
.card .label { font-size: 12px; }
.card .value { font-size: 30px; font-weight: 700; line-height: 1.25; }
.card .sub { font-size: 12px; margin-top: 3px; }
.bar-track { height: 5px; background: var(--bg); border-radius: 3px; margin-top: 8px; overflow: hidden; }
.bar-fill { height: 100%; border-radius: 3px; }
.badge.st { background: rgba(246,70,93,.2); color: var(--up); }
.badge.ind { background: rgba(76,154,255,.16); color: var(--accent); }
.ind-chip {
  display: flex; flex-direction: column; gap: 2px; padding: 10px 16px;
  background: var(--bg-panel-2); border-radius: 6px; font-size: 12px;
}
.chart { height: 560px; }
.legend-note { font-size: 12px; margin-top: 8px; line-height: 1.7; }
</style>
