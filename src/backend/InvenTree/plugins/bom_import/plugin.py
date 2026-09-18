"""Independent BOM import / matching plugin.

Goal:
    Upload a BOM file (CSV / XLSX), auto-detect common columns, then match
    every line against InvenTree parts. Output for each line:

        designators, quantity, value/comment, footprint,
        matched Part (IPN / name), stock, shortage, confidence

This is deliberately separate from the ``part_resources`` plugin.
"""

from __future__ import annotations

import csv
import io
import re
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.db import transaction
from django.db.models import Q, Sum
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.urls import path
from django.utils.translation import gettext_lazy as _
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods

from part.models import Part
from stock.models import StockItem
from plugin import InvenTreePlugin
from plugin.mixins import SettingsMixin, UrlsMixin, UserInterfaceMixin

try:
    from rapidfuzz import fuzz as _fuzz
except Exception:  # pragma: no cover
    _fuzz = None


def _norm(value) -> str:
    return re.sub(r'[^0-9a-z]+', '', str(value or '').lower())


def _ratio(left, right) -> float:
    left = _norm(left)
    right = _norm(right)
    if not left or not right:
        return 0.0
    if _fuzz:
        return float(_fuzz.WRatio(left, right)) / 100.0
    from difflib import SequenceMatcher
    return SequenceMatcher(None, left, right).ratio()


HEADER_ALIASES = [
    ('ipn', [
        'ipn', 'internal part number', 'internalpartnumber', '内部编号',
        '内部料号', '物料编码', '物料编号', '料号', '编码',
    ]),
    ('lcsc', [
        'lcsc', 'lcsc part', 'lcsc part number', '立创料号', '立创编号',
        '嘉立创料号', '供应商料号', 'seed',
    ]),
    ('mpn', [
        'mpn', 'manufacturer part', 'manufacturerpart', 'manufacturer part number',
        'mfr part', '原厂料号', '制造商料号', '厂家型号', '型号', 'part number',
    ]),
    ('designators', [
        'designator', 'designators', 'reference', 'references', 'refdes',
        'ref', '位置', '位号', '元件标号', '器件位号',
    ]),
    ('quantity', [
        'quantity', 'qty', '数量', '用量', '个数',
    ]),
    ('footprint', [
        'footprint', 'package', '封装', '封装/尺寸', '尺寸', 'footprint name',
    ]),
    ('comment', [
        'comment', 'value', 'values', 'description', '规格', '规格描述',
        '元件值', '值', '名称',
    ]),
    ('supplier', [
        'supplier', 'vendor', 'manufacturer', '品牌', '供应商', '制造商',
    ]),
]


def _clean_header(value) -> str:
    return re.sub(r'[\s_\-/]+', ' ', str(value or '').strip().lower())


def _header_mapping(cells):
    """Map a header row to standard keys."""
    mapping = {}
    for index, cell in enumerate(cells):
        header = _clean_header(cell)
        if not header:
            continue

        for key, aliases in HEADER_ALIASES:
            if key in mapping:
                continue
            if any(alias == header or alias in header for alias in aliases):
                mapping[key] = index
                break

    return mapping


def _parse_quantity(value):
    text = str(value or '').strip()
    if not text:
        return 1
    match = re.search(r'-?\d+(?:[.,]\d+)?', text.replace(' ', ''))
    if not match:
        return 1
    number = match.group(0).replace(',', '.')
    try:
        value = float(number)
        return int(value) if value.is_integer() else value
    except ValueError:
        return 1


def _decode_text(data: bytes) -> str:
    for encoding in ('utf-8-sig', 'utf-8', 'gb18030', 'gbk', 'latin-1'):
        try:
            return data.decode(encoding)
        except Exception:
            continue
    return data.decode('utf-8', 'replace')


def _rows_from_csv(data: bytes):
    text = _decode_text(data)
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=',;\t|')
    except Exception:
        dialect = csv.excel

    reader = csv.reader(io.StringIO(text), dialect)
    return [[str(cell or '').strip() for cell in row] for row in reader]


