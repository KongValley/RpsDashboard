/**
 * 打印构建产物里某个关键字周围的原始（压缩后）代码片段。
 * 用来确认 fetch 调用到底是怎么拼出来的 —— 尤其是 URL 前缀和 base。
 *
 * 用法：node backend/tests/dump_ctx.mjs <关键字> [上下文字符数] [文件]
 */
import { readFileSync } from 'node:fs'
import { join, resolve } from 'node:path'

const key = process.argv[2]
const span = Number(process.argv[3] ?? 400)
const file = process.argv[4] ?? 'index-DP6s0SPa.js'
if (!key) {
  console.error('用法: node dump_ctx.mjs <关键字> [上下文字符数] [文件名]')
  process.exit(1)
}

const dir = resolve('frontend/dist/assets')
const path = join(dir, file)
const code = readFileSync(path, 'utf8')

let from = 0
let n = 0
while (true) {
  const i = code.indexOf(key, from)
  if (i === -1) break
  n += 1
  const start = Math.max(0, i - span)
  const end = Math.min(code.length, i + key.length + span)
  console.log(`\n===== 第 ${n} 处命中 @${i} (${start}..${end}) =====`)
  console.log(code.slice(start, end))
  from = i + key.length
  if (n >= 6) {
    console.log('\n(命中过多，只显示前 6 处)')
    break
  }
}
if (!n) console.log(`未命中「${key}」`)
else console.log(`\n共 ${n} 处命中`)