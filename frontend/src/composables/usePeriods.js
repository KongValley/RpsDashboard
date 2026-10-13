/**
 * 周期清单的唯一来源：从 /api/meta/periods 拉一次，模块级缓存给所有视图共用。
 *
 * 刻意不写死数组（原来 5 个视图各写一份 [20,60,120,250]）：后端 config.PERIODS
 * 才是真源，新增周期时前端不该改代码。首次渲染的几百毫秒里 periods 为空，
 * 视图用 `v-if="periods.length"` 之类的守卫即可，不要退回写死兜底。
 */
import { ref } from 'vue'
import { metaApi } from '../api'

/** 默认周期（与后端 config.DEFAULT_PERIOD 一致） */
export const DEFAULT_PERIOD = 60

const periods = ref([])
const error = ref('')
let inflight = null

function load() {
  if (inflight) return inflight
  inflight = metaApi.periods()
    .then((ps) => { periods.value = ps || [] })
    .catch((e) => {
      // 失败时保留空清单并报出来（视图已有错误条）；允许下次调用重试。
      console.error('周期清单加载失败', e)
      error.value = `周期清单加载失败：${e?.message || e}`
      inflight = null
    })
  return inflight
}

/** @returns {{ periods: import('vue').Ref<number[]>, error: import('vue').Ref<string> }} */
export function usePeriods() {
  load()
  return { periods, error }
}