def _rows_from_xlsx(data: bytes):
    try:
        from openpyxl import load_workbook
    except Exception as exc:  # pragma: no cover
        raise ValueError('openpyxl 不可用，无法解析 Excel') from exc

    workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    sheet = workbook.active
    rows = []
    for row in sheet.iter_rows(values_only=True):
        rows.append([('' if cell is None else str(cell)).strip() for cell in row])
    return rows


def _parse_rows(rows):
    """Return {columns, mapping, header_row, rows} from a raw table."""
    rows = [row for row in rows if any(str(cell).strip() for cell in row)]
    if not rows:
        return {'columns': [], 'mapping': {}, 'header_row': 0, 'rows': []}

    best_index = 0
    best_mapping = {}
    best_score = 0

    for index, row in enumerate(rows[:20]):
        mapping = _header_mapping(row)
        score = len(mapping)
        if score > best_score:
            best_index = index
            best_mapping = mapping
            best_score = score

    if best_score < 2:
        best_index = 0
        best_mapping = _header_mapping(rows[0]) if rows else {}

    headers = rows[best_index]
    data_rows = rows[best_index + 1:]

    parsed = []
    for row_index, row in enumerate(data_rows):
        if not any(str(cell).strip() for cell in row):
            continue

        def cell(key):
            column = best_mapping.get(key)
            if column is None or column >= len(row):
                return ''
            return str(row[column] or '').strip()

        designators = cell('designators')
        quantity = _parse_quantity(cell('quantity'))

        parsed.append({
            'line': row_index + 1,
            'designators': designators,
            'quantity': quantity,
            'comment': cell('comment'),
            'footprint': cell('footprint'),
            'mpn': cell('mpn'),
            'ipn': cell('ipn'),
            'lcsc': cell('lcsc'),
            'supplier': cell('supplier'),
            'raw': row,
        })

    return {
        'columns': headers,
        'mapping': best_mapping,
        'header_row': best_index,
        'rows': parsed,
    }


_LIBRARY_CACHE = {'timestamp': 0.0, 'count': -1, 'index': None}


def _library_index(force=False):
    """Build a lightweight in-memory part index for fuzzy matching."""
    now = time.time()
    part_count = Part.objects.count()

    if (
        not force
        and _LIBRARY_CACHE['index'] is not None
        and now - _LIBRARY_CACHE['timestamp'] < 60
        and part_count == _LIBRARY_CACHE['count']
    ):
        return _LIBRARY_CACHE['index']

    location_map = {}
    try:
        stock_rows = (
            StockItem.objects.filter(quantity__gt=0)
            .values('part_id', 'location__pathstring', 'location__name')
            .annotate(total=Sum('quantity'))
        )
        for row in stock_rows:
            location_map.setdefault(row['part_id'], []).append({
                'path': row.get('location__pathstring') or row.get('location__name') or '',
                'name': row.get('location__name') or '',
                'quantity': float(row.get('total') or 0),
            })
        for locations in location_map.values():
            locations.sort(key=lambda item: item['quantity'], reverse=True)
    except Exception:
        location_map = {}

    index = []
    parts = (
        Part.objects.filter(active=True)
        .prefetch_related('parameters_list__template')
    )

    for part in parts:
        params = []
        try:
            for parameter in part.parameters_list.all():
                template = getattr(parameter, 'template', None)
                if template is None:
                    continue
                params.append({
                    'name': template.name,
                    'value': str(parameter.data or ''),
                    'units': template.units or '',
                })
        except Exception:
            params = []

        texts = [part.name or '', part.IPN or '', part.description or '']
        texts += [f"{p['name']} {p['value']}" for p in params]

        index.append({
            'pk': part.pk,
            'name': part.name or '',
            'ipn': part.IPN or '',
            'description': part.description or '',
            'parameters': params,
            'texts': texts,
            'stock': float(part.total_stock or 0),
            'stock_locations': location_map.get(part.pk, []),
        })

    _LIBRARY_CACHE.update({
        'timestamp': now,
        'count': part_count,
        'index': index,
    })
    return index


