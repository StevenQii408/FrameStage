import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  // Keep this workbench separate from other local apps on 5173. Never drift
  // silently to an origin the backend has not explicitly allowed.
  server: { port: 5174, host: '127.0.0.1', strictPort: true },
  preview: { port: 5174, host: '127.0.0.1', strictPort: true },
})
