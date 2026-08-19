import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../backend/src/main/resources/static",
    emptyOutDir: true
  },
  server: {
    proxy: {
      "/api": "http://localhost:8080"
    }
  }
});
