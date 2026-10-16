<template>
  <div class="factors">
    <div v-if="errMsg" class="warnbar soft inset">{{ errMsg }}</div>

    <div v-if="!data" class="panel"><p class="muted">加载中…</p></div>

    <div v-else-if="!data.factors.length" class="panel">
      <p class="muted">
        还没有因子评估：先执行 <span class="mono">cli factors</span>
        （本机约 3~5 分钟，要读全量日 K 并建 13 个全市场矩阵），跑完刷新本页即可。
      </p>
    </div>

    <template v-else>
      <div class="panel">
        <div class="flex items-center gap-3" style="margin-bottom:10px">
          <h3 style="margin:0">因子评估</h3>
          <span class="muted">
            {{ data.run_at || '—' }}｜period {{ data.params.period }}｜
            hold {{ data.params.hold }}｜全样本研究口径
          </span>
          <div class="spacer" />
          <button class="btn" @click="load">刷新</button>
        </div>
        <div class="note muted" v-for="(n, i) in data.notes" :key="i">· {{ n }}</div>

        <table class="w-table" style="margin-top:10px">
          <thead>
            <tr>
              <th style="width:110px">因子</th><th>说明</th>
              <th class="num" style="width:90px">IC均值</th>
              <th class="num" style="width:80px">IC_IR</th>
              <th class="num" style="width:80px">胜率</th>
              <th class="num" style="width:90px">样本</th>
              <th class="num" style="width:90px">自相关</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="f in data.factors" :key="f.name">
              <td class="mono">{{ f.name }}</td>
              <td class="muted">{{ f.label }}</td>
              <td class="num" :class="vcls(f.ic_mean)">{{ num(f.ic_mean) }}</td>
              <td class="num"><span :class="irBadge(f.ic_ir)">{{ num(f.ic_ir, 2) }}</span></td>
              <td class="num muted">{{ f.ic_win == null ? '—' : (f.ic_win * 100).toFixed(1) + '%' }}</td>
              <td class="num muted">{{ (f.samples || 0).toLocaleString() }}</td>
              <td class="num muted">{{ num(f.autocorr, 3) }}</td>
            </tr>
          </tbody>
        </table>
        <p class="note muted" style="margin-top:8px">
          自相关是换手代理：越低说明排名换得越快、交易成本越高。
          样本 = 有效 IC 天数（横截面不足 100 只票的交易日不计入）。
        </p>
      </div>

      <div class="panel" style="margin-top:12px">
        <div class="flex items-center gap-3" style="margin-bottom:6px">
          <h3 style="margin:0">分层前瞻收益</h3>
          <select v-model="picked" style="width:230px">
            <option v-for="f in data.factors" :key="f.name" :value="f.name">
              {{ f.name }} · {{ f.label }}
            </option>
          </select>
          <span class="muted">零成本口径；分档依据是该因子当日的横截面百分位</span>
        </div>
        <VChart v-if="hasBuckets" class="chart" :option="bucketOption" autoresize />
        <p v-else class="muted">这个因子没有可用的分层样本（标准化后整列为空）。</p>
      </div>

      <div class="panel" style="margin-top:12px">
        <h3 style="margin:0 0 4px">因子相关矩阵</h3>
        <p class="note muted" style="margin-top:0">
          时间平均的横截面相关（只用两个因子同时有效的样本）。
          |corr| ≥ 0.7 说明这两个基本是同一个赌注，合成时不宜重复计入。
        </p>
        <VChart class="chart corr" :option="corrOption" autoresize />
      </div>
    </template>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { factorApi } from '../api'

const data = ref(null)
/** 分层图当前展示的因子（默认后端排在最前的那个） */
const picked = ref('')
const errMsg = ref('')

async function load() {
  errMsg.value = ''
  try {
    const d = await factorApi.latest()
    data.value = d
    if (!d.factors?.some((f) => f.name === picked.value)) {
      picked.value = d.factors?.[0]?.name || ''
    }
  } catch (e) {
    errMsg.value = `因子评估读取失败：${e?.message || e}`
    data.value = null
  }
}

const current = computed(
  () => data.value?.factors.find((f) => f.name === picked.value) || null)

const hasBuckets = computed(
  () => (current.value?.buckets || []).some((x) => x.avg_forward_return != null))

