import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'
import { resolve } from 'node:path'

// 开发模式：/api 代理到本机 18005；构建产物由 18005 直接托管，无需代理
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5175,
    proxy: {
      '/api': { target: 'http://127.0.0.1:18005', changeOrigin: true },
      '/three-d': { target: 'http://127.0.0.1:18005', changeOrigin: true },
    },
  },
  build: {
    rollupOptions: {
      input: {
        main: resolve(import.meta.dirname, 'index.html'),
        threeD: resolve(import.meta.dirname, 'three-d-ui/index.html'),
      },
    },
  },
})
