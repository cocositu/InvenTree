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
import zipfile

from django.http import HttpResponse, JsonResponse
from django.urls import path
from django.utils.translation import gettext_lazy as _
from rest_framework import permissions

from common.models import Attachment
from part.models import Part
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

KIND_LABELS = {kind: str(label) for kind, label, _, _ in RESOURCE_KINDS}
KIND_ORDER = [kind for kind, _, _, _ in RESOURCE_KINDS] + ['other']
KIND_LABELS['other'] = str(_('Other'))


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


def serialize_attachment(attachment) -> dict:
    """把附件序列化成前端需要的结构。"""
    is_external = bool(attachment.link) and not attachment.attachment

    # 在线预览 URL：
    # - 上传文件走 /media/...，服务端返回 inline，浏览器原生预览 PDF/图片
    # - 外链直接跳转
    preview_url = attachment.link if is_external else f'/media/{attachment.attachment.name}'

    name = ''
    if attachment.attachment:
        name = str(attachment.attachment.name).split('/')[-1]
    elif attachment.link:
        name = str(attachment.link).rstrip('/').split('/')[-1] or str(attachment.link)

    return {
        'pk': attachment.pk,
        'kind': classify_attachment(attachment),
        'name': name,
        'comment': attachment.comment or '',
        'external': is_external,
        'url': preview_url,
        'is_image': bool(attachment.is_image),
        'thumbnail': f'/media/{attachment.thumbnail.name}'
        if attachment.thumbnail
        else None,
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

    def _attachments(self, part):
        """返回该物料的全部附件。"""
        return list(
            Attachment.objects.filter(
                model_type='part.part', model_id=part.pk
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

        items = [serialize_attachment(a) for a in self._attachments(part)]

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
                        'panel.js:renderPartPanel', check_hash=False
                    ),
                }
            )

        return panels