<template>
  <div class="review">
    <div v-if="errMsg" class="warnbar soft inset">{{ errMsg }}</div>

    <div class="panel">
      <div class="flex items-center gap-3" style="margin-bottom:12px">
        <h3 style="margin:0">每日复盘</h3>
        <span class="muted">{{ data?.date ? `数据日期 ${data.date}` : '加载中…' }}</span>
        <div class="spacer" />
        <label class="muted">日期
          <select v-model="date" style="margin-left:6px;min-width:130px" @change="applyQuery">
            <option value="">最新</option>
            <option v-for="d in availableDates" :key="d" :value="d">{{ d }}</option>
          </select>
        </label>
        <label class="muted">周期
          <select v-model.number="period" style="margin-left:6px" @change="applyQuery">
            <option v-for="p in periods" :key="p" :value="p">RPS{{ p }}</option>
          </select>
        </label>
        <label class="muted">信号阈值
          <input v-model.number="threshold" type="number" min="0" max="100"
                 style="width:74px;margin-left:6px" @change="applyQuery" />
        </label>
        <button class="btn" @click="load">刷新</button>
      </div>

      <p v-if="data && !data.date" class="muted">{{ data.message }}</p>

      <ReviewNote :date="data?.date || ''" />

      <template v-if="data?.breadth">
        <div class="stats">
          <div class="stat">
            <span class="muted">有行情</span>
            <strong>{{ num(data.breadth.traded) }}</strong>
          </div>
          <div class="stat">
            <span class="muted">上涨</span>
            <strong class="up">{{ num(data.breadth.up) }}</strong>
          </div>
          <div class="stat">
            <span class="muted">下跌</span>
            <strong class="down">{{ num(data.breadth.down) }}</strong>
          </div>
          <div class="stat">
            <span class="muted">平盘</span>
            <strong class="muted">{{ num(data.breadth.flat) }}</strong>
          </div>
          <div class="stat">
            <span class="muted">涨停</span>
            <strong class="up">{{ num(data.breadth.limit_up) }}</strong>
          </div>
          <div class="stat">
            <span class="muted">跌停</span>
            <strong class="down">{{ num(data.breadth.limit_down) }}</strong>
          </div>
          <div class="stat">
            <span class="muted">中位涨跌</span>
            <strong :class="(data.breadth.median_pct ?? 0) >= 0 ? 'up' : 'down'">
              {{ pct(data.breadth.median_pct == null ? null : data.breadth.median_pct / 100) }}
            </strong>
          </div>
        </div>
        <p class="note muted" style="margin-top:8px">
          「有行情」是当日有 K 线且未停牌的只数（停牌不计入分母）。
          涨停/跌停按**实测涨跌停比例 + 容差**判定，与回测、预警规则同一套口径。
        </p>
      </template>
    </div>

    <div v-if="data?.limit_up?.length" class="panel" style="margin-top:12px">
      <div class="flex items-center gap-3" style="margin-bottom:10px">
        <h3 style="margin:0">涨停板</h3>
        <span class="muted">{{ data.limit_up.length }} 只（按涨幅排序）</span>
      </div>
      <table class="w-table">
        <thead>
          <tr><th>代码</th><th>名称</th><th>行业</th>
            <th class="num">涨幅</th><th class="num">涨跌停比例</th><th class="num">连板</th></tr>
        </thead>
        <tbody>
          <tr v-for="x in data.limit_up" :key="x.symbol">
            <td class="mono">
              <router-link :to="stockLink(x.symbol)" class="mono">{{ x.symbol }}</router-link>
            </td>
            <td>{{ x.name || '—' }}</td>
            <td class="muted">{{ x.industry || '未分类' }}</td>
            <td class="num up">{{ pct(x.pct / 100) }}</td>
            <td class="num muted">{{ x.limit_ratio }}%</td>
            <td class="num">
              <span class="badge" :class="x.streak >= 2 ? 'ok' : 'dim'">{{ x.streak }} 板</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="data?.limit_down?.length" class="panel" style="margin-top:12px">
      <div class="flex items-center gap-3" style="margin-bottom:10px">
        <h3 style="margin:0">跌停板</h3>
        <span class="muted">{{ data.limit_down.length }} 只</span>
      </div>
      <table class="w-table">
        <thead>
          <tr><th>代码</th><th>名称</th><th>行业</th>
            <th class="num">跌幅</th><th class="num">涨跌停比例</th></tr>
        </thead>
        <tbody>
          <tr v-for="x in data.limit_down" :key="x.symbol">
            <td class="mono">
              <router-link :to="stockLink(x.symbol)" class="mono">{{ x.symbol }}</router-link>
            </td>
            <td>{{ x.name || '—' }}</td>
            <td class="muted">{{ x.industry || '未分类' }}</td>
            <td class="num down">{{ pct(x.pct / 100) }}</td>
            <td class="num muted">{{ x.limit_ratio }}%</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="data?.sectors?.length" class="panel" style="margin-top:12px">
      <div class="flex items-center gap-3" style="margin-bottom:10px">
        <h3 style="margin:0">行业当日涨跌</h3>
        <span class="muted">等权平均（全部 {{ data.sectors.length }} 个行业，列首尾各 10）</span>
      </div>
      <table class="w-table">
        <thead>
          <tr><th>行业</th><th class="num">平均涨跌</th>
            <th class="num">上涨家数</th><th class="num">家数</th></tr>
        </thead>
        <tbody>
          <tr v-for="s in sectorRows" :key="s.industry">
            <td>{{ s.industry }}</td>
            <td class="num" :class="s.avg_pct >= 0 ? 'up' : 'down'">{{ pct(s.avg_pct / 100) }}</td>
            <td class="num muted">{{ s.up }}</td>
            <td class="num muted">{{ s.n }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="data?.signals?.length" class="panel" style="margin-top:12px">
      <div class="flex items-center gap-3" style="margin-bottom:10px">
        <h3 style="margin:0">当日信号名单</h3>
        <span class="muted">{{ data.signals.length }} 只（按区间涨幅排序）</span>
      </div>
      <p class="note muted" style="margin-top:0">{{ data.signals_note }}</p>
      <table class="w-table">
        <thead>
          <tr><th>代码</th><th>名称</th><th>行业</th>
            <th class="num">RPS{{ data.period }}</th>
            <th class="num">区间涨幅</th><th class="num">当日涨跌</th></tr>
        </thead>
        <tbody>
          <tr v-for="s in data.signals" :key="s.symbol">
            <td class="mono">
              <router-link :to="stockLink(s.symbol)" class="mono">{{ s.symbol }}</router-link>
            </td>
            <td>{{ s.name || '—' }}</td>
            <td class="muted">{{ s.industry || '未分类' }}</td>
            <td class="num">{{ num(s.rps, 1) }}</td>
            <td class="num" :class="(s.period_return ?? 0) >= 0 ? 'up' : 'down'">
              {{ pct(s.period_return) }}
            </td>
            <td class="num" :class="(s.pct_chg ?? 0) >= 0 ? 'up' : 'down'">
              {{ pct(s.pct_chg) }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { metaApi, reviewApi } from '../api'
import { DEFAULT_PERIOD, usePeriods } from '../composables/usePeriods'
import { useLatestRequest } from '../composables/useLatestRequest'
import ReviewNote from '../components/ReviewNote.vue'
import { stockLink as makeStockLink } from '../composables/useStockLinks'

const route = useRoute()
const router = useRouter()
const { periods } = usePeriods()
const data = ref(null)
const errMsg = ref('')
// URL 是日期的**唯一真源**：在 setup 阶段读一次（不是 mount 后再写回控件）——
// mount 后写回会连带触发重挂载再请求一次。
const q0 = route.query
const qNum = (k, fallback) => (q0[k] != null && q0[k] !== '' ? Number(q0[k]) : fallback)
const date = ref(typeof q0.date === 'string' ? q0.date : '')
const period = ref(qNum('period', DEFAULT_PERIOD))
const threshold = ref(qNum('threshold', 80))
const availableDates = ref([])
const latest = useLatestRequest()

/**
 * 把当前筛选写进 URL：**只改 query、不发请求**。
 * App.vue 用 `$route.fullPath` 做 key，query 一变就重新挂载 → 由新实例加载一次。
 * 选好某天再点个股、再返回时，日期就靠这个 query 传过去。
 */
function applyQuery() {
  router.replace({
    path: '/review',
    query: {
      ...(date.value ? { date: date.value } : {}),
      ...(period.value !== DEFAULT_PERIOD ? { period: period.value } : {}),
      ...(threshold.value !== 80 ? { threshold: threshold.value } : {}),
    },
  })
}

/** 个股链接：带**生效日**与来源，返回时才能回到这天的复盘页。 */
function stockLink(symbol) {
  return makeStockLink(symbol, data.value?.date || '', 'review')
}

const sectorRows = computed(() => {
  const rows = data.value?.sectors || []
  return rows.length <= 20 ? rows : [...rows.slice(0, 10), ...rows.slice(-10)]
})

const num = (v, digits = 0) => (v == null || Number.isNaN(Number(v))
  ? '—'
  : Number(v).toLocaleString('zh-CN', {
    minimumFractionDigits: digits, maximumFractionDigits: digits,
  }))
const pct = (v) => (v == null || Number.isNaN(Number(v))
  ? '—'
  : `${(Number(v) * 100).toFixed(2)}%`)

async function load() {
  const t = latest.begin()
  errMsg.value = ''
  try {
    const d = await reviewApi.day({
      ...(date.value ? { date: date.value } : {}),
      period: period.value,
      threshold: threshold.value,
    })
    if (!latest.isCurrent(t)) return
    data.value = d
    // 回显生效日（最近交易日）到 URL：用户选周末时看到的仍是明确的一天
    if (d?.date && d.date !== date.value) {
      date.value = d.date
      applyQuery()
    }
  } catch (e) {
    if (!latest.isCurrent(t)) return
    console.error(e)
    errMsg.value = `复盘数据加载失败：${e?.message || e}`
  }
}

onMounted(() => {
  load()
  // 不传 limit：后端默认 2000，覆盖全部 rps 历史
  metaApi.dates()
    .then((ds) => { availableDates.value = ds || [] })
    .catch((e) => console.error('日期清单加载失败（不影响最新视图）', e))
})
</script>
