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
          数据起始 {{ info.first_bar_date || '—' }}
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

      <div v-if="errMsg" class="warnbar soft inset">{{ errMsg }}</div>

      <div v-if="pattern && pattern.current" class="panel pat-now">
        <div class="flex items-center gap-3">
          <span class="badge pat big">三升底成立</span>
          <span class="muted">
            确认日 <b class="mono">{{ pattern.current.date }}</b>
            · 第一根阳线 <b class="mono">{{ pattern.current.base_date }}</b>
            · 三根合计 <b class="mono">{{ pctText(pattern.current.strength) }}</b>
            · 失效位（第一根最低价）<b class="mono">{{ fmt(pattern.current.base_low) }}</b>
            <template v-if="pattern.current.min_low_after != null">
              · 此后最低 <b class="mono">{{ fmt(pattern.current.min_low_after) }}</b>
            </template>
          </span>
        </div>
      </div>

      <div v-else-if="pattern && pattern.patterns.length" class="panel pat-old">
        <span class="muted">
          最近一次三升底在 <b class="mono">{{ pattern.patterns[0].date }}</b>，
          {{ pattern.patterns[0].active ? '形态仍有效' : '已跌破第一根最低价、形态失效' }}
          ，此后未再出现新的形态。
        </span>
      </div>

      <div v-if="pattern && pattern.pattern_missing" class="panel pat-old">
        <span class="muted">形态数据尚未计算，请先执行 <code>python -m rpsboard.cli patterns</code></span>
      </div>

      <div v-if="pattern && pattern.patterns.length" class="panel" style="margin-top:12px">
        <h3 style="margin-top:0">
          三升底历史形态
          <span class="muted" style="font-weight:400;font-size:12px">
            （近 {{ days }} 个交易日，共 {{ pattern.patterns.length }} 次）
          </span>
        </h3>
        <div class="muted note" style="margin-top:0">
          形态定义：连续三根阳线、收盘逐根抬高、第一根实体不超过第三根的一半，
          且第三根收盘突破第一根最高价。跌破第一根最低价即失效。
        </div>
        <table class="rps-table" style="margin-top:8px">
          <thead>
            <tr>
              <th>确认日</th><th>第一根阳线</th><th class="num">三根合计</th>
              <th class="num">第一/第三根实体比</th><th class="num">失效位</th>
              <th class="num">此后最低</th><th>状态</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="p in pattern.patterns" :key="p.date">
              <td class="mono">{{ p.date }}</td>
              <td class="mono muted">{{ p.base_date }}</td>
              <td class="num" :class="cls(p.strength)">{{ pctText(p.strength) }}</td>
              <td class="num muted">{{ p.body_ratio != null ? p.body_ratio.toFixed(3) : '—' }}</td>
              <td class="num muted">{{ fmt(p.base_low) }}</td>
              <td class="num muted">{{ p.min_low_after != null ? fmt(p.min_low_after) : '—' }}</td>
              <td>
                <span class="badge pat" :class="p.active ? 'ok' : 'stale'">
                  {{ p.active ? '有效' : '已失效' }}
                </span>
              </td>
            </tr>
          </tbody>
        </table>
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
            <select v-model.number="days" @change="() => { loadCurve(); loadPatterns() }" style="margin-left:6px">
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
import { usePeriods } from '../composables/usePeriods'
import { useLatestRequest } from '../composables/useLatestRequest'

const route = useRoute()
const { periods, error: periodsError } = usePeriods()
const symbol = computed(() => route.params.symbol)

const info = ref(null)
const cur = ref({})
const pct = ref({})
const industryRps = ref(null)
// 只给日期轴/价格的骨架：各周期数组由接口返回时补进来（键名 rps{p}）
const curve = ref({ dates: [], close: [] })
const pattern = ref(null)
const days = ref(250)
/** RPS 曲线配色（按系列下标取用，周期数增减都不改写法） */
const RPS_COLORS = ['#4c9aff', '#f6465d', '#f0a020', '#2ebd85', '#b06cf0', '#38bdf8']
/** 代表中期强弱的周期：加粗 + 画 80/90 参考线（按周期值判断，不看下标） */
const EMPHASIS_PERIOD = 60
const dataDate = ref(null)
/** 接口错误提示：失败不能再伪装成「形态数据尚未计算」 */
const errMsg = ref('')
watch(periodsError, (v) => { if (v) errMsg.value = v }, { immediate: true })
// 切换股票/窗口天数时只认最后一次响应（旧响应会把新股票的数据写回来）
const latestDetail = useLatestRequest()
const latestCurve = useLatestRequest()
const latestPatterns = useLatestRequest()

async function loadDetail() {
  const t = latestDetail.begin()
  try {
    const d = await stockApi.detail(symbol.value)
    if (!latestDetail.isCurrent(t)) return
    info.value = d.info
    cur.value = d.current || {}
    pct.value = d.market_percentile || {}
    industryRps.value = d.industry_rps
    dataDate.value = cur.value?.date
  } catch (e) {
    if (!latestDetail.isCurrent(t)) return
    console.error(e)
    errMsg.value = `个股信息加载失败：${e?.message || e}`
  }
}

