---
labels: Platform: Wasm
paths: xbmc/windowing/wasm/* xbmc/platform/wasm/*
tags: wasm, emscripten, webassembly
content: TARGET_WASM|EM_ASM|emscripten_\w+
summary: WebAssembly build: which thread may own and touch browser objects.
---
- convention [23+]: Kodi runs on a pthread in the WebAssembly build. The WebGL context is created on the browser main thread and proxied, since a thread that never returns to its event loop (Kodi's modal dialog loops) must not own per-frame browser resources; document, clipboard and DOM input exist only on the main thread. | check: wasm code never touches document, the canvas or other DOM objects from Kodi threads; that work goes through the main thread proxy (MAIN_THREAD_ASYNC_EM_ASM, kodi_pre.js) | at: `xbmc/windowing/wasm/WinSystemWasmGLESContext.cpp:CreateProxiedGLContext`, `xbmc/platform/wasm/kodi_pre.js`
