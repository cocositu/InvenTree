/**
 * 元器件「设计资源」面板
 *
 * 数据来自：
 *   /plugin/part-resources/list/<part_id>/
 *   /plugin/part-resources/bom/<part_id>/
 *
 * 可交互能力：
 *   - KiCad 封装：勾选图层查看（F.Cu / F.SilkS / F.Mask ...）
 *   - KiCad 符号：服务端渲染引脚编号 + 引脚名
 *   - STEP / IGES：前端 canvas 网格查看器，可拖拽旋转、滚轮缩放
 */

// InvenTree 不会自动加载插件的 panel.css，这里显式注入（带版本号防缓存）
(function ensurePanelStyles() {
  if (document.getElementById('part-resources-panel-style')) return;
  const link = document.createElement('link');
  link.id = 'part-resources-panel-style';
  link.rel = 'stylesheet';
  link.href = '/static/plugins/part-resources/panel.css?v=4';
  document.head.appendChild(link);
})();

const KIND_ICONS = {
  datasheet: 'ti:file-type-pdf:outline',
  footprint: 'ti:vector-triangle:outline',
  symbol: 'ti:circuit-resistor:outline',
  model3d: 'ti:cube:outline',
  other: 'ti:file:outline'
};

const resourceIndex = new Map();

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

function thumbHtml(item) {
  if (item.thumbnail) {
    return `<img class="pr-thumb" src="${escapeHtml(item.thumbnail)}" alt="" loading="lazy">`;
  }
  return `<span class="pr-thumb pr-thumb-empty"><i class="${KIND_ICONS[item.kind] || KIND_ICONS.other}"></i></span>`;
}

function resourceRow(item) {
  const label = escapeHtml(item.comment || item.name || '未命名');
  const previewUrl = item.preview_url || item.url || item.download_url || '#';
  const downloadUrl = item.download_url || item.url || '#';
  const size = formatSize(item.size);
  const previewAttrs = item.renderable ? ` data-preview-pk="${escapeHtml(item.pk)}"` : '';

  const badges = [];
  if (item.external) badges.push('<span class="pr-badge">外链</span>');
  if (item.renderable) badges.push('<span class="pr-badge pr-badge-render">可交互</span>');
  if (item.layers && item.layers.length) badges.push(`<span class="pr-badge">${item.layers.length} 层</span>`);
  if (item.pin_count) badges.push(`<span class="pr-badge">${item.pin_count} 脚</span>`);

  const download = item.external
    ? ''
    : `<a class="pr-btn" href="${escapeHtml(downloadUrl)}" download title="下载原文件">下载</a>`;

  return `
    <div class="pr-row">
      <div class="pr-row-main">
        ${thumbHtml(item)}
        <div class="pr-row-text">
          <a class="pr-link" href="${escapeHtml(previewUrl)}"${previewAttrs} target="_blank" rel="noopener noreferrer">
            ${label}
          </a>
          <div class="pr-meta">
            ${escapeHtml(item.name)}${size ? ` · ${size}` : ''}${badges.join('')}
          </div>
        </div>
      </div>
      <div class="pr-actions">
        <a class="pr-btn" href="${escapeHtml(previewUrl)}"${previewAttrs} target="_blank" rel="noopener noreferrer"
           title="交互预览">预览</a>
        ${download}
      </div>
    </div>
  `;
}

function renderResourceSections(payload) {
  return payload.categories
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
}

