(function () {
  const XLSX_LOCAL_PATH = "./xlsx.full.min.js";
  const XLSX_CDN_PATH = "https://cdn.sheetjs.com/xlsx-0.20.3/package/dist/xlsx.full.min.js";
  const APP_SCRIPT_PATH = "./model-inventory-studio.js";

  function loadScript(src) {
    return new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = src;
      script.async = true;
      script.onload = () => resolve(src);
      script.onerror = () => reject(new Error(`Failed to load ${src}`));
      document.head.appendChild(script);
    });
  }

  async function ensureXlsx() {
    if (globalThis.XLSX) {
      return { source: "existing" };
    }

    try {
      await loadScript(XLSX_LOCAL_PATH);
      if (globalThis.XLSX) {
        return { source: "local" };
      }
    } catch (error) {
      console.warn("Local XLSX bundle not found, falling back to CDN.", error);
    }

    await loadScript(XLSX_CDN_PATH);
    if (!globalThis.XLSX) {
      throw new Error("XLSX library failed to initialize.");
    }
    return { source: "cdn" };
  }

  function renderBootError(error) {
    console.error(error);
    document.body.innerHTML = `
      <main style="padding: 32px; color: #e5eefc; background: #08111f; font-family: Inter, system-ui, sans-serif;">
        <h1 style="margin-bottom: 12px;">模型分析表工作台启动失败</h1>
        <p style="line-height: 1.7; max-width: 720px;">
          无法加载 Excel 解析依赖。请确认同目录下存在 <code>xlsx.full.min.js</code>，
          或在联网环境下重新打开页面以自动从 CDN 补载。
        </p>
        <pre style="margin-top: 16px; padding: 16px; border-radius: 12px; background: rgba(255,255,255,0.06); white-space: pre-wrap;">${String(error?.message || error)}</pre>
      </main>
    `;
  }

  async function boot() {
    try {
      const result = await ensureXlsx();
      document.documentElement.dataset.xlsxSource = result.source;
      await loadScript(APP_SCRIPT_PATH);
    } catch (error) {
      renderBootError(error);
    }
  }

  boot();
})();
