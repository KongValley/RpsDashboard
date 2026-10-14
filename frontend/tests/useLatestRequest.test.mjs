/**
 * 竞态守卫的行为测试（node:test，零第三方依赖：`npm test`）。
 *
 * 守的是真实缺陷：快速连点周期按钮/拖滑块时多个请求同时在飞，响应**不保证**
 * 按发出顺序回来，后回来的旧响应会把新参数的结果覆盖掉 —— 表格看着「不跟手」，
 * 实际显示的是几秒前那次查询的数据。
 */

import test from 'node:test'
import assert from 'node:assert/strict'

import { useLatestRequest } from '../src/composables/useLatestRequest.js'

test('乱序返回：晚到的旧响应必须被丢弃', async () => {
  const latest = useLatestRequest()
  const applied = []

  const slow = (async () => {
    const t = latest.begin()
    await new Promise((r) => setTimeout(r, 20))
    if (latest.isCurrent(t)) applied.push('旧请求')
  })()
  const fast = (async () => {
    const t = latest.begin()
    await new Promise((r) => setTimeout(r, 1))
    if (latest.isCurrent(t)) applied.push('新请求')
  })()

  await Promise.all([slow, fast])
  assert.deepEqual(applied, ['新请求'], '只有最后一次请求的结果可以写回状态')
})

test('顺序请求逐个生效', () => {
  const latest = useLatestRequest()
  const applied = []
  for (const name of ['A', 'B', 'C']) {
    const t = latest.begin()
    if (latest.isCurrent(t)) applied.push(name)
  }
  assert.deepEqual(applied, ['A', 'B', 'C'])
})

test('两个实例各自维护序号（互不干扰）', () => {
  const ranking = useLatestRequest()
  const detail = useLatestRequest()
  const t1 = ranking.begin()
  const d1 = detail.begin()
  const t2 = ranking.begin()   // 排行榜发了新请求 → 只有 t1 作废
  assert.equal(ranking.isCurrent(t1), false)
  assert.equal(ranking.isCurrent(t2), true)
  assert.equal(detail.isCurrent(d1), true, '别的实例发请求不影响本实例的令牌')
})

test('begin 单调递增：旧令牌永久失效', () => {
  const latest = useLatestRequest()
  const first = latest.begin()
  const second = latest.begin()
  assert.equal(latest.isCurrent(first), false)
  assert.equal(latest.isCurrent(second), true)
})