function renderBomSection(bom) {
  if (!bom || !bom.lines || !bom.lines.length) return '';

  const rows = bom.lines
    .map((line) => {
      const part = line.part || {};
      const partUrl = `/part/${encodeURIComponent(part.pk)}/part-resources`;
      const quantity = Number.isFinite(Number(line.quantity)) ? Number(line.quantity) : 1;
      const note = line.note ? ` · ${escapeHtml(line.note)}` : '';

      const resourceLinks = (line.resources || [])
        .map((item) => {
          const label = escapeHtml(item.comment || item.name || '资源');
          const url = item.preview_url || item.url || item.download_url || '#';
          const attrs = item.renderable ? ` data-preview-pk="${escapeHtml(item.pk)}"` : '';
          return `<a class="pr-bom-resource" href="${escapeHtml(url)}"${attrs} target="_blank" rel="noopener noreferrer">${label}</a>`;
        })
        .join('<span class="pr-bom-sep">·</span>');

      const resources = resourceLinks || '<span class="pr-bom-empty">无设计资源</span>';
      const pack = line.resource_count
        ? `<a class="pr-btn pr-btn-pack" href="${escapeHtml(line.pack_url)}" title="打包该子件资源">打包</a>`
        : '';

      return `
        <div class="pr-bom-row">
          <div class="pr-bom-main">
            <a class="pr-link" href="${partUrl}" target="_blank" rel="noopener noreferrer">
              ${escapeHtml(part.name || '未命名子件')}
            </a>
            <div class="pr-meta">
              ${escapeHtml(part.ipn || '')} × ${quantity}${note}
            </div>
            <div class="pr-bom-resources">${resources}</div>
          </div>
          <div class="pr-bom-actions">
            ${pack}
            <span class="pr-bom-count" title="资源数量">${line.resource_count || 0}</span>
          </div>
        </div>`;
    })
    .join('');

  const packAll = bom.total_resources
    ? `<a class="pr-pack pr-pack-bom" href="${escapeHtml(bom.pack_url)}">打包下载 BOM 全部资源 (.zip)</a>`
    : '<span class="pr-bom-hint">BOM 子件暂无设计资源</span>';

  return `
    <section class="pr-section pr-bom-section">
      <h5 class="pr-section-title">
        <i class="ti:sitemap:outline"></i>
        BOM 设计资源
        <span class="pr-count">${bom.line_count || bom.lines.length}</span>
      </h5>
      <div class="pr-bom-header">${packAll}</div>
      ${rows}
    </section>`;
}

function renderHeader(payload) {
  if (!payload.total) return '';
  return `
    <div class="pr-header">
      <div>
        <strong>共 ${payload.total} 个资源</strong>
        ${payload.part.link ? `<a class="pr-supplier" href="${escapeHtml(payload.part.link)}" target="_blank" rel="noopener noreferrer">供应商页面 ↗</a>` : ''}
      </div>
      <a class="pr-pack" href="${escapeHtml(payload.pack_url)}">打包下载全部 (.zip)</a>
    </div>`;
}

function previewUrlFor(item, layers) {
  const base = item.preview_url || item.url || item.download_url || '#';
  if (base === '#' || !base.startsWith('/plugin/')) return base;
  const params = new URLSearchParams();
  params.set('size', '1024');
  if (layers && layers.length) {
    params.set('layers', layers.join(','));
  }
  return `${base}${base.includes('?') ? '&' : '?'}${params.toString()}`;
}

let activeViewer = null;

function closeViewer() {
  if (activeViewer) {
    if (typeof activeViewer.destroy === 'function') activeViewer.destroy();
    activeViewer.remove();
    activeViewer = null;
    document.removeEventListener('keydown', onViewerKeydown);
  }
}

function onViewerKeydown(event) {
  if (event.key === 'Escape') closeViewer();
}

