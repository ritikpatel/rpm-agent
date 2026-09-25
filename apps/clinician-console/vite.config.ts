import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server proxies the API so the console runs against the local stack: `npm run dev` -> http://localhost:5173
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://localhost:8000",
      "/ws": { target: "ws://localhost:8000", ws: true },
    },
  },
});
