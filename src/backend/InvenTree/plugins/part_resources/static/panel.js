/**
 * 元器件「设计资源」面板
 *
 * 由 PartResourcesPlugin.get_ui_panels() 注入到 Part 详情页。
 * 数据全部来自插件自己的 API：/plugin/part-resources/list/<part_id>/
 *
 * 说明：InvenTree 的附件是以 Content-Disposition: inline 提供的，
 * 因此这里的预览链接只要 target="_blank"，浏览器就会原生预览
 * PDF / 图片，无需额外实现预览逻辑。
 */

const KIND_ICONS = {
  datasheet: 'ti:file-type-pdf:outline',
  footprint: 'ti:vector-triangle:outline',
  symbol: 'ti:circuit-resistor:outline',
  model3d: 'ti:cube:outline',
  other: 'ti:file:outline'
};

function formatSize(bytes) {
  if (!bytes) return '';
  const units = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  let v = bytes;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function resourceRow(item) {
  const label = escapeHtml(item.comment || item.name || '未命名');
  const size = formatSize(item.size);
  const externalBadge = item.external
    ? '<span class="pr-badge">外链</span>'
    : '';
  const thumb =
    item.thumbnail && item.is_image
      ? `<img class="pr-thumb" src="${escapeHtml(item.thumbnail)}" alt="">`
      : '';

  return `
    <div class="pr-row">
      <div class="pr-row-main">
        ${thumb}
        <div class="pr-row-text">
          <a class="pr-link" href="${escapeHtml(item.url)}" target="_blank" rel="noopener noreferrer">
            ${label}
          </a>
          <div class="pr-meta">
            ${escapeHtml(item.name)}${size ? ` · ${size}` : ''}${externalBadge}
          </div>
        </div>
      </div>
      <div class="pr-actions">
        <a class="pr-btn" href="${escapeHtml(item.url)}" target="_blank" rel="noopener noreferrer"
           title="在线预览">预览</a>
        <a class="pr-btn" href="${escapeHtml(item.url)}" download title="下载">下载</a>
      </div>
    </div>
  `;
}

function renderCategories(target, payload) {
  if (!payload.total) {
    target.innerHTML = `
      <div class="pr-empty">
        <p>该物料还没有设计资源。</p>
        <p class="pr-hint">
          在下方「附件」面板上传数据手册 / 封装 / 原理图符号 / 3D 模型，
          并给附件打上对应标签（datasheet、footprint、symbol、3dmodel），
          这里就会自动分类显示。
        </p>
      </div>`;
    return;
  }

  const sections = payload.categories
    .map(
      (cat) => `
      <section class="pr-section">
        <h5 class="pr-section-title">
          <i class="${KIND_ICONS[cat.kind] || KIND_ICONS.other}"></i>
          ${escapeHtml(cat.label)}
          <span class="pr-count">${cat.items.length}</span>
        </h5>
        ${cat.items.map(resourceRow).join('')}
      </section>`
    )
    .join('');

  target.innerHTML = `
    <div class="pr-header">
      <div>
        <strong>共 ${payload.total} 个资源</strong>
        ${payload.part.link ? `<a class="pr-supplier" href="${escapeHtml(payload.part.link)}" target="_blank" rel="noopener noreferrer">供应商页面 ↗</a>` : ''}
      </div>
      <a class="pr-pack" href="${escapeHtml(payload.pack_url)}">打包下载全部 (.zip)</a>
    </div>
    ${sections}
  `;
}

/**
 * 面板入口：InvenTree 调用此函数并把内容渲染进 target 元素
 */
export async function renderPartPanel(target, data) {
  if (!target) return;

  const partId = data?.id ?? data?.instance?.pk;
  if (!partId) {
    target.innerHTML = '<p>无法确定物料 ID</p>';
    return;
  }

  target.innerHTML = '<p class="pr-loading">正在加载设计资源…</p>';

  try {
    const res = await fetch(`/plugin/part-resources/list/${partId}/`, {
      credentials: 'include',
      headers: { Accept: 'application/json' }
    });

    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`);
    }

    renderCategories(target, await res.json());
  } catch (err) {
    target.innerHTML = `<p style="color:var(--mantine-color-red-6)">加载设计资源失败：${escapeHtml(err.message)}</p>`;
  }
}