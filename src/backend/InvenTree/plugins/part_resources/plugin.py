"""元器件设计资源插件。

把物料（Part）上挂载的附件按「设计资源」的语义组织起来，提供：

1. 类型识别 —— 数据手册 / 封装 / 原理图符号 / 3D 模型 / 其他
2. 一键打包下载 —— 该物料的全部资源打成 zip
3. Part 详情页面板 —— 按类型分组展示，支持在线预览

设计说明
--------
InvenTree 原生已经具备以下能力，本插件**不重复实现**：

- 附件存储（文件或外链），模型 ``common.models.Attachment``
- 在线预览：附件以 ``Content-Disposition: inline`` 提供，前端用
  ``<a target="_blank">`` 打开，浏览器原生预览 PDF / 图片

因此插件只补两件事：**分类** 与 **打包下载**。

类型识别优先级（从强到弱）：
  1. 附件的标签（tags）—— 最可靠，推荐用这种方式
  2. 文件名扩展名 —— .kicad_mod / .kicad_sym / .step 等
  3. 注释关键字 —— 兜底
"""

import io
import logging
import zipfile

from django.http import HttpResponse, JsonResponse
from django.urls import path
import logging

logger = logging.getLogger('inventree.plugins.part_resources')

from django.utils.translation import gettext_lazy as _
from rest_framework import permissions

from common.models import Attachment

from . import kicad_cli
from . import render as renderer
from part.models import BomItem, Part
from plugin import InvenTreePlugin
from plugin.mixins import SettingsMixin, UrlsMixin, UserInterfaceMixin


# 资源类型定义：kind -> (显示名, 标签别名, 扩展名)
RESOURCE_KINDS = [
    (
        'datasheet',
        _('Datasheet'),
        {'datasheet', 'ds', '数据手册', '手册', '规格书'},
        {'.pdf', '.html', '.htm'},
    ),
    (
        'footprint',
        _('Footprint'),
        {'footprint', 'fplib', '封装', 'pcb', 'kicad_mod'},
        {'.kicad_mod', '.pretty', '.mod'},
    ),
    (
        'symbol',
        _('Schematic Symbol'),
        {'symbol', 'schematic', '原理图', '原理图符号', 'kicad_sym'},
        {'.kicad_sym', '.lib', '.sch'},
    ),
    (
        'model3d',
        _('3D Model'),
        {'3d', '3dmodel', 'step', 'stp', '模型'},
        {'.step', '.stp', '.igs', '.iges', '.wrl', '.3mf'},
    ),
]

# 可由插件渲染成图片的扩展名（缩略图缓存 / 按需高清预览）
RENDERABLE_2D_SUFFIXES = ('.kicad_mod', '.kicad_sym')
RENDERABLE_3D_SUFFIXES = ('.step', '.stp', '.iges', '.igs')
RENDERABLE_SUFFIXES = RENDERABLE_2D_SUFFIXES + RENDERABLE_3D_SUFFIXES

KIND_LABELS = {kind: str(label) for kind, label, _, _ in RESOURCE_KINDS}
KIND_ORDER = [kind for kind, _, _, _ in RESOURCE_KINDS] + ['other']
KIND_LABELS['other'] = str(_('Other'))

# 进程内缓存 kicad-cli 生成的 SVG（超时/文件变化后自动失效）
_KICAD_SVG_CACHE = {}


def classify_attachment(attachment) -> str:
    """判断一条附件属于哪一类设计资源。

    优先级：标签 > 扩展名 > 注释关键字 > 其他。

    Args:
        attachment: common.models.Attachment 实例

    Returns:
        str: 资源类型标识（datasheet / footprint / symbol / model3d / other）
    """
    # 1) 标签（最可靠）
    try:
        tags = {t.name.strip().lower() for t in attachment.tags.all()}
    except Exception:  # pragma: no cover - 标签表不可用时退化为无标签
        tags = set()

    for kind, _label, aliases, _exts in RESOURCE_KINDS:
        if tags & {a.lower() for a in aliases}:
            return kind

    # 2) 文件扩展名
    name = ''
    if attachment.attachment:
        name = str(attachment.attachment.name).lower()

    for kind, _label, _aliases, exts in RESOURCE_KINDS:
        if any(name.endswith(ext) for ext in exts):
            return kind

    # 3) 注释关键字
    comment = (attachment.comment or '').lower()
    for kind, _label, aliases, _exts in RESOURCE_KINDS:
        if any(a.lower() in comment for a in aliases):
            return kind

    return 'other'


