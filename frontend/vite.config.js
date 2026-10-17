import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

const backend = process.env.BACKEND_URL ?? 'http://127.0.0.1:8765'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5288,
    strictPort: true,
    proxy: { '/api': backend },
  },
})
