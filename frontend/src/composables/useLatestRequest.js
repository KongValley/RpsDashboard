/**
 * 「只认最后一次请求」的竞态守卫。
 *
 * 场景：快速连点周期按钮/拖 RPS 滑块时会有多个请求同时在飞，而响应**不保证**
 * 按发出顺序回来（后端快的先回、慢的后回）。后回来的旧响应会把新参数的结果
 * 覆盖掉 —— 表格看起来「不跟手」，实际是显示了几秒前那次查询的数据。
 *
 * 用法（每个数据源一个实例，不要跨数据源共用）：
 *
 *     const latest = useLatestRequest()
 *     async function load() {
 *       const t = latest.begin()
 *       const rows = await api.xxx()
 *       if (!latest.isCurrent(t)) return   // 期间又发了新请求 → 丢弃这次结果
 *       state.value = rows
 *     }
 */

export function useLatestRequest() {
  let seq = 0
  return {
    /** 开始一次请求，返回令牌 */
    begin: () => ++seq,
    /** 令牌是否仍是最后一次（不是就说明结果过期了） */
    isCurrent: (t) => t === seq,
  }
}
