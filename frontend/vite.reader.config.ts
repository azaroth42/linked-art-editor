/**
 * Build of the Public Reader script (Step 1.21): `corepack pnpm build:reader`.
 *
 * A separate build from the SPA, so the reader page never loads SPA code (decision D8). The
 * entry, `src/reader/main.ts`, is written to `dist-reader/reader.js` under a fixed name, because
 * the server-rendered page links to it (`lae.reader.assets.SCRIPT_SRC`); the Canvas Panel chunk
 * it imports on demand gets a content hash. `lae api` serves the directory at `/reader/assets/`.
 */
import { defineConfig } from "vite";

export default defineConfig({
  base: "/reader/assets/",
  publicDir: false,
  build: {
    outDir: "dist-reader",
    emptyOutDir: true,
    target: "es2022",
    // Canvas Panel is one 594 kB IIFE that cannot be split (Spike S5, section 6).
    chunkSizeWarningLimit: 650,
    rollupOptions: {
      input: "src/reader/main.ts",
      output: {
        entryFileNames: "reader.js",
        chunkFileNames: "[name]-[hash].js",
        assetFileNames: "[name]-[hash][extname]",
      },
    },
  },
});
