<template>
  <div class="rankings">
    <div v-if="errMsg" class="errbar">接口错误：{{ errMsg }}</div>
    <div v-if="patternMissing && sanxingdi" class="errbar warn">
      形态数据尚未计算，三升底筛选结果不可用。请先执行
      <code>python -m rpsboard.cli patterns</code>
    </div>
    <div class="panel filters">
      <div class="row">
        <div class="group">
          <label>周期</label>
          <button
            v-for="p in periods" :key="p"
            class="btn" :class="{ active: period === p }"
            @click="setPeriod(p)"
          >{{ p }}日</button>
        </div>

        <div class="group">
          <label>RPS ≥ <b class="mono">{{ rpsMin }}</b></label>
          <input type="range" min="0" max="100" step="1" v-model.number="rpsMin" />
          <button class="btn" @click="rpsMin = 80">80</button>
          <button class="btn" @click="rpsMin = 90">90</button>
        </div>

        <div class="group">
          <label>行业</label>
          <select v-model="industry" style="min-width:140px">
            <option value="">全部行业</option>
            <option v-for="i in industries" :key="i" :value="i">{{ i }}</option>
          </select>
        </div>

        <div class="group">
          <label>成交额 ≥</label>
          <select v-model.number="minAmount">
            <option :value="0">不限</option>
            <option :value="1e8">1亿</option>
            <option :value="5e8">5亿</option>
            <option :value="1e9">10亿</option>
          </select>
        </div>

        <div class="group">
          <label>Δ名次对比</label>
          <select v-model.number="deltaDays" style="min-width:104px">
            <option :value="0">不对比</option>
            <option :value="1">前 1 日</option>
            <option :value="5">前 5 日</option>
            <option :value="20">前 20 日</option>
            <option :value="60">前 60 日</option>
          </select>
        </div>

        <div class="group">
          <label title="三升底：连续三根阳线、收盘逐根抬高、第一根实体不超过第三根一半、且第三根收盘突破第一根最高价">
            三升底
          </label>
          <select v-model.number="sanxingdi" style="min-width:118px">
            <option :value="0">不筛选</option>
            <option :value="1">仅最新日</option>
            <option :value="5">近 5 日</option>
            <option :value="10">近 10 日</option>
            <option :value="20">近 20 日</option>
          </select>
        </div>

        <div class="group">
          <label>数据日期</label>
          <select v-model="date" style="min-width:118px">
            <option value="">最新（{{ dataDate || '—' }}）</option>
            <option v-for="d in availableDates" :key="d" :value="d">{{ d }}</option>
          </select>
        </div>

        <div class="group">
          <label class="chk">
            <input type="checkbox" v-model="excludeSt" /> 剔除ST
          </label>
        </div>
      </div>

      <div class="row meta">
        <span class="muted">
          数据日期 <b>{{ dataDate || '—' }}</b> · 共 <b>{{ total.toLocaleString() }}</b> 只
          <span v-if="total > rows.length">（显示第 {{ (page - 1) * pageSize + 1 }}–{{ rows.length + (page - 1) * pageSize }} 名）</span>
        </span>
        <div class="spacer" />
        <button class="btn" :disabled="page === 1" @click="page--; load()">上一页</button>
        <span class="muted">第 {{ page }} 页</span>
        <button class="btn" :disabled="rows.length < pageSize" @click="page++; load()">下一页</button>
        <button class="btn" :disabled="exporting" @click="exportCsv">
          {{ exporting ? '导出中…' : '导出筛选结果' }}
        </button>
      </div>
    </div>

    <div class="panel" style="margin-top:12px;padding:0">
      <RpsTable :rows="rows" :page="page" :page-size="pageSize"
                :loading="loading" :show-delta="deltaDays > 0"
                :show-pattern="sanxingdi > 0" :watched="watched"
                @select="goDetail" @toggle-watch="toggleWatch" />
    </div>
  </div>
</template>

<script setup>
import { ref, watch, onMounted } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { rpsApi, metaApi, watchApi } from '../api'
import RpsTable from '../components/RpsTable.vue'

const router = useRouter()
const route = useRoute()

/** 自选股代码集合（行首星标用） */
const watched = ref([])

async function loadWatchlist() {
  try {
    watched.value = (await watchApi.list()).map((r) => r.symbol)
  } catch (e) {
    console.error('自选列表加载失败', e)
  }
}

/** 星标点击：立刻改本地状态（点击手感），再落库；失败时回滚并提示 */
async function toggleWatch(symbol) {
  const has = watched.value.includes(symbol)
  watched.value = has
    ? watched.value.filter((s) => s !== symbol)
    : [...watched.value, symbol]
  try {
    if (has) await watchApi.remove(symbol)
    else await watchApi.add(symbol)
  } catch (e) {
    watched.value = has
      ? [...watched.value, symbol]
      : watched.value.filter((s) => s !== symbol)
    errMsg.value = `自选更新失败：${e?.message || e}`
  }
}

const periods = ref([20, 60, 120, 250])
const period = ref(60)
const rpsMin = ref(80)
const industry = ref('')
const minAmount = ref(0)
const excludeSt = ref(true)
const pageSize = 100
const page = ref(1)
/** 空 = 用最新交易日；非空 = 回看指定历史日 */
const date = ref('')
const availableDates = ref([])
/** 0 = 不对比名次；>0 = 与 N 个交易日前的名次比较 */
const deltaDays = ref(0)
/** 0 = 不筛选三升底；>0 = 近 N 个交易日（含当日）内出现过三升底 */
const sanxingdi = ref(0)