function openResourceViewer(pk) {
  const item = resourceIndex.get(String(pk));
  if (!item) return;

  closeViewer();

  const overlay = document.createElement('div');
  overlay.className = 'pr-viewer-overlay';

  const modal = document.createElement('div');
  modal.className = 'pr-viewer';

  const header = document.createElement('div');
  header.className = 'pr-viewer-header';
  header.innerHTML = `<strong>${escapeHtml(item.comment || item.name || '预览')}</strong>`;

  const close = document.createElement('button');
  close.className = 'pr-viewer-close';
  close.type = 'button';
  close.textContent = '×';
  close.title = '关闭';
  close.addEventListener('click', closeViewer);
  header.appendChild(close);

  const body = document.createElement('div');
  body.className = 'pr-viewer-body';
  modal.appendChild(header);
  modal.appendChild(body);
  overlay.appendChild(modal);
  overlay.addEventListener('click', (event) => {
    if (event.target === overlay) closeViewer();
  });
  document.body.appendChild(overlay);
  document.addEventListener('keydown', onViewerKeydown);
  activeViewer = overlay;

  // 3D 模型：canvas 旋转查看器
  if (item.is_3d) {
    const hint = document.createElement('div');
    hint.className = 'pr-viewer-hint';
    hint.textContent = '按住拖动旋转 · 滚轮缩放 · 双击复位';
    body.appendChild(hint);

    const canvas = document.createElement('canvas');
    canvas.className = 'pr-mesh-canvas';
    body.appendChild(canvas);

    const status = document.createElement('div');
    status.className = 'pr-viewer-status';
    status.textContent = '正在加载 3D 网格…';
    body.appendChild(status);

    fetch(`/plugin/part-resources/mesh/${item.pk}/`, {
      credentials: 'include',
      headers: { Accept: 'application/json' }
    })
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then((mesh) => {
        status.remove();
        if (activeViewer !== overlay) return;
        createMeshViewer(canvas, mesh);
      })
      .catch((err) => {
        status.textContent = `3D 网格加载失败：${err.message}`;
        status.classList.add('pr-viewer-error');
        const fallback = document.createElement('img');
        fallback.className = 'pr-viewer-img';
        fallback.src = previewUrlFor(item, null);
        body.insertBefore(fallback, status);
      });
    return;
  }

  // 2D 可渲染资源（KiCad 封装 / 符号）
  if (item.renderable) {
    const layers = Array.isArray(item.layers) ? item.layers : [];
    const selected = new Set(layers);
    const image = document.createElement('img');
    image.className = 'pr-viewer-img';
    image.alt = item.comment || item.name || '';

    const refresh = () => {
      if (selected.size === 0 || selected.size === layers.length) {
        image.src = previewUrlFor(item, null);
      } else {
        image.src = previewUrlFor(item, Array.from(selected));
      }
    };

    if (layers.length > 1) {
      const toolbar = document.createElement('div');
      toolbar.className = 'pr-layer-toolbar';

      const allButton = document.createElement('button');
      allButton.type = 'button';
      allButton.className = 'pr-layer-btn pr-layer-btn-all active';
      allButton.textContent = '全部';
      allButton.addEventListener('click', () => {
        layers.forEach((layer) => selected.add(layer));
        toolbar.querySelectorAll('.pr-layer-btn').forEach((btn) => btn.classList.add('active'));
        refresh();
      });
      toolbar.appendChild(allButton);

      layers.forEach((layer) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'pr-layer-btn active';
        button.textContent = layer;
        button.addEventListener('click', () => {
          if (selected.has(layer)) {
            if (selected.size <= 1) return;
            selected.delete(layer);
            button.classList.remove('active');
          } else {
            selected.add(layer);
            button.classList.add('active');
          }
          allButton.classList.toggle('active', selected.size === layers.length);
          refresh();
        });
        toolbar.appendChild(button);
      });

      body.appendChild(toolbar);
    }

    if (item.pin_count) {
      const hint = document.createElement('div');
      hint.className = 'pr-viewer-hint';
      hint.textContent = `引脚数量：${item.pin_count}`;
      body.appendChild(hint);
    }

    body.appendChild(image);
    refresh();
    return;
  }

  // 兜底：图片 / PDF / 外链
  const fallback = document.createElement('img');
  fallback.className = 'pr-viewer-img';
  fallback.src = previewUrlFor(item, null);
  body.appendChild(fallback);
}

/**
 * 极简 canvas 3D 网格查看器：画家算法 + 平面着色，满足旋转 / 缩放预览。
 */
function createMeshViewer(canvas, mesh) {
  const vertices = Array.isArray(mesh.vertices) ? mesh.vertices : [];
  const indices = Array.isArray(mesh.indices) ? mesh.indices : [];
  const colors = Array.isArray(mesh.colors) ? mesh.colors : [];
  if (!vertices.length || !indices.length) return;

  const ctx = canvas.getContext('2d');
  const width = Math.max(360, canvas.clientWidth || 720);
  const height = 480;
  canvas.width = width;
  canvas.height = height;
  canvas.style.width = `${width}px`;
  canvas.style.height = `${height}px`;

  let minX = Infinity, minY = Infinity, minZ = Infinity;
  let maxX = -Infinity, maxY = -Infinity, maxZ = -Infinity;
  for (const v of vertices) {
    minX = Math.min(minX, v[0]); maxX = Math.max(maxX, v[0]);
    minY = Math.min(minY, v[1]); maxY = Math.max(maxY, v[1]);
    minZ = Math.min(minZ, v[2]); maxZ = Math.max(maxZ, v[2]);
  }
  const cx = (minX + maxX) / 2;
  const cy = (minY + maxY) / 2;
  const cz = (minZ + maxZ) / 2;
  let radius = 0;
  for (const v of vertices) {
    const d = Math.hypot(v[0] - cx, v[1] - cy, v[2] - cz);
    if (d > radius) radius = d;
  }
  if (radius <= 0) radius = 1;
  radius *= 1.12;

  let rotX = -0.55;
  let rotY = 0.7;
  let zoom = 1;
  let dragging = false;
  let lastX = 0;
  let lastY = 0;
  let scheduled = false;
  const light = normalize3([-0.4, -0.6, 0.8]);

  const rx = new Float64Array(vertices.length);
  const ry = new Float64Array(vertices.length);
  const rz = new Float64Array(vertices.length);
  const px = new Float64Array(vertices.length);
  const py = new Float64Array(vertices.length);

  function normalize3(vector) {
    const length = Math.hypot(vector[0], vector[1], vector[2]) || 1;
    return [vector[0] / length, vector[1] / length, vector[2] / length];
  }

  function clamp(value, min, max) {
    return Math.min(max, Math.max(min, value));
  }

  function scheduleDraw() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => {
      scheduled = false;
      draw();
    });
  }

  function draw() {
    const cosX = Math.cos(rotX);
    const sinX = Math.sin(rotX);
    const cosY = Math.cos(rotY);
    const sinY = Math.sin(rotY);
    const scale = zoom * Math.min(width, height) / (radius * 2.35);

    for (let i = 0; i < vertices.length; i += 1) {
      const v = vertices[i];
      const x = v[0] - cx;
      const y = v[1] - cy;
      const z = v[2] - cz;
      const x1 = x * cosY + z * sinY;
      const z1 = -x * sinY + z * cosY;
      const y2 = y * cosX - z1 * sinX;
      const z2 = y * sinX + z1 * cosX;
      rx[i] = x1;
      ry[i] = y2;
      rz[i] = z2;
      px[i] = width / 2 + x1 * scale;
      py[i] = height / 2 - y2 * scale;
    }

    const order = [];
    for (let t = 0; t < indices.length; t += 3) {
      const a = indices[t];
      const b = indices[t + 1];
      const c = indices[t + 2];
      order.push([(rz[a] + rz[b] + rz[c]) / 3, t]);
    }
    order.sort((left, right) => left[0] - right[0]);

    ctx.fillStyle = '#fafafa';
    ctx.fillRect(0, 0, width, height);

    for (const entry of order) {
      const t = entry[1];
      const a = indices[t];
      const b = indices[t + 1];
      const c = indices[t + 2];

      const ux = rx[b] - rx[a];
      const uy = ry[b] - ry[a];
      const uz = rz[b] - rz[a];
      const vx = rx[c] - rx[a];
      const vy = ry[c] - ry[a];
      const vz = rz[c] - rz[a];
      const nx = uy * vz - uz * vy;
      const ny = uz * vx - ux * vz;
      const nz = ux * vy - uy * vx;
      const normalLength = Math.hypot(nx, ny, nz) || 1;
      const lambert = Math.abs(
        (nx / normalLength) * light[0] +
        (ny / normalLength) * light[1] +
        (nz / normalLength) * light[2]
      );
      const intensity = 0.34 + 0.66 * lambert;
      const base = colors[t / 3] || [0.38, 0.55, 0.72];
      const fill = `rgb(${Math.round(255 * clamp(0.08 + 0.92 * base[0] * intensity, 0, 1))},${Math.round(255 * clamp(0.08 + 0.92 * base[1] * intensity, 0, 1))},${Math.round(255 * clamp(0.08 + 0.92 * base[2] * intensity, 0, 1))})`;

      ctx.beginPath();
      ctx.moveTo(px[a], py[a]);
      ctx.lineTo(px[b], py[b]);
      ctx.lineTo(px[c], py[c]);
      ctx.closePath();
      ctx.fillStyle = fill;
      ctx.fill();
    }
  }

  canvas.style.cursor = 'grab';
  canvas.addEventListener('pointerdown', (event) => {
    dragging = true;
    lastX = event.clientX;
    lastY = event.clientY;
    canvas.style.cursor = 'grabbing';
    canvas.setPointerCapture(event.pointerId);
  });
  canvas.addEventListener('pointermove', (event) => {
    if (!dragging) return;
    rotY += (event.clientX - lastX) * 0.009;
    rotX = clamp(rotX - (event.clientY - lastY) * 0.009, -Math.PI / 2, Math.PI / 2);
    lastX = event.clientX;
    lastY = event.clientY;
    scheduleDraw();
  });
  const endDrag = (event) => {
    if (!dragging) return;
    dragging = false;
    canvas.style.cursor = 'grab';
    try { canvas.releasePointerCapture(event.pointerId); } catch (_err) { /* noop */ }
  };
  canvas.addEventListener('pointerup', endDrag);
  canvas.addEventListener('pointercancel', endDrag);
  canvas.addEventListener('wheel', (event) => {
    event.preventDefault();
    zoom = clamp(zoom * (event.deltaY < 0 ? 1.12 : 0.89), 0.25, 4);
    scheduleDraw();
  }, { passive: false });
  canvas.addEventListener('dblclick', () => {
    rotX = -0.55;
    rotY = 0.7;
    zoom = 1;
    scheduleDraw();
  });

  draw();
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

  closeViewer();
  resourceIndex.clear();
  target.innerHTML = '<p class="pr-loading">正在加载设计资源…</p>';

  try {
    const options = {
      credentials: 'include',
      headers: { Accept: 'application/json' }
    };

    const [listRes, bomRes] = await Promise.all([
      fetch(`/plugin/part-resources/list/${partId}/`, options),
      fetch(`/plugin/part-resources/bom/${partId}/`, options).catch(() => null)
    ]);

    if (!listRes.ok) {
      throw new Error(`HTTP ${listRes.status}`);
    }

    const payload = await listRes.json();
    for (const category of payload.categories || []) {
      for (const item of category.items || []) {
        resourceIndex.set(String(item.pk), item);
      }
    }

    let bomPayload = null;
    if (bomRes && bomRes.ok) {
      try {
        bomPayload = await bomRes.json();
      } catch (_err) {
        bomPayload = null;
      }
      for (const line of bomPayload?.lines || []) {
        for (const item of line.resources || []) {
          if (!resourceIndex.has(String(item.pk))) {
            resourceIndex.set(String(item.pk), item);
          }
        }
      }
    }

    target.onclick = (event) => {
      const trigger = event.target.closest('[data-preview-pk]');
      if (!trigger || !target.contains(trigger)) return;
      const pk = trigger.getAttribute('data-preview-pk');
      if (!pk) return;
      event.preventDefault();
      openResourceViewer(pk);
    };

    const bomHtml = renderBomSection(bomPayload);

    if (!payload.total && !bomHtml) {
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

    target.innerHTML = `${renderHeader(payload)}${renderResourceSections(payload)}${bomHtml}`;
  } catch (err) {
    target.innerHTML = `<p style="color:var(--mantine-color-red-6)">加载设计资源失败：${escapeHtml(err.message)}</p>`;
  }
}
