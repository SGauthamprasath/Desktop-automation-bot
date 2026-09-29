import { resolve } from 'path'
import { defineConfig } from 'electron-vite'
import react from '@vitejs/plugin-react'

// electron-vite builds three separate bundles: main process, preload, renderer.
// Defaults are fine for main/preload (src/main, src/preload); the renderer
// needs the React plugin for JSX + fast refresh, and has two pages: the bot
// itself and the speech bubble.
export default defineConfig({
  main: {},
  preload: {},
  renderer: {
    plugins: [react()],
    build: {
      rollupOptions: {
        input: {
          index: resolve(import.meta.dirname, 'src/renderer/index.html'),
          bubble: resolve(import.meta.dirname, 'src/renderer/bubble.html')
        }
      }
    }
  }
})
