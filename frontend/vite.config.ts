import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'path'

export default defineConfig({
  plugins: [
    tailwindcss(),
    react(),
  ],
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          const normalized = id.replace(/\\/g, "/");
          if (!normalized.includes("node_modules")) return undefined;
          if (
            normalized.includes("/node_modules/react/") ||
            normalized.includes("/node_modules/react-dom/") ||
            normalized.includes("/node_modules/scheduler/")
          ) {
            return "vendor-react";
          }
          if (normalized.includes("sigma") || normalized.includes("graphology")) {
            return "vendor-graph";
          }
          if (
            normalized.includes("react-markdown") ||
            normalized.includes("remark") ||
            normalized.includes("rehype") ||
            normalized.includes("micromark") ||
            normalized.includes("unified")
          ) {
            return "vendor-markdown";
          }
          if (normalized.includes("/node_modules/lucide-react/")) {
            return "vendor-icons";
          }
          if (
            normalized.includes("/node_modules/@hookform/") ||
            normalized.includes("/node_modules/react-hook-form/") ||
            normalized.includes("/node_modules/zod/")
          ) {
            return "vendor-forms";
          }
          if (normalized.includes("/node_modules/zustand/")) {
            return "vendor-state";
          }
          if (normalized.includes("/node_modules/react-window/")) {
            return "vendor-list";
          }
          if (normalized.includes("/node_modules/chroma-js/")) {
            return "vendor-color";
          }
          return undefined;
        },
      },
    },
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
