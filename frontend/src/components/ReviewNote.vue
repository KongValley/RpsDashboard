<template>
  <div class="review-note">
    <div class="flex items-center gap-3" style="margin-bottom:6px">
      <label :for="fieldId" class="muted">
        {{ symbol ? `个股笔记 · ${symbol}` : '当日复盘笔记' }}
      </label>
      <span v-if="effectiveDate" class="muted mono">{{ effectiveDate }}</span>
      <div class="spacer" />
      <span v-if="saving" class="muted">保存中…</span>
      <span v-else-if="dirty" class="muted warn">有未保存的修改</span>
      <span v-else-if="savedAt" class="muted">已保存 {{ savedAt }}</span>
      <button class="btn" :disabled="!dirty || saving || !effectiveDate"
              @click="save()">保存</button>
    </div>
    <textarea :id="fieldId" v-model="text" rows="4" class="note-input"
              :disabled="!effectiveDate"
              :placeholder="effectiveDate
                ? '今天市场/这只票的判断、计划、以及事后要复盘的点…'
                : '数据尚未就绪，无法锚定笔记日期'"></textarea>
    <div v-if="message" class="warnbar soft inset" style="margin-top:6px">{{ message }}</div>
    <div v-if="effectiveDate" class="muted note-hint">
      笔记存在**实际生效的交易日**上；填非交易日会被后端拒绝并提示最近的交易日。
    </div>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate } from 'vue-router'
import { reviewApi } from '../api'

const props = defineProps({
  /** 生效交易日（YYYY-MM-DD）。null/空 = 数据未就绪，只读。 */
  date: { type: String, default: '' },
  /** '' = 市场笔记；6 位代码 = 个股笔记。 */
  symbol: { type: String, default: '' },
})

const text = ref('')
/** 已保存的基线：dirty = 当前文本与基线不同。 */
const baseline = ref('')
const effectiveDate = ref('')
const savedAt = ref('')
const saving = ref(false)
const message = ref('')
const fieldId = computed(() => `note-${props.symbol || 'market'}-${props.date || 'none'}`)
const dirty = computed(() => text.value !== baseline.value)

/** 每次请求带一个序号：慢请求回来时不能覆盖新日期/新股票的文本。 */
let seq = 0

async function load() {
  const t = ++seq
  message.value = ''
  savedAt.value = ''
  if (!props.date) {
    text.value = ''
    baseline.value = ''
    effectiveDate.value = ''
    return
  }
  try {
    const d = await reviewApi.note({
      ...(props.date ? { date: props.date } : {}),
      ...(props.symbol ? { symbol: props.symbol } : {}),
    })
    if (t !== seq) return            // 期间切换了日期/股票 → 丢弃这次结果
    text.value = d.note || ''
    baseline.value = text.value
    effectiveDate.value = d.date || ''
    savedAt.value = d.updated_at || ''
  } catch (e) {
    if (t !== seq) return
    message.value = `笔记加载失败：${e?.message || e}`
    text.value = ''
    baseline.value = ''
    effectiveDate.value = ''
  }
}

async function save() {
  if (!dirty.value || saving.value || !effectiveDate.value) return true
  // 提交时冻结一份快照：保存期间用户继续输入不能影响这次提交，
  // 也不能让响应把新输入标成「已保存」。
  const snapshot = { date: effectiveDate.value, symbol: props.symbol, note: text.value }
  saving.value = true
  message.value = ''
  try {
    const r = await reviewApi.saveNote(snapshot)
    if (snapshot.note === text.value) {
      baseline.value = r.note ?? snapshot.note
      savedAt.value = r.updated_at || ''
    } else {
      // 用户在保存期间又改了：基线推进到已保存的那一版，仍保持 dirty
      baseline.value = r.note ?? snapshot.note
      savedAt.value = r.updated_at || ''
    }
    if (r.date && r.date !== effectiveDate.value) effectiveDate.value = r.date
    return true
  } catch (e) {
    message.value = `保存失败（内容已保留）：${e?.message || e}`
    return false
  } finally {
    saving.value = false
  }
}

function beforeUnload(e) {
  if (!dirty.value) return
  e.preventDefault()
  e.returnValue = ''
}

watch(() => [props.date, props.symbol], load, { immediate: true })
watch(dirty, (v) => {
  if (typeof window === 'undefined') return
  if (v) window.addEventListener('beforeunload', beforeUnload)
  else window.removeEventListener('beforeunload', beforeUnload)
})

/**
 * 未保存就离开时统一拦一次（站内日期/股票切换、顶栏导航、前后退）。
 * 保存失败时**不放行**，草稿留在原页 —— 静默丢稿是最难被用户接受的行为。
 */
async function guard(to) {
  if (!dirty.value) return true
  const choice = window.confirm(
    '这条笔记还没保存。\n\n确定 = 保存并继续\n取消 = 放弃修改并继续')
  if (!choice) return true                // 放弃：直接走
  const ok = await save()
  if (!ok) return false                   // 保存失败：留在原页保留草稿
  return to ? true : true
}

onBeforeRouteUpdate(guard)
onBeforeRouteLeave(guard)

defineExpose({ save, dirty, text })
</script>

<style scoped>
.review-note { margin-top: 10px; }
.note-input {
  width: 100%; box-sizing: border-box; resize: vertical;
  font-family: inherit; line-height: 1.6;
}
.note-hint { font-size: 11px; margin-top: 4px; }
.warn { color: var(--warn); }
</style>
