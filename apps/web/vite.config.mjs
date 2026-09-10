import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const backend = "http://127.0.0.1:8000";
const apiPrefixes = [
  "/health", "/runtime", "/budget", "/projects", "/ip-profile", "/assets", "/imports", "/uploads", "/audio-uploads", "/inbox", "/audio-imports", "/image-imports",
  "/opportunities", "/account-connections", "/historical-content", "/voice-profiles",
  "/talking-profiles", "/image-assets", "/audio-assets", "/analysis-results", "/clips",
];

export default defineConfig({
  base: "/app/",
  plugins: [react()],
  server: {
    port: 5173,
    proxy: Object.fromEntries(apiPrefixes.map((prefix) => [prefix, backend])),
  },
});
