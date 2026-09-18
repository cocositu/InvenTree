/**
 * Dashboard card for the independent BOM import plugin.
 *
 * The full application is served as a normal Django page at
 * /plugin/bom-import/.  Embed it in an iframe so it works inside the
 * React dashboard without requiring a frontend route.
 */
export function renderBomImportDashboard(target, data) {
  if (!target) return;

  target.innerHTML = `
    <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:8px">
      <div>
        <strong>BOM 导入 / 匹配</strong>
        <div style="font-size:12px;color:#6b7280">导入 BOM 表，快速匹配库内元器件</div>
      </div>
      <a href="/plugin/bom-import/" target="_blank" rel="noopener"
         style="font-size:12px;color:#2563eb;text-decoration:none;white-space:nowrap">在新标签打开 ↗</a>
    </div>
    <iframe src="/plugin/bom-import/" title="BOM Import"
      style="width:100%;min-height:560px;border:1px solid #e5e7eb;border-radius:8px;background:#fff"></iframe>
  `;
}