const industries = ref([])
const rows = ref([])
const total = ref(0)
const dataDate = ref(null)
const loading = ref(false)
const exporting = ref(false)
const patternMissing = ref(false)
const errMsg = ref('')

async function load() {
  loading.value = true
  errMsg.value = ''
  try {
    const data = await rpsApi.ranking({
      date: date.value || undefined,
      period: period.value,
      rps_min: rpsMin.value,
      rps_max: 100,
      industries: industry.value || undefined,
      exclude_st: excludeSt.value,
      min_amount: minAmount.value || undefined,
      limit: pageSize,
      page: page.value,
      delta_days: deltaDays.value || undefined,
      sanxingdi: sanxingdi.value || undefined,
    })
    if (!data || typeof data !== 'object' || !('rows' in data)) {
      throw new Error(`响应结构异常: ${JSON.stringify(data).slice(0, 120)}`)
    }
    rows.value = data.rows || []
    total.value = data.total || 0
    // 后端会把非交易日回退到最近的有数据日期，这里只展示实际生效的日期，
    // **不写回 date.value**——否则会触发 watch(date) 形成重复请求。
    dataDate.value = data.data_date
    // 形态表还没算过时，后端会带 pattern_missing，必须提示，
    // 否则用户会以为「市场里根本没有三升底」。
    patternMissing.value = !!data.pattern_missing
  } catch (e) {
    console.error(e)
    errMsg.value = e?.message || String(e)
  } finally {
    loading.value = false
  }
}

function setPeriod(p) {
  period.value = p
  page.value = 1
  load()
}

function goDetail(symbol) {
  router.push(`/stock/${symbol}`)
}

/**
 * 导出**当前筛选条件下的全量** CSV。
 * 早期版本导的是 `rows.value`（当前页 100 行），按钮却写「导出 CSV」，
 * 用户会以为拿到了全市场 —— 现在由服务端按同一套筛选条件出全量。
 */
async function exportCsv() {
  exporting.value = true
  try {
    await rpsApi.exportRanking({
      date: date.value || undefined,
      period: period.value,
      rps_min: rpsMin.value,
      rps_max: 100,
      industries: industry.value || undefined,
      exclude_st: excludeSt.value,
      min_amount: minAmount.value || undefined,
      sort: 'rps',
      // 形态筛选必须一起传：否则用户开着「三升底」点导出，拿到的是未过滤的文件
      sanxingdi: sanxingdi.value || undefined,
    }, `rps${period.value}_${dataDate.value || 'latest'}.csv`)
  } catch (e) {
    alert(`导出失败：${e.message}`)
  } finally {
    exporting.value = false
  }
}

watch([industry, minAmount, excludeSt], () => { page.value = 1; load() })
watch(rpsMin, () => { page.value = 1; load() })
watch(date, () => { page.value = 1; load() })
watch(deltaDays, () => { page.value = 1; load() })
watch(sanxingdi, () => { page.value = 1; load() })

// 主数据与元数据**并行独立**加载：筛选项没就绪不该阻塞排行榜。
// （早期写成 `await Promise.all([periods, industries])` 再 load()，
//   一旦元数据请求挂起，整个排行榜就永远停在「加载中」。）
onMounted(() => {
  load()
  loadWatchlist()
  // 不传 limit：后端默认 2000，覆盖全部 rps 历史（此前写死 400 会把日期下拉截断）
  Promise.all([metaApi.periods(), metaApi.industries(), metaApi.dates()])
    .then(([ps, inds, dates]) => {
      periods.value = ps
      industries.value = inds
      availableDates.value = dates || []
    })
    .catch((e) => console.error('元数据加载失败（不影响排行）', e))
  // 从板块页点格子跳过来时带着 ?industries=xxx，这里消费它。
  // 早期这里只 useRouter 从不 useRoute，导致「点击格子查看该行业榜单」点了没反应。
  const q = route.query
  if (q.industries) industry.value = String(q.industries).split(',').filter(Boolean)[0] || ''
  if (q.date) date.value = String(q.date)
  if (q.period) period.value = Number(q.period)
  if (q.rps_min) rpsMin.value = Number(q.rps_min)
})
</script>

<style scoped>
.filters { display: flex; flex-direction: column; gap: 12px; }
.errbar {
  background: rgba(246,70,93,.12); border: 1px solid rgba(246,70,93,.4);
  color: var(--up); border-radius: 6px; padding: 10px 14px;
  font-size: 12px; margin-bottom: 10px; word-break: break-all;
}
.row { display: flex; align-items: center; gap: 20px; flex-wrap: wrap; }
.group { display: flex; align-items: center; gap: 6px; }
.group label { color: var(--text-dim); font-size: 13px; margin-right: 2px; }
.group input[type="range"] { width: 150px; }
.chk { display: flex; align-items: center; gap: 5px; cursor: pointer; }
.meta { border-top: 1px solid var(--border); padding-top: 12px; font-size: 13px; }
.spacer { flex: 1; }
</style>