const bucketOption = computed(() => {
  const b = (current.value?.buckets || []).filter((x) => x.avg_forward_return != null)
  return {
    tooltip: {
      trigger: 'axis',
      formatter: (ps) => {
        const row = b[ps[0].dataIndex]
        return `${row.range} 分位<br/>平均前瞻收益 ${(row.avg_forward_return * 100).toFixed(2)}%`
          + `<br/>上涨概率 ${(row.win_rate * 100).toFixed(1)}%<br/>样本 ${row.samples.toLocaleString()}`
      },
    },
    legend: { data: ['平均前瞻收益', '上涨概率'], textStyle: { color: '#8b98a9' }, top: 0 },
    grid: { left: 60, right: 60, top: 34, bottom: 40 },
    xAxis: { type: 'category', data: b.map((x) => x.range),
      axisLabel: { color: '#8b98a9' }, axisLine: { lineStyle: { color: '#2a323d' } } },
    yAxis: [
      { type: 'value', axisLabel: { color: '#8b98a9', formatter: (v) => (v * 100).toFixed(0) + '%' },
        splitLine: { lineStyle: { color: 'rgba(42,50,61,.6)' } } },
      { type: 'value', min: 0, max: 1, axisLabel: { color: '#8b98a9', formatter: (v) => (v * 100).toFixed(0) + '%' },
        splitLine: { show: false } },
    ],
    series: [
      { name: '平均前瞻收益', type: 'bar', data: b.map((x) => x.avg_forward_return),
        itemStyle: { color: (p) => (p.value >= 0 ? '#f6465d' : '#2ebd85'), borderRadius: [3, 3, 0, 0] },
        barMaxWidth: 46 },
      { name: '上涨概率', type: 'line', yAxisIndex: 1,
        data: b.map((x) => x.win_rate), showSymbol: true, symbolSize: 7,
        lineStyle: { color: '#f0a020', width: 2 }, itemStyle: { color: '#f0a020' } },
    ],
  }
})

const corrOption = computed(() => {
  const c = data.value?.corr || { names: [], matrix: [] }
  const cells = []
  c.names.forEach((_, i) => c.names.forEach((__, j) => {
    // 对角线是恒等 1，画上去只会把 colour ramp 拉平，直接留空
    if (i === j) return
    const v = c.matrix?.[i]?.[j]
    if (v != null) cells.push([i, j, v])
  }))
  return {
    tooltip: {
      formatter: (p) => `${c.names[p.value[0]]} × ${c.names[p.value[1]]}<br/>`
        + `corr ${Number(p.value[2]).toFixed(2)}`,
    },
    grid: { left: 100, right: 70, top: 16, bottom: 80 },
    xAxis: { type: 'category', data: c.names,
      axisLabel: { color: '#8b98a9', fontSize: 10, rotate: 45 },
      axisLine: { lineStyle: { color: '#2a323d' } } },
    yAxis: { type: 'category', data: c.names,
      axisLabel: { color: '#8b98a9', fontSize: 10 },
      axisLine: { lineStyle: { color: '#2a323d' } } },
    visualMap: {
      min: -1, max: 1, calculable: true, orient: 'vertical', right: 0, top: 'center',
      inRange: { color: ['#2ebd85', '#1b2430', '#f6465d'] },
      textStyle: { color: '#8b98a9' },
    },
    series: [{
      type: 'heatmap', data: cells,
      emphasis: { itemStyle: { borderColor: '#e6edf3', borderWidth: 1 } },
    }],
  }
})

function num(v, digits = 4) { return v == null ? '—' : Number(v).toFixed(digits) }
function vcls(v) { return v == null ? '' : (v >= 0 ? 'up' : 'down') }
/** |IC_IR| ≥ 0.3 算可用、≥ 0.5 算强（IC_IR 是「信号 / 噪声」的比值） */
function irBadge(v) {
  if (v == null) return ''
  const a = Math.abs(v)
  return a >= 0.5 ? 'badge ok strong' : (a >= 0.3 ? 'badge ok' : '')
}

onMounted(load)
</script>

<style scoped>
.spacer { flex: 1; }
.note { font-size: 12px; line-height: 1.7; }
.chart { height: 320px; }
.chart.corr { height: 520px; }

.w-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.w-table th {
  text-align: left; font-weight: 500; color: var(--text-dim);
  border-bottom: 1px solid var(--border); padding: 6px 8px; font-size: 12px;
}
.w-table td { padding: 6px 8px; border-bottom: 1px solid var(--border); }
.w-table .num { text-align: right; }
.badge.ok { background: rgba(56, 189, 148, .18); color: var(--down); }
.badge.ok.strong { font-weight: 700; }
</style>
