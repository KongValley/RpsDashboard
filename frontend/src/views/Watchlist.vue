<template>
  <div class="watchlist">
    <div v-if="errMsg" class="warnbar soft inset">{{ errMsg }}</div>

    <div class="panel">
      <div class="flex items-center gap-3" style="margin-bottom:12px">
        <h3 style="margin:0">自选股</h3>
        <span class="muted">
          在排行榜点行首星标加入；本页只做加/删与展示（预警命中在下方）
        </span>
        <div class="spacer" />
        <button class="btn" @click="reload">刷新</button>
      </div>

      <table class="w-table" v-if="items.length">
        <thead>
          <tr>
            <th>代码</th><th>名称</th><th>行业</th>
            <th class="num">RPS20</th><th class="num">RPS60</th>
            <th class="num">RPS120</th><th class="num">RPS250</th>
            <th>备注</th><th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in items" :key="r.symbol" @click="goDetail(r.symbol)">
            <td class="mono">{{ r.symbol }}</td>
            <td><span v-if="r.is_st" class="badge st">ST</span>{{ r.name || '—' }}</td>
            <td class="muted">{{ r.industry || '未分类' }}</td>
            <td class="num">{{ num(r.rps20) }}</td>
            <td class="num">{{ num(r.rps60) }}</td>
            <td class="num">{{ num(r.rps120) }}</td>
            <td class="num">{{ num(r.rps250) }}</td>
            <td class="muted">{{ r.note || '' }}</td>
            <td>
              <button class="btn" @click.stop="remove(r.symbol)">移除</button>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-else class="muted">还没有自选股：到「排行榜」点行首的 ☆ 即可加入。</p>
    </div>

    <div class="panel" style="margin-top:12px">
      <div class="flex items-center gap-3" style="margin-bottom:12px">
        <h3 style="margin:0">条件预警</h3>
        <span class="muted">
          规则在服务端评估（`cli alert run`，或每次「更新数据」的末尾一步），
          这里只展示规则与命中记录，不做消息推送
        </span>
      </div>

      <div class="flex items-center gap-2" style="margin-bottom:10px;flex-wrap:wrap">
        <input v-model.trim="newRule.name" type="text" placeholder="规则名，如 RPS90" style="width:150px" />
        <select v-model="newRule.kind">
          <option v-for="k in kinds" :key="k" :value="k">{{ k }}</option>
        </select>
        <input v-model.trim="newRule.params" type="text"
               placeholder="参数 JSON，如 {&quot;period&quot;:60,&quot;min&quot;:90,&quot;max&quot;:100}"
               style="width:340px" />
        <button class="btn" @click="addRule">新增规则</button>
      </div>

      <table class="w-table" v-if="rules.length">
        <thead>
          <tr><th>#</th><th>规则名</th><th>类型</th><th>参数</th><th class="num">累计命中</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="r in rules" :key="r.id">
            <td class="mono">{{ r.id }}</td>
            <td>{{ r.name }}</td>
            <td class="mono muted">{{ r.kind }}</td>
            <td class="mono muted">{{ JSON.stringify(r.params) }}</td>
            <td class="num">{{ r.hits }}</td>
            <td><button class="btn" @click="removeRule(r.id)">删除</button></td>
          </tr>
        </tbody>
      </table>
      <p v-else class="muted">还没有规则。</p>

      <h4 style="margin:16px 0 8px">
        命中记录 <span class="muted" style="font-weight:400;font-size:12px">（{{ hitDate || '—' }}）</span>
      </h4>
      <table class="w-table" v-if="hits.length">
        <thead>
          <tr><th>规则</th><th>代码</th><th>名称</th><th>说明</th></tr>
        </thead>
        <tbody>
          <tr v-for="h in hits" :key="`${h.rule_id}-${h.symbol}`" @click="goDetail(h.symbol)">
            <td class="muted">{{ h.rule_name }}</td>
            <td class="mono">{{ h.symbol }}</td>
            <td>{{ h.name || '—' }}</td>
            <td class="muted">{{ h.detail }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="muted">这一天没有命中记录。</p>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { watchApi, alertApi } from '../api'

const router = useRouter()
const items = ref([])
const rules = ref([])
const hits = ref([])
const hitDate = ref(null)
const kinds = ref([])
const errMsg = ref('')
const newRule = ref({ name: '', kind: 'rps_threshold', params: '{"period":60,"min":90,"max":100}' })

const num = (v) => (v == null ? '—' : Number(v).toFixed(0))

async function reload() {
  errMsg.value = ''
  try {
    items.value = await watchApi.list()
  } catch (e) {
    errMsg.value = `自选列表加载失败：${e?.message || e}`
  }
  try {
    rules.value = await alertApi.rules()
  } catch (e) {
    errMsg.value = `预警规则加载失败：${e?.message || e}`
  }
  try {
    hits.value = await alertApi.hits({})
    hitDate.value = hits.value.length ? hits.value[0].date : null
  } catch (e) {
    errMsg.value = `命中记录加载失败：${e?.message || e}`
  }
}

async function remove(symbol) {
  try {
    await watchApi.remove(symbol)
    items.value = items.value.filter((r) => r.symbol !== symbol)
  } catch (e) {
    errMsg.value = `移除失败：${e?.message || e}`
  }
}

async function addRule() {
  // 前端先校验 JSON，避免把明显写错的东西送上去
  let params
  try {
    params = JSON.parse(newRule.value.params || '{}')
  } catch (e) {
    errMsg.value = `参数不是合法 JSON：${e.message}`
    return
  }
  try {
    await alertApi.addRule({ name: newRule.value.name, kind: newRule.value.kind, params })
    await reload()
  } catch (e) {
    errMsg.value = `新增规则失败：${e?.message || e}`
  }
}

async function removeRule(id) {
  try {
    await alertApi.removeRule(id)
    await reload()
  } catch (e) {
    errMsg.value = `删除规则失败：${e?.message || e}`
  }
}

function goDetail(symbol) {
  router.push(`/stock/${symbol}`)
}

onMounted(async () => {
  try {
    kinds.value = await alertApi.kinds()
    if (kinds.value.length) newRule.value.kind = kinds.value[0]
  } catch (e) {
    console.error('预警类型加载失败', e)
  }
  await reload()
})
</script>

<style scoped>
.spacer { flex: 1; }
.w-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.w-table th {
  text-align: left; font-weight: 500; color: var(--text-dim);
  border-bottom: 1px solid var(--border); padding: 6px 8px; font-size: 12px;
}
.w-table td { padding: 6px 8px; border-bottom: 1px solid var(--border); }
.w-table tbody tr { cursor: pointer; }
.w-table tbody tr:hover { background: var(--bg-panel-2); }
.w-table .num { text-align: right; }
.badge.st {
  background: rgba(246, 70, 93, .2); color: var(--up);
  margin-right: 5px; font-size: 10px; padding: 1px 5px; border-radius: 3px;
}
</style>
