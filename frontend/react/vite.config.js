import { defineConfig } from 'vite'

// 所有靜態資源放在 /react/，由同源入口提供且不混入業務 API 路徑。
export default defineConfig({ base: '/react/', esbuild: { jsx: 'automatic' } })
