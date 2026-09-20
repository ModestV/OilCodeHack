import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");
  return {
    plugins: [react()],
    build: {
      target: "es2022",
      rollupOptions: {
        output: {
          manualChunks(id) {
            if (id.includes("node_modules/echarts") || id.includes("node_modules/zrender"))
              return "vendor-charts";
            if (
              id.includes("node_modules/react/") ||
              id.includes("node_modules/react-dom/") ||
              id.includes("node_modules/scheduler/")
            )
              return "vendor-react";
            return undefined;
          },
        },
      },
    },
    server: {
      host: "127.0.0.1",
      port: 5173,
      proxy: { "/api": env.VITE_API_TARGET || "http://127.0.0.1:8000" },
    },
  };
});
