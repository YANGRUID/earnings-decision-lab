import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // Falls back to Vite's own default (5173) when PORT isn't set, e.g. a
    // plain `npm run dev` — only used to let an external launcher assign a
    // free port when 5173 is already taken (e.g. by the Docker Compose
    // frontend container's own port mapping).
    port: process.env.PORT ? Number(process.env.PORT) : undefined,
  },
})
