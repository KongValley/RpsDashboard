/**
 * 检查实际下发到浏览器的构建产物里，是否真的含有发起 /api 请求的代码。
 *
 * 背景：页面渲染出 Vue 模板（说明 app 已 mount），但后端 access log 里
 * 始终没有 /api/meta/status 的记录 —— 需要区分「代码没打进去」和
 * 「代码在但请求没发出」。PowerShell 的 [System.IO.File] 用的是进程
 * 当前目录（不受 cd 影响），且 PS 5.1 读 UTF-8 中文会乱码，
 * 因此统一用 Node 按绝对路径 + UTF-8 读取。
 *
 * 用法：node backend/tests/inspect_bundle.mjs [dist/assets 目录]
 */
import { readdirSync, readFileSync } from 'node:fs'
import { join, resolve } from 'node:path'

const dir = resolve(process.argv[2] ?? 'frontend/dist/assets')
const KEYWORDS = [
  '/api/meta/status',
  'meta/status',
  '连接中',
  '数据尚未初始化',
  'rps/sector',
  'rps/heatmap',
  'Promise.race',
  'setInterval',
  'fetch(',
]

let files
try {
  files = readdirSync(dir).filter((f) => f.endsWith('.js')).sort()
} catch (e) {
  console.error(`读取目录失败: ${dir} -> ${e.message}`)
  process.exit(1)
}

if (!files.length) {
  console.error(`目录里没有 .js: ${dir}`)
  process.exit(1)
}

console.log(`目录: ${dir}`)
console.log(`文件: ${files.length} 个\n`)

let anyFetch = false
for (const f of files) {
  const code = readFileSync(join(dir, f), 'utf8')
  const hits = KEYWORDS.filter((k) => code.includes(k))
  if (hits.some((k) => k === 'fetch(')) anyFetch = true
  const flag = hits.length ? '' : '   <-- 无任何关键字'
  console.log(`${f}  (${code.length} 字节)`)
  for (const k of KEYWORDS) {
    if (code.includes(k)) console.log(`    + ${k}`)
  }
  if (flag) console.log(flag)
  console.log('')
}

console.log(anyFetch ? '结论: 产物中含 fetch 调用' : '结论: 产物中【不含】任何 fetch 调用 —— 构建产物或源码有问题')