def serialize_attachment(plugin, attachment) -> dict:
    """把附件序列化成前端需要的结构。

    Args:
        plugin: PartResourcesPlugin 实例（用于生成缩略图、拼接插件 URL）
        attachment: common.models.Attachment 实例
    """
    is_external = bool(attachment.link) and not attachment.attachment

    name = ''
    if attachment.attachment:
        name = str(attachment.attachment.name).split('/')[-1]
    elif attachment.link:
        name = str(attachment.link).rstrip('/').split('/')[-1] or str(attachment.link)

    lower = name.lower()
    is_3d = lower.endswith(RENDERABLE_3D_SUFFIXES)
    is_2d = lower.endswith(RENDERABLE_2D_SUFFIXES)

    # STEP / IGES 需要可选的 cascadio 依赖；未安装时仍允许下载，但不伪装成可预览
    renderable = (
        (not is_external)
        and (is_2d or (is_3d and plugin._can_render_3d()))
    )

    # 预览 URL 分三种情况：
    #   1. KiCad 封装 / 符号、STEP / IGES -> 插件实时渲染成 PNG
    #   2. 外链              -> 直接跳转
    #   3. 其它上传文件       -> /media/...，服务端以 inline 返回，
    #                          浏览器原生预览 PDF / 图片
    if renderable:
        preview_url = f'/plugin/{plugin.SLUG}/preview/{attachment.pk}/'
    elif is_external:
        preview_url = attachment.link
    else:
        preview_url = f'/media/{attachment.attachment.name}'

    # 原文件下载地址和预览地址分开：KiCad/STEP 的预览是渲染图，下载必须是原文件
    download_url = attachment.link if is_external else f'/media/{attachment.attachment.name}'

    # 缩略图：InvenTree 只为图片生成缩略图；
    # KiCad / STEP 文件由插件渲染一张并缓存进 thumbnail 字段（仅首次生成）
    thumbnail = None
    if attachment.thumbnail:
        thumbnail = f'/media/{attachment.thumbnail.name}'
    elif renderable:
        thumbnail = plugin._ensure_thumbnail(attachment)

    # 缓存版本：换成官方 kicad-cli / cascadio 后，旧浏览器缓存必须失效
    if lower.endswith(('.kicad_mod', '.kicad_sym', '.mod', '.pretty')):
        render_version = f'{attachment.file_size or 0}-kicad-cli-v2'
    elif is_3d:
        render_version = f'{attachment.file_size or 0}-cascadio-v2'
    else:
        render_version = str(attachment.file_size or 0)

    # 可交互元数据：KiCad 封装图层、引脚数量等（首次解析后缓存进 metadata）
    resource_meta = {}
    if renderable and not is_external:
        resource_meta = plugin._describe_attachment(attachment, name)

    return {
        'pk': attachment.pk,
        'kind': classify_attachment(attachment),
        'name': name,
        'comment': attachment.comment or '',
        'external': is_external,
        'renderable': renderable,
        'is_3d': is_3d,
        'preview_url': preview_url,
        'download_url': download_url,
        # 兼容旧版前端的字段名
        'url': preview_url,
        'is_image': bool(attachment.is_image),
        'thumbnail': thumbnail,
        'layers': resource_meta.get('layers', []),
        'pin_count': resource_meta.get('pin_count', 0),
        'render_version': render_version,
        'size': attachment.file_size or 0,
        'upload_date': attachment.upload_date.isoformat()
        if attachment.upload_date
        else None,
    }