def _score_part(item, row):
    """Return (score, matched_on) in the range 0..1."""
    score = 0.0
    reasons = []

    row_ipn = row.get('ipn') or ''
    row_mpn = row.get('mpn') or ''
    row_lcsc = row.get('lcsc') or ''
    row_comment = row.get('comment') or ''
    row_footprint = row.get('footprint') or ''

    # Exact identifiers
    for query, label in ((row_ipn, 'IPN'), (row_mpn, 'MPN'), (row_lcsc, 'LCSC')):
        if not query:
            continue
        if item['ipn'] and _norm(query) == _norm(item['ipn']):
            score = max(score, 1.0)
            reasons.append(f'{label}=IPN')
        if item['name'] and _norm(query) == _norm(item['name']):
            score = max(score, 0.97)
            reasons.append(f'{label}=Name')
        for param in item['parameters']:
            if _norm(query) == _norm(param['value']):
                score = max(score, 0.95)
                reasons.append(f"{label}={param['name']}")

    # Value / comment matching
    if row_comment:
        comment_score = max(
            _ratio(row_comment, item['name']),
            _ratio(row_comment, item['description']),
        )
        for param in item['parameters']:
            comment_score = max(
                comment_score,
                _ratio(row_comment, param['value']),
                _ratio(row_comment, f"{param['name']} {param['value']}"),
            )
        if comment_score >= 0.6:
            score = max(score, comment_score * 0.92)
            reasons.append('Value')

    # Footprint matching: required here because many values/footprints repeat
    if row_footprint:
        footprint_score = 0.0
        for param in item['parameters']:
            footprint_score = max(
                footprint_score,
                _ratio(row_footprint, param['value']),
                _ratio(row_footprint, f"{param['name']} {param['value']}"),
            )
        if footprint_score >= 0.7:
            score = max(score, footprint_score * 0.9)
            reasons.append('Footprint')
        else:
            score *= 0.65
    else:
        footprint_score = 0.0

    # General fuzzy text
    combined = ' '.join(x for x in (row_mpn, row_lcsc, row_comment, row_footprint) if x)
    if combined:
        for text in item['texts'][:8]:
            text_score = _ratio(combined, text)
            if text_score >= 0.65:
                score = max(score, text_score * 0.88)
                reasons.append('Text')

    return min(score, 1.0), reasons


def _match_row(row, index, limit=3):
    scored = []
    for item in index:
        score, reasons = _score_part(item, row)
        if score <= 0:
            continue
        scored.append((score, reasons, item))

    scored.sort(key=lambda entry: entry[0], reverse=True)
    candidates = []
    for score, reasons, item in scored[:limit]:
        candidates.append({
            'pk': item['pk'],
            'name': item['name'],
            'ipn': item['ipn'],
            'description': item['description'],
            'score': round(score, 4),
            'matched_on': sorted(set(reasons))[:4],
            'stock': item['stock'],
            'stock_locations': item.get('stock_locations', [])[:6],
        })

    best = candidates[0] if candidates else None
    if best:
        best_score = best['score']
        if best_score >= 0.9:
            status = 'matched'
        elif best_score >= 0.65:
            status = 'review'
        else:
            status = 'unmatched'
    else:
        best_score = 0.0
        status = 'unmatched'

    quantity = float(row.get('quantity') or 0)
    stock = float(best['stock']) if best else 0.0

    return {
        **row,
        'status': status,
        'score': best_score,
        'candidates': candidates,
        'matched_part': best,
        'stock': stock,
        'shortage': max(0.0, quantity - stock),
    }


