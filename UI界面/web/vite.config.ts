import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import path from 'path';
import {defineConfig} from 'vite';

export default defineConfig(() => {
  return {
    // 依赖预构建缓存目录：默认 node_modules/.vite。
    // 允许用 VITE_CACHE_DIR 覆盖 —— 目的是**隔离起第二实例**（备用端口 + 临时后端）
    // 做页面/接口证据，而不与生产 dev 实例争同一份缓存目录。默认行为完全不变。
    cacheDir: process.env.VITE_CACHE_DIR || 'node_modules/.vite',
    plugins: [react(), tailwindcss()],
    resolve: {
      alias: {
        '@': path.resolve(__dirname, '.'),
      },
    },
    server: {
      // HMR is disabled in AI Studio via DISABLE_HMR env var.
      // Do not modify—file watching is disabled to prevent flickering during agent edits.
      hmr: process.env.DISABLE_HMR !== 'true',
      // Disable file watching when DISABLE_HMR is true to save CPU during agent edits.
      watch: process.env.DISABLE_HMR === 'true' ? null : {},
      // 接入真实后端（只读）。仅开发期同源转发，不改变任何界面。
      proxy: {
        '/api': {
          target: process.env.API_PROXY_TARGET || 'http://127.0.0.1:8787',
          changeOrigin: true,
        },
        /**
         * 币安**公开无鉴权**聪明钱接口的开发期同源转发（只读 GET）。
         *
         * 为什么需要：浏览器直连 www.binance.bh 会被 CORS 拦；该端点路径含 `/public/`，
         * 不带 cookie / Authorization 即 200（2026-10-07 实测，见
         * docs/验收证据/币安聪明钱真实响应_2026-10-07.json），因此同源转发不涉及任何凭据。
         *
         * 定位：**仅开发期验证路径**。生产构建下 `/bapi/*` 不存在，前端会静默降级为空态；
         * 正式取数由后端任务实现 `GET /api/v1/smart-money/{exchange}/{market_id}` 后走 `/api` 代理。
         * 不新增进程、不新增端口。
         */
        '/bapi': {
          target: process.env.BAPI_PROXY_TARGET || 'https://www.binance.bh',
          changeOrigin: true,
        },
      },
    },
  };
});
