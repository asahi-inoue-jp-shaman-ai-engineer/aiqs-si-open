import { defineConfig } from 'vite';
import { fileURLToPath } from 'node:url';

export default defineConfig({
  publicDir: false,
  build: {
    emptyOutDir: false,
    lib: {
      entry: fileURLToPath(new URL('./src/existing-entry-view.js', import.meta.url)),
      name: 'SiExistingEntry', formats: ['iife'], fileName: () => 'si-existing-entry.js',
    },
  },
});