async function loadCurve() {
  const t = latestCurve.begin()
  try {
    const d = await stockApi.curve(symbol.value, days.value)
    if (!latestCurve.isCurrent(t)) return
    curve.value = d
  } catch (e) {
    if (!latestCurve.isCurrent(t)) return
    console.error(e)
    errMsg.value = `曲线加载失败：${e?.message || e}`
  }
}

async function loadPatterns() {
  const t = latestPatterns.begin()
  try {
    const d = await stockApi.patterns(symbol.value, days.value)
    if (!latestPatterns.isCurrent(t)) return
    pattern.value = d
  } catch (e) {
    if (!latestPatterns.isCurrent(t)) return
    // 网络/500 是「加载失败」，与后端明确告知的「形态还没算」是两回事：
    // 以前这里把两者都写成 pattern_missing ，用户看到「形态数据尚未计算」会去重算，
    // 但真正的原因是接口挂了。
    console.error(e)
    errMsg.value = `形态加载失败：${e?.message || e}`
    pattern.value = { patterns: [], current: null, pattern_missing: false }
  }
}

function pctText(v) {
  return v == null ? '—' : `${(Number(v) * 100).toFixed(2)}%`
}

// 曲线上标出历史形态位置：确认日打点，用竖线标出三根阳线的区间
const patternMarks = computed(() => {
  const c = curve.value
  const pats = pattern.value?.patterns || []
  if (!c?.dates?.length || !pats.length) return null
  const index = new Map(c.dates.map((d, i) => [d, i]))
  const active = pats
    .filter((p) => index.has(p.date))
    .map((p) => ({
      xAxis: p.date,
      name: `${p.date} 三升底${p.active ? '' : '（已失效）'}`,
    }))
  return active.length ? active : null
})

const curveOption = computed(() => {
  const c = curve.value
  const dates = c.dates || []
  return {
    tooltip: { trigger: 'axis' },
    legend: { data: ['收盘价(后复权)', ...periods.value.map((p) => `RPS${p}`)],
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
      ...periods.value.map((p, i) => {
        // 配色按**系列下标**取，周期增删不改写法；加粗与 markLine 必须按
        // **周期值**判断——写死 `i === 1` 在 15 日插到最前之后指的是 20 日。
        const color = RPS_COLORS[i % RPS_COLORS.length]
        const emphasis = p === EMPHASIS_PERIOD
        return {
          name: `RPS${p}`, type: 'line', xAxisIndex: 0, yAxisIndex: 0,
          data: c[`rps${p}`] || [], showSymbol: false,
          lineStyle: { width: emphasis ? 2.2 : 1.1, color },
          itemStyle: { color },
          markLine: emphasis ? {
            silent: true, symbol: 'none',
            data: [{ yAxis: 80 }, { yAxis: 90 }],
            lineStyle: { color: '#f6465d', type: 'dashed', width: 1, opacity: .5 },
            label: { formatter: '{c}', color: '#8b98a9', fontSize: 10 },
          } : undefined,
        }
      }),
      {
        name: '收盘价(后复权)', type: 'line', xAxisIndex: 1, yAxisIndex: 1,
        data: c.close || [], showSymbol: false,
        lineStyle: { color: '#e6edf3', width: 1.6 },
        areaStyle: { color: 'rgba(76,154,255,.10)' },
        markLine: patternMarks.value ? {
          silent: true, symbol: 'none',
          data: patternMarks.value,
          lineStyle: { color: '#f6465d', width: 1, type: 'dashed', opacity: .55 },
          label: { color: '#f6465d', fontSize: 10, formatter: '三升底' },
        } : undefined,
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

function reloadAll() {
  // 错误提示按「一次加载批次」清理：不放在各 loader 的成功分支里，
  // 否则 A 失败、B 成功会把 A 的失败消息抹掉。
  errMsg.value = ''
  loadDetail(); loadCurve(); loadPatterns()
}
watch(symbol, reloadAll)
onMounted(reloadAll)
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
.badge.pat { font-size: 10px; padding: 1px 6px; white-space: nowrap; }
.badge.pat.big { font-size: 12px; padding: 4px 10px; font-weight: 700; }
.badge.pat.ok { background: rgba(246,70,93,.18); color: var(--up); }
.badge.pat.stale {
  background: rgba(120,132,150,.16); color: var(--text-dim);
}
.pat-now { border-color: rgba(246,70,93,.4); }
.pat-old { background: var(--bg-panel-2); }
td.num, th.num { text-align: right; }
.ind-chip {
  display: flex; flex-direction: column; gap: 2px; padding: 10px 16px;
  background: var(--bg-panel-2); border-radius: 6px; font-size: 12px;
}
.chart { height: 560px; }
.legend-note { font-size: 12px; margin-top: 8px; line-height: 1.7; }
</style>
