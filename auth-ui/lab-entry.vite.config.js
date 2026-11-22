import { defineConfig } from 'vite';
import { fileURLToPath } from 'node:url';

export default defineConfig({
  publicDir: false,
  build: {
    emptyOutDir: false,
    lib: {
      entry: fileURLToPath(new URL('./src/independent-entry-view.js', import.meta.url)),
      name: 'SiSyntheticEntry', formats: ['iife'], fileName: () => 'si-independent-entry.js',
    },
  },
});
