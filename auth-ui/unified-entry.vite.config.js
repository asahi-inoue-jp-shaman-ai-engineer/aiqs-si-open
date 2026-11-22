import { defineConfig } from 'vite';
import { fileURLToPath } from 'node:url';

export default defineConfig({
  publicDir: false,
  build: {
    emptyOutDir: false,
    lib: {
      entry: fileURLToPath(new URL('./src/unified-entry-view.js', import.meta.url)),
      name: 'SiUnifiedEntry', formats: ['iife'], fileName: () => 'si-unified-entry.js',
    },
  },
});
