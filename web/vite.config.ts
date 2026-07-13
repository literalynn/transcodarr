import react from "@vitejs/plugin-react";
import path from "node:path";
import { defineConfig } from "vite";

// Dev : proxy vers le backend FastAPI (docker : le build est servi par l'app)
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "src") },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://localhost:8080", changeOrigin: true },
      "/ws": { target: "ws://localhost:8080", ws: true },
    },
  },
  build: { outDir: "dist", sourcemap: false },
});
