/* global process */
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发代理目标可用环境变量覆盖：冒烟/端到端时把后端指到别的端口，
// 不必改这个文件（生产构建用不到代理）。
const apiTarget = process.env.RPSBOARD_API_TARGET || 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: apiTarget,
        changeOrigin: true,
      },
    },
  },
})