class BomImportPlugin(SettingsMixin, UrlsMixin, UserInterfaceMixin, InvenTreePlugin):
    """Import a BOM table and match lines against the InvenTree part library."""

    NAME = 'BOM Import'
    SLUG = 'bom-import'
    TITLE = _('BOM Import / Match')
    DESCRIPTION = _(
        'Import a BOM file, match every line against the part library, '
        'and aggregate designators / quantities / stock.'
    )
    VERSION = '1.0.0'
    AUTHOR = 'cocositu'

    SETTINGS = {
        'ENABLE_NAVIGATION': {
            'name': _('Enable Navigation Entry'),
            'description': _('Show a BOM Import entry in the navigation bar'),
            'default': True,
            'validator': bool,
        },
        'MATCH_THRESHOLD': {
            'name': _('Match Threshold'),
            'description': _('Scores below this value are marked as review/unmatched'),
            'default': 0.65,
            'validator': float,
        },
    }

    # ------------------------------------------------------------------
    # URL views
    # ------------------------------------------------------------------
    def view_index(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return HttpResponse('请先登录 InvenTree 后再打开 BOM Import。', status=403)

        page = Path(__file__).parent / 'static' / 'index.html'
        response = HttpResponse(
            page.read_text(encoding='utf-8'),
            content_type='text/html; charset=utf-8',
        )
        # 允许 InvenTree 前端 /web/bom-import/ 页面以同源 iframe 方式嵌入
        response['X-Frame-Options'] = 'SAMEORIGIN'
        return response

    def view_parse(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({'error': 'permission denied'}, status=403)

        upload = request.FILES.get('file')
        text = request.POST.get('text') or ''

        try:
            if upload is not None:
                data = upload.read()
                suffix = str(upload.name or '').lower()
                if suffix.endswith(('.xlsx', '.xlsm')):
                    rows = _rows_from_xlsx(data)
                else:
                    rows = _rows_from_csv(data)
            elif text.strip():
                rows = _rows_from_csv(text.encode('utf-8'))
            else:
                return JsonResponse({'error': '请选择 BOM 文件或粘贴表格内容'}, status=400)
        except Exception as exc:
            return JsonResponse({'error': f'解析失败: {exc}'}, status=400)

        payload = _parse_rows(rows)
        payload['row_count'] = len(payload['rows'])
        return JsonResponse(payload)

    def view_match(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({'error': 'permission denied'}, status=403)

        import json

        try:
            body = json.loads(request.body or b'{}')
        except Exception:
            return JsonResponse({'error': 'invalid json'}, status=400)

        rows = body.get('rows') or []
        if not isinstance(rows, list):
            return JsonResponse({'error': 'rows must be a list'}, status=400)

        index = _library_index()
        threshold = float(self.get_setting('MATCH_THRESHOLD') or 0.65)

        results = []
        for row in rows:
            result = _match_row(row, index)
            if result['score'] < threshold and result['status'] == 'matched':
                result['status'] = 'review'
            results.append(result)

        matched = sum(1 for row in results if row['status'] == 'matched')
        review = sum(1 for row in results if row['status'] == 'review')
        unmatched = sum(1 for row in results if row['status'] == 'unmatched')
        total_qty = sum(float(row.get('quantity') or 0) for row in results)
        shortage = sum(float(row.get('shortage') or 0) for row in results if row['status'] == 'matched')

        return JsonResponse({
            'rows': results,
            'summary': {
                'total': len(results),
                'matched': matched,
                'review': review,
                'unmatched': unmatched,
                'total_quantity': total_qty,
                'shortage': shortage,
            },
        })

    def view_search(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({'error': 'permission denied'}, status=403)

        query = (request.GET.get('q') or '').strip()
        if not query:
            return JsonResponse({'results': []})

        parts = (
            Part.objects.filter(active=True)
            .filter(
                Q(name__icontains=query)
                | Q(IPN__icontains=query)
                | Q(description__icontains=query)
                | Q(parameters_list__data__icontains=query)
            )
            .distinct()[:20]
        )

        return JsonResponse({
            'results': [
                {
                    'pk': part.pk,
                    'name': part.name,
                    'ipn': part.IPN or '',
                    'description': part.description or '',
                    'stock': float(part.total_stock or 0),
                }
                for part in parts
            ]
        })

    @transaction.atomic
    def _deduct_items(self, items, user):
        """Deduct matched BOM quantities from unallocated stock.

        Uses ``StockItem.take_stock(...)`` so InvenTree stock history is preserved.
        Returns a per-item result list and summary.
        """
        details = []
        total_deducted = Decimal('0')
        total_shortage = Decimal('0')

        for entry in items:
            try:
                part = Part.objects.get(pk=int(entry.get('pk')))
                quantity = Decimal(str(entry.get('quantity') or 0))
            except (Part.DoesNotExist, TypeError, ValueError, InvalidOperation):
                details.append({
                    'pk': entry.get('pk'),
                    'line': entry.get('line'),
                    'designators': entry.get('designators') or '',
                    'deducted': 0,
                    'shortage': float(entry.get('quantity') or 0),
                    'error': '零件不存在或数量非法',
                })
                continue

            if quantity <= 0:
                continue

            remaining = quantity
            deducted = Decimal('0')
            used_items = []

            stock_items = (
                StockItem.objects.filter(part=part, quantity__gt=0)
                .filter(Q(serial__isnull=True) | Q(serial=''))
                .order_by('expiry_date', 'pk')
            )

            for stock_item in stock_items:
                if remaining <= 0:
                    break

                available = stock_item.unallocated_quantity()
                if available <= 0:
                    continue

                take = min(available, remaining)
                note = f"BOM Import: {entry.get('designators') or entry.get('line') or ''}".strip()
                result = stock_item.take_stock(take, user, notes=note)

                if result is False:
                    continue

                deducted += take
                remaining -= take
                used_items.append({
                    'stock_item': stock_item.pk,
                    'quantity': float(take),
                })

                total_deducted += take

            shortage = max(remaining, Decimal('0'))
            total_shortage += shortage

            details.append({
                'pk': part.pk,
                'line': entry.get('line'),
                'designators': entry.get('designators') or '',
                'part_name': part.name,
                'requested': float(quantity),
                'deducted': float(deducted),
                'shortage': float(shortage),
                'stock_items': used_items,
            })

        return {
            'details': details,
            'deducted': float(total_deducted),
            'shortage': float(total_shortage),
        }

    @require_http_methods(['POST'])
    def view_deduct(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({'error': 'permission denied'}, status=403)

        if not StockItem.check_related_permission('change', request.user):
            return JsonResponse({'error': '没有修改库存的权限'}, status=403)

        import json

        try:
            body = json.loads(request.body or b'{}')
        except Exception:
            return JsonResponse({'error': 'invalid json'}, status=400)

        items = body.get('items') or []
        if not isinstance(items, list) or not items:
            return JsonResponse({'error': '没有可扣减的行'}, status=400)

        result = self._deduct_items(items, request.user)
        return JsonResponse(result)

    def setup_urls(self):
        return [
            path(
                '',
                ensure_csrf_cookie(self.view_index),
                name='bom-import-index',
            ),
            path(
                'parse/',
                require_http_methods(['POST'])(self.view_parse),
                name='bom-import-parse',
            ),
            path(
                'match/',
                require_http_methods(['POST'])(self.view_match),
                name='bom-import-match',
            ),
            path('search/', self.view_search, name='bom-import-search'),
            path(
                'deduct/',
                require_http_methods(['POST'])(self.view_deduct),
                name='bom-import-deduct',
            ),
        ]

    # ------------------------------------------------------------------
    # UI hooks
    # ------------------------------------------------------------------
    def get_ui_navigation_items(self, request, context, **kwargs):
        # 指向 InvenTree 前端自己的路由页 /web/bom-import/，
        # 前端路由页会加载独立的 /plugin/bom-import/ 页面。
        if not self.get_setting('ENABLE_NAVIGATION'):
            return []
        return [
            {
                'key': 'bom-import-nav',
                'title': str(_('BOM Import / Match')),
                'icon': 'ti:file-import:outline',
                'options': {'url': '/bom-import/'},
            }
        ]

    def get_ui_dashboard_items(self, request, context, **kwargs):
        if not self.get_setting('ENABLE_NAVIGATION'):
            return []
        return [
            {
                'key': 'bom-import-dashboard',
                'title': str(_('BOM Import / Match')),
                'description': str(
                    _('Import a BOM file and match every line against the part library')
                ),
                'icon': 'ti:file-import:outline',
                'source': self.plugin_static_file(
                    'dashboard.js:renderBomImportDashboard', check_hash=False
                ),
                'options': {'width': 6, 'height': 4},
            }
        ]