class PartResourcesPlugin(SettingsMixin, UrlsMixin, UserInterfaceMixin, InvenTreePlugin):
    """元器件设计资源管理。"""

    NAME = 'Part Resources'
    SLUG = 'part-resources'
    TITLE = _('Part Design Resources')
    DESCRIPTION = _(
        'Organise part attachments into design resources (datasheet, footprint, '
        'schematic symbol, 3D model) and download them as a single zip archive'
    )
    VERSION = '1.0.0'
    AUTHOR = 'cocositu'

    SETTINGS = {
        'ENABLE_PART_PANEL': {
            'name': _('Enable Part Panel'),
            'description': _('Show the design resources panel on Part detail pages'),
            'default': True,
            'validator': bool,
        },
        'ENABLE_3D_PREVIEW': {
            'name': _('Enable 3D Preview'),
            'description': _(
                'Render STEP / IGES models to cached thumbnails and on-demand previews '
                '(requires the optional cascadio package)'
            ),
            'default': True,
            'validator': bool,
        },
        'USE_KICAD_CLI': {
            'name': _('Use KiCad CLI'),
            'description': _(
                'Use the official kicad-cli to render .kicad_mod / .kicad_sym previews. '
                'Falls back to the built-in renderer when unavailable.'
            ),
            'default': True,
            'validator': bool,
        },
        'KICAD_CLI_PATH': {
            'name': _('KiCad CLI Path'),
            'description': _(
                'Optional path or command prefix for kicad-cli. '
                'Linux/Debian: /usr/bin/kicad-cli. '
                'Leave empty for automatic detection.'
            ),
            'default': '',
            'validator': str,
        },
    }

    # ------------------------------------------------------------------
    # 帮助方法
    # ------------------------------------------------------------------
    def _get_part(self, request, part_id):
        """取物料并做权限检查，失败返回 (None, 错误响应)。"""
        try:
            part = Part.objects.get(pk=part_id)
        except Part.DoesNotExist:
            return None, JsonResponse({'error': 'Part not found'}, status=404)

        # check_related_permission 是 InvenTreeAttachmentMixin 提供的**类方法**，
        # 不是实例方法（Attachment.check_permission 内部也是委托给它）。
        if not Part.check_related_permission('view', request.user):
            return None, JsonResponse({'error': 'Permission denied'}, status=403)

        return part, None

    def _can_render_3d(self) -> bool:
        """是否启用并具备 3D CAD（STEP / IGES）渲染能力。"""
        if not self.get_setting('ENABLE_3D_PREVIEW'):
            return False
        return renderer.step_renderer_available()

    def _kicad_cli_prefix(self):
        """返回 kicad-cli 命令前缀，未检测到返回 None。"""
        if not self.get_setting('USE_KICAD_CLI'):
            return None
        try:
            return kicad_cli.detect(self.get_setting('KICAD_CLI_PATH') or '')
        except Exception:
            return None

    def _describe_attachment(self, attachment, name):
        """解析附件的可交互元数据，并缓存到 Attachment.metadata。"""
        metadata = attachment.metadata or {}
        cached = metadata.get('part_resources')
        if isinstance(cached, dict) and cached:
            return cached

        _name, data = self._render_source(attachment)
        if not data:
            return {}

        try:
            info = renderer.describe_attachment(name, data)
        except Exception:
            return {}

        if not info:
            return {}

        try:
            metadata = dict(metadata)
            metadata['part_resources'] = info
            Attachment.objects.filter(pk=attachment.pk).update(metadata=metadata)
            attachment.metadata = metadata
        except Exception:
            pass

        return info

    def _attachments(self, part):
        """返回该物料的全部附件。"""
        return list(
            Attachment.objects.filter(
                model_type='part', model_id=part.pk
            ).order_by('pk')
        )

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------
    def view_resource_list(self, request, part_id, *args, **kwargs):
        """GET /plugin/part-resources/list/<part_id>/

        返回按类型分组的资源清单，供前端面板渲染。
        """
        part, error = self._get_part(request, part_id)
        if error:
            return error

        items = [serialize_attachment(self, a) for a in self._attachments(part)]

        categories = []
        for kind in KIND_ORDER:
            group = [i for i in items if i['kind'] == kind]
            if group:
                categories.append(
                    {'kind': kind, 'label': KIND_LABELS.get(kind, kind), 'items': group}
                )

        return JsonResponse(
            {
                'part': {
                    'pk': part.pk,
                    'name': part.name,
                    'ipn': part.IPN or '',
                    'link': part.link or '',
                },
                'categories': categories,
                'total': len(items),
                'pack_url': f'/plugin/{self.SLUG}/pack/{part.pk}/',
            }
        )

    def view_resource_pack(self, request, part_id, *args, **kwargs):
        """GET /plugin/part-resources/pack/<part_id>/

        把该物料的全部资源打包成 zip 下载。

        - 上传的文件直接放入 zip
        - 外链写进一个 ``_links.txt``，避免丢失信息
        """
        part, error = self._get_part(request, part_id)
        if error:
            return error

        attachments = self._attachments(part)
        if not attachments:
            return JsonResponse({'error': 'No resources found'}, status=404)

        buffer = io.BytesIO()
        used_names = {}
        external_links = []

        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            # 一份清单，说明包内结构与来源
            manifest = [
                f'Part: {part.name}',
                f'IPN: {part.IPN or "-"}',
                '',
            ]

            for att in attachments:
                kind = classify_attachment(att)
                folder = KIND_LABELS.get(kind, 'other')

                if att.attachment:
                    src = att.attachment
                    base = str(src.name).split('/')[-1]
                    # 同一目录下重名时加序号，避免覆盖
                    key = f'{folder}/{base}'
                    if key in used_names:
                        used_names[key] += 1
                        stem, dot, ext = base.rpartition('.')
                        base = f'{stem}_{used_names[key]}{dot}{ext}'
                        key = f'{folder}/{base}'
                    else:
                        used_names[key] = 0

                    try:
                        with src.open('rb') as fh:
                            zf.writestr(key, fh.read())
                        manifest.append(f'[{folder}] {base}')
                    except Exception as exc:  # pragma: no cover
                        manifest.append(f'[{folder}] {base}  (读取失败: {exc})')
                elif att.link:
                    external_links.append(f'[{folder}] {att.comment or ""} {att.link}')
                    manifest.append(f'[{folder}] {att.link}  (外部链接)')

            if external_links:
                zf.writestr('_links.txt', '\n'.join(external_links))

            zf.writestr('_manifest.txt', '\n'.join(manifest))

        buffer.seek(0)

        safe_name = ''.join(
            c if c.isalnum() or c in '-_.' else '_' for c in (part.IPN or part.name)
        )

        response = HttpResponse(buffer.read(), content_type='application/zip')
        response['Content-Disposition'] = (
            f'attachment; filename="{safe_name}-resources.zip"'
        )
        return response

    # ------------------------------------------------------------------
    # 预览渲染（缩略图缓存 + 按需高清）
    # ------------------------------------------------------------------
    THUMBNAIL_SIZE = 256
    PREVIEW_SIZE = 1024

    def _render_source(self, attachment):
        """读取附件内容，返回 (文件名, 字节) ，失败返回 (None, None)。"""
        if not attachment.attachment:
            return None, None
        try:
            with attachment.attachment.open('rb') as fh:
                data = fh.read()
        except Exception:
            return None, None
        return str(attachment.attachment.name), data

    def _ensure_thumbnail(self, attachment, force=False):
        """为可渲染的附件生成缩略图并缓存到 Attachment.thumbnail。

        缩略图存进 InvenTree 原生的 ``thumbnail`` 字段，因此：
          - 列表页直接复用静态文件，不会每次请求都重新渲染
          - 与 InvenTree 自己的图片缩略图机制一致

        Returns:
            str | None: 缩略图的媒体路径
        """
        if attachment.thumbnail and not force:
            return f'/media/{attachment.thumbnail.name}'

        name, data = self._render_source(attachment)
        if not data:
            return None

        lower_name = (name or '').lower()

        # 官方 kicad-cli 优先：SVG -> rsvg-convert/cairosvg -> PNG 缩略图
        img = None
        if lower_name.endswith(('.kicad_mod', '.kicad_sym', '.mod', '.pretty')):
            try:
                img = kicad_cli.render_thumbnail(
                    name,
                    data,
                    size=self.THUMBNAIL_SIZE,
                    preferred=self.get_setting('KICAD_CLI_PATH') or '',
                )
            except Exception as exc:
                logger.warning('kicad-cli thumbnail failed for %s: %s', name, exc)
                img = None

        # 回退：内置 Pillow 渲染器（KiCad / STEP 等）
        if img is None:
            img = renderer.render_attachment_preview(name, data, self.THUMBNAIL_SIZE)

        if not img:
            return None

        buf = io.BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)

        from django.core.files.base import ContentFile

        stem = name.split('/')[-1].rsplit('.', 1)[0]
        thumb_name = f'{attachment.pk}-{stem}-thumb.png'
        # rebuild=False avoids InvenTree deleting custom non-image thumbnails
        attachment.thumbnail.save(thumb_name, ContentFile(buf.read()), save=False)
        attachment.save(update_fields=['thumbnail'], rebuild=False)
        return f'/media/{attachment.thumbnail.name}'

    def view_attachment_preview(self, request, attachment_id, *args, **kwargs):
        """GET /plugin/part-resources/preview/<attachment_id>/

        按需实时渲染高清预览图（默认 1024px）。

        列表页用缓存的缩略图，点开某项时才走这个端点 —— 避免列表渲染开销。
        """
        try:
            attachment = Attachment.objects.get(pk=attachment_id)
        except Attachment.DoesNotExist:
            return JsonResponse({'error': 'Attachment not found'}, status=404)

        if not attachment.check_permission('view', request.user):
            return JsonResponse({'error': 'Permission denied'}, status=403)

        try:
            size = int(request.GET.get('size', self.PREVIEW_SIZE))
        except (TypeError, ValueError):
            size = self.PREVIEW_SIZE
        size = max(128, min(size, 2048))

        layers_raw = request.GET.get('layers') or request.GET.get('layer')
        layers = None
        if layers_raw:
            layers = [value.strip() for value in layers_raw.split(',') if value.strip()]

        name, data = self._render_source(attachment)
        if not data:
            return JsonResponse({'error': 'Not a renderable attachment'}, status=400)

        # 优先使用官方 kicad-cli 渲染 KiCad 封装 / 符号
        lower_name = (name or '').lower()
        cli_prefix = self._kicad_cli_prefix()
        if cli_prefix and lower_name.endswith(('.kicad_mod', '.kicad_sym', '.mod', '.pretty')):
            cache_key = (attachment.pk, tuple(layers or []), attachment.file_size or 0)
            svg = _KICAD_SVG_CACHE.get(cache_key)

            if svg is None:
                try:
                    svg = kicad_cli.render_preview(
                        name,
                        data,
                        layers=layers,
                        timeout=90,
                        preferred=self.get_setting('KICAD_CLI_PATH') or '',
                    )
                except Exception as exc:
                    logger.warning('kicad-cli preview failed for %s: %s', name, exc)
                    svg = None
                else:
                    if len(_KICAD_SVG_CACHE) > 128:
                        _KICAD_SVG_CACHE.clear()
                    _KICAD_SVG_CACHE[cache_key] = svg

            if svg:
                response = HttpResponse(svg, content_type='image/svg+xml')
                response['Cache-Control'] = 'public, max-age=86400'
                return response

        img = renderer.render_attachment_preview(name, data, size, layers=layers)
        if not img:
            return JsonResponse({'error': 'No renderer for this file type'}, status=415)

        buf = io.BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)

        response = HttpResponse(buf.read(), content_type='image/png')
        response['Cache-Control'] = 'public, max-age=86400'
        return response

    def view_attachment_mesh(self, request, attachment_id, *args, **kwargs):
        """GET /plugin/part-resources/mesh/<attachment_id>/

        返回轻量 JSON 网格（顶点 / 索引 / 颜色），供前端 canvas 3D 查看器旋转。
        """
        try:
            attachment = Attachment.objects.get(pk=attachment_id)
        except Attachment.DoesNotExist:
            return JsonResponse({'error': 'Attachment not found'}, status=404)

        if not attachment.check_permission('view', request.user):
            return JsonResponse({'error': 'Permission denied'}, status=403)

        name, data = self._render_source(attachment)
        lower = (name or '').lower()
        if not data or not lower.endswith(('.step', '.stp', '.iges', '.igs')):
            return JsonResponse({'error': 'Not a 3D CAD attachment'}, status=415)

        file_type = 'iges' if lower.endswith(('.iges', '.igs')) else 'step'
        mesh = renderer.mesh_from_brep(data, file_type=file_type)
        if not mesh:
            return JsonResponse({'error': 'Unable to tessellate STEP / IGES'}, status=415)

        return JsonResponse(mesh)

    def view_backend_status(self, request, *args, **kwargs):
        """GET /plugin/part-resources/backend/

        Return the detected kicad-cli path/version. Useful on Debian:

            curl -H "Authorization: Token <token>" \
                 http://127.0.0.1:8000/plugin/part-resources/backend/
        """
        prefix = self._kicad_cli_prefix()
        if not prefix:
            return JsonResponse({
                'available': False,
                'prefix': [],
                'configured': self.get_setting('KICAD_CLI_PATH') or '',
                'version': '',
            })

        return JsonResponse({
            'available': True,
            'prefix': prefix,
            'configured': self.get_setting('KICAD_CLI_PATH') or '',
            'version': kicad_cli.version(prefix),
        })

    # ------------------------------------------------------------------
    # BOM 联动
    # ------------------------------------------------------------------
    def _bom_lines(self, part):
        """返回该装配件的 BOM 行。

        Returns:
            list[dict]: 每行包含子件、用量，以及该子件的设计资源
        """
        lines = []

        # get_bom_items() 会包含 InvenTree 原生的 inherited / virtual BOM 行，
        # 比直接 BomItem.objects.filter(part=part) 更完整。
        bom_items = (
            part.get_bom_items(include_inherited=True, include_virtual=True)
            .select_related('sub_part')
            .prefetch_related('sub_part__parameters_list__template')
            .order_by('sub_part__name')
        )

        for item in bom_items:
            sub = item.sub_part
            if sub is None:
                continue

            resources = [serialize_attachment(self, a) for a in self._attachments(sub)]

            # 把子件的参数（封装/容差/耐压等）一起带到 BOM 行
            parameters = []
            try:
                for parameter in sub.parameters_list.all():
                    template = getattr(parameter, 'template', None)
                    if template is None:
                        continue
                    parameters.append(
                        {
                            'name': template.name,
                            'value': parameter.data,
                            'units': template.units or '',
                        }
                    )
            except Exception:
                parameters = []

            lines.append(
                {
                    'bom_pk': item.pk,
                    'quantity': float(item.quantity or 0),
                    'note': item.note or '',
                    'inherited': bool(getattr(item, 'inherited', False)),
                    'optional': bool(getattr(item, 'optional', False)),
                    'consumable': bool(getattr(item, 'consumable', False)),
                    'reference': getattr(item, 'reference', '') or '',
                    'part': {
                        'pk': sub.pk,
                        'name': sub.name,
                        'ipn': sub.IPN or '',
                        'description': sub.description or '',
                        'link': sub.link or '',
                        'assembly': bool(getattr(sub, 'assembly', False)),
                        'parameters': parameters,
                    },
                    'resources': resources,
                    'resource_count': len(resources),
                    'pack_url': f'/plugin/{self.SLUG}/pack/{sub.pk}/',
                }
            )

        return lines

    def view_bom_resources(self, request, part_id, *args, **kwargs):
        """GET /plugin/part-resources/bom/<part_id>/

        列出该装配件 BOM 中每个子件的设计资源，并把子件的参数一并带出，
        便于对照选型（封装、容差、耐压等）。
        """
        part, error = self._get_part(request, part_id)
        if error:
            return error

        lines = self._bom_lines(part)

        return JsonResponse(
            {
                'part': {
                    'pk': part.pk,
                    'name': part.name,
                    'ipn': part.IPN or '',
                    'link': part.link or '',
                },
                'lines': lines,
                'line_count': len(lines),
                'total_resources': sum(l['resource_count'] for l in lines),
                'pack_url': f'/plugin/{self.SLUG}/pack-bom/{part.pk}/',
            }
        )

    def _add_attachment_to_zip(self, zf, att, folder, used_names):
        """把一条附件写进 zip，处理重名。"""
        if not att.attachment:
            return None

        src = att.attachment
        base = str(src.name).split('/')[-1]
        key = f'{folder}/{base}'

        if key in used_names:
            used_names[key] += 1
            stem, dot, ext = base.rpartition('.')
            base = f'{stem}_{used_names[key]}{dot}{ext}'
            key = f'{folder}/{base}'
        else:
            used_names[key] = 0

        with src.open('rb') as fh:
            zf.writestr(key, fh.read())

        return base

    def view_bom_pack(self, request, part_id, *args, **kwargs):
        """GET /plugin/part-resources/pack-bom/<part_id>/

        把整个 BOM 涉及的**全部子件设计资源**打包下载。

        结构：
            <子件IPN或名称>/Datasheet/xxx.pdf
            <子件IPN或名称>/Footprint/xxx.kicad_mod
            _bom.txt        清单（含用量、参数、外链）
        """
        part, error = self._get_part(request, part_id)
        if error:
            return error

        lines = self._bom_lines(part)
        if not lines:
            return JsonResponse({'error': 'No BOM items found'}, status=404)

        buffer = io.BytesIO()
        bom_report = [
            f'BOM for: {part.name}   (IPN: {part.IPN or "-"})',
            '=' * 62,
            '',
        ]
        total = 0

        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            for line in lines:
                sub = line['part']
                folder = ''.join(
                    c if c.isalnum() or c in '-_.' else '_'
                    for c in (sub['ipn'] or sub['name'])
                )

                bom_report.append(
                    f'[{folder}]  {sub["name"]}  x{line["quantity"]:g}   {line["note"]}'
                )
                if sub['description']:
                    bom_report.append(f'          {sub["description"]}')
                parameters = sub.get('parameters') or []
                if parameters:
                    text = ', '.join(
                        f'{p["name"]}={p["value"]}{(" " + p["units"]) if p.get("units") else ""}'
                        for p in parameters
                    )
                    bom_report.append(f'          params: {text}')
                if sub['link']:
                    bom_report.append(f'          link: {sub["link"]}')

                if not line['resources']:
                    bom_report.append('          (无设计资源)')
                    bom_report.append('')
                    continue

                used_names = {}
                for att in self._attachments(Part.objects.get(pk=sub['pk'])):
                    kind = classify_attachment(att)
                    kind_folder = KIND_LABELS.get(kind, 'other')

                    if att.attachment:
                        base = self._add_attachment_to_zip(
                            zf, att, f'{folder}/{kind_folder}', used_names
                        )
                        if base:
                            total += 1
                            bom_report.append(f'          [{kind_folder}] {base}')
                    elif att.link:
                        bom_report.append(
                            f'          [{kind_folder}] (外链) {att.link}'
                        )

                bom_report.append('')

            zf.writestr('_bom.txt', '\n'.join(bom_report))

        buffer.seek(0)

        safe = ''.join(
            c if c.isalnum() or c in '-_.' else '_' for c in (part.IPN or part.name)
        )
        response = HttpResponse(buffer.read(), content_type='application/zip')
        response['Content-Disposition'] = f'attachment; filename="{safe}-bom-resources.zip"'
        return response

    # ------------------------------------------------------------------
    # URL 注册
    # ------------------------------------------------------------------
    def setup_urls(self):
        """注册插件 API 路由。

        注意：必须在这里返回**绑定到实例**的方法，不能用类属性 URLS。

        ``UrlsMixin`` 的默认实现是 ``getattr(self, 'URLS', None)``；如果在类体里
        直接写 ``URLS = [path(..., view_resource_list, ...)]``，拿到的会是尚未绑定
        self 的普通函数，Django 调用时就会抛
        ``TypeError: ... missing 1 required positional argument: 'request'``。
        """
        return [
            path(
                'list/<int:part_id>/',
                self.view_resource_list,
                name='part-resources-list',
            ),
            path(
                'pack/<int:part_id>/',
                self.view_resource_pack,
                name='part-resources-pack',
            ),
            path(
                'preview/<int:attachment_id>/',
                self.view_attachment_preview,
                name='part-resources-preview',
            ),
            path(
                'mesh/<int:attachment_id>/',
                self.view_attachment_mesh,
                name='part-resources-mesh',
            ),
            path(
                'backend/',
                self.view_backend_status,
                name='part-resources-backend',
            ),
            path(
                'bom/<int:part_id>/',
                self.view_bom_resources,
                name='part-resources-bom',
            ),
            path(
                'pack-bom/<int:part_id>/',
                self.view_bom_pack,
                name='part-resources-pack-bom',
            ),
        ]

    # ------------------------------------------------------------------
    # UI 面板
    # ------------------------------------------------------------------
    def get_ui_panels(self, request, context, **kwargs):
        """在 Part 详情页注入「设计资源」面板。"""
        panels = []
        context = context or {}

        if not self.get_setting('ENABLE_PART_PANEL'):
            return panels

        if context.get('target_model') == 'part':
            panels.append(
                {
                    'key': 'part-resources',
                    'title': str(_('Design Resources')),
                    'icon': 'ti:files:outline',
                    'source': self.plugin_static_file(
                        'panel-f54cd5d491.js:renderPartPanel', check_hash=False
                    ),
                }
            )

        return panels
