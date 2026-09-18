import {
  Alert,
  Anchor,
  Badge,
  Button,
  Checkbox,
  FileInput,
  Group,
  NumberInput,
  Paper,
  ScrollArea,
  Select,
  Stack,
  Table,
  Text,
  Textarea,
  Title
} from '@mantine/core';
import {
  IconAlertCircle,
  IconDownload,
  IconPlayerPlay,
  IconUpload
} from '@tabler/icons-react';
import { useMemo, useState } from 'react';

import { getCsrfCookie } from '../functions/auth';

type StockLocation = {
  path: string;
  name: string;
  quantity: number;
};

type Candidate = {
  pk: number;
  name: string;
  ipn: string;
  description: string;
  score: number;
  matched_on: string[];
  stock: number;
  stock_locations: StockLocation[];
};

type RowResult = {
  line: number;
  designators: string;
  quantity: number;
  comment: string;
  footprint: string;
  mpn: string;
  lcsc: string;
  status: 'matched' | 'review' | 'unmatched';
  score: number;
  stock: number;
  shortage: number;
  candidates: Candidate[];
};

type Summary = {
  total: number;
  matched: number;
  review: number;
  unmatched: number;
  total_quantity: number;
  shortage: number;
};

type Notice = {
  color: 'green' | 'yellow' | 'red';
  text: string;
};

async function postJSON(url: string, payload: unknown) {
  const token = getCsrfCookie();
  const response = await fetch(url, {
    method: 'POST',
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      'X-CSRFToken': token ?? ''
    },
    body: JSON.stringify(payload)
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

async function postForm(url: string, form: FormData) {
  const token = getCsrfCookie();
  const response = await fetch(url, {
    method: 'POST',
    credentials: 'include',
    headers: { 'X-CSRFToken': token ?? '' },
    body: form
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

function parseMultiplier(value: number | string) {
  const parsed = Math.floor(Number(value));
  return Number.isFinite(parsed) && parsed > 0 ? parsed : 1;
}

function statusColor(status: string) {
  if (status === 'matched') return 'green';
  if (status === 'review') return 'yellow';
  return 'red';
}

function statusLabel(status: string) {
  if (status === 'matched') return '已匹配';
  if (status === 'review') return '待确认';
  return '未匹配';
}

export default function BomImport() {
  const [file, setFile] = useState<File | null>(null);
  const [text, setText] = useState('');
  const [rows, setRows] = useState<RowResult[]>([]);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [mapping, setMapping] = useState<Record<string, number>>({});
  const [busy, setBusy] = useState('');
  const [notice, setNotice] = useState<Notice | null>(null);
  const [globalMultiplier, setGlobalMultiplier] = useState<number | string>(1);
  const [selected, setSelected] = useState<Record<number, boolean>>({});
  const [multipliers, setMultipliers] = useState<Record<number, number | string>>({});

  const selectedCount = useMemo(
    () => Object.values(selected).filter(Boolean).length,
    [selected]
  );
  const allSelected = rows.length > 0 && selectedCount === rows.length;
  const someSelected = selectedCount > 0 && !allSelected;

  function toggleAll(checked: boolean) {
    const next: Record<number, boolean> = {};
    rows.forEach((_row, index) => {
      next[index] = checked;
    });
    setSelected(next);
  }

  function handleGlobalMultiplier(value: number | string) {
    setGlobalMultiplier(value);
    const factor = parseMultiplier(value);
    setMultipliers((current) => {
      const next = { ...current };
      rows.forEach((_row, index) => {
        next[index] = factor;
      });
      return next;
    });
  }

  function updateCandidate(index: number, candidate: Candidate) {
    setRows((current) =>
      current.map((row, rowIndex) => {
        if (rowIndex !== index) return row;
        const candidates = [
          candidate,
          ...row.candidates.filter((item) => item.pk !== candidate.pk)
        ];
        return {
          ...row,
          candidates,
          score: candidate.score,
          stock: candidate.stock,
          shortage: Math.max(0, Number(row.quantity || 0) - Number(candidate.stock || 0)),
          status:
            candidate.score >= 0.9
              ? 'matched'
              : candidate.score >= 0.65
                ? 'review'
                : 'unmatched'
        };
      })
    );
  }

  async function runMatch(payload: { rows: unknown[]; mapping?: Record<string, number> }) {
    setBusy('match');
    setNotice(null);
    try {
      const data = await postJSON('/plugin/bom-import/match/', { rows: payload.rows });
      setRows(data.rows || []);
      setSummary(data.summary || null);
      setMapping(payload.mapping || {});
      setSelected({});
      setMultipliers({});
    } catch (error: any) {
      setNotice({ color: 'red', text: error.message || '匹配失败' });
    } finally {
      setBusy('');
    }
  }

  async function parseAndMatch() {
    if (!file && !text.trim()) {
      setNotice({ color: 'yellow', text: '请选择 BOM 文件或粘贴表格内容' });
      return;
    }

    setBusy('parse');
    setNotice(null);
    try {
      const form = new FormData();
      if (file) form.append('file', file);
      else form.append('text', text);
      const parsed = await postForm('/plugin/bom-import/parse/', form);
      await runMatch(parsed);
    } catch (error: any) {
      setNotice({ color: 'red', text: error.message || '解析失败' });
      setBusy('');
    }
  }

  function collectItems() {
    const items: any[] = [];
    rows.forEach((row, index) => {
      const candidate = row.candidates?.[0];
      if (!candidate || Number(row.quantity || 0) <= 0 || row.status === 'unmatched') return;
      if (!selected[index]) return;

      const factor = parseMultiplier(multipliers[index] ?? 1);

      items.push({
        pk: candidate.pk,
        line: row.line,
        designators: row.designators,
        quantity: Number(row.quantity || 0) * factor,
        multiplier: factor,
        part_name: candidate.name
      });
    });
    return items;
  }

  async function deduct() {
    const items = collectItems();
    if (!items.length) {
      setNotice({ color: 'yellow', text: '请先勾选要扣减的行' });
      return;
    }

    const total = items.reduce((sum, item) => sum + Number(item.quantity || 0), 0);
    if (!window.confirm(`扣减：${items.length} 种元件，共 ${total} 个数量。确认从库存扣减？`)) {
      return;
    }

    setBusy('deduct');
    setNotice(null);
    try {
      const result = await postJSON('/plugin/bom-import/deduct/', { items });
      const refreshed = await postJSON('/plugin/bom-import/match/', { rows });
      setRows(refreshed.rows || []);
      setSummary(refreshed.summary || null);
      const failures = (result.details || []).filter((item: any) => item.error);
      setNotice({
        color: failures.length ? 'yellow' : 'green',
        text: `扣减完成：共扣 ${result.deducted}，缺口 ${result.shortage}${
          failures.length ? `，${failures.length} 行失败` : ''
        }`
      });
    } catch (error: any) {
      setNotice({ color: 'red', text: `扣减失败：${error.message || ''}` });
    } finally {
      setBusy('');
    }
  }

  function exportCSV() {
    const header = [
      'line', 'designators', 'quantity', 'comment', 'footprint', 'mpn', 'lcsc',
      'part_name', 'part_ipn', 'score', 'stock', 'shortage', 'status'
    ];
    const lines = [header.join(',')];
    for (const row of rows) {
      const candidate = row.candidates?.[0];
      lines.push(
        [
          row.line, row.designators, row.quantity, row.comment, row.footprint,
          row.mpn, row.lcsc, candidate?.name || '', candidate?.ipn || '',
          Math.round((row.score || 0) * 100), Math.round(row.stock || 0),
          Math.round(row.shortage || 0), statusLabel(row.status)
        ]
          .map((value) => `"${String(value ?? '').replaceAll('"', '""')}"`)
          .join(',')
      );
    }
    const blob = new Blob(['\ufeff' + lines.join('\n')], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = 'bom-match-result.csv';
    link.click();
    URL.revokeObjectURL(url);
  }

  return (
    <Stack gap='md'>
      <Group justify='space-between'>
        <div>
          <Title order={2}>BOM Import / Match</Title>
          <Text size='sm' c='dimmed'>
            导入 BOM 表，匹配库内元器件，汇总位号 / 数量 / 库位 / 缺料
          </Text>
        </div>
        <Button
          variant='light'
          leftSection={<IconDownload size={16} />}
          disabled={!rows.length}
          onClick={exportCSV}
        >
          导出结果 CSV
        </Button>
      </Group>

      {notice && (
        <Alert color={notice.color} icon={<IconAlertCircle size={16} />}>
          {notice.text}
        </Alert>
      )}

      <Paper withBorder p='md'>
        <Group align='flex-end'>
          <Checkbox
            label='整张 BOM（全选）'
            checked={allSelected}
            indeterminate={someSelected}
            onChange={(event) => toggleAll(event.currentTarget.checked)}
          />
          <NumberInput
            label='整张倍率'
            description='应用到所有行'
            min={1}
            allowDecimal={false}
            value={globalMultiplier}
            onChange={handleGlobalMultiplier}
            w={160}
          />
          <Button
            leftSection={<IconPlayerPlay size={16} />}
            disabled={!selectedCount}
            loading={busy === 'deduct'}
            onClick={deduct}
          >
            执行扣减 {selectedCount ? `(${selectedCount})` : ''}
          </Button>
          <Text size='xs' c='dimmed'>
            勾选行后可逐行改倍率；整张 BOM 勾选后倍率应用到全部行。只扣未占用库存，并写入库存历史。
          </Text>
        </Group>
      </Paper>

      <Paper withBorder p='md'>
        <Group align='flex-end'>
          <FileInput
            label='BOM 文件'
            placeholder='CSV / XLSX'
            leftSection={<IconUpload size={16} />}
            value={file}
            onChange={setFile}
            clearable
            w={260}
          />
          <Button onClick={parseAndMatch} loading={busy === 'parse'}>
            上传并解析
          </Button>
          <Text size='sm' c='dimmed'>或</Text>
          <Button variant='light' onClick={parseAndMatch} loading={busy === 'parse'}>
            粘贴内容解析
          </Button>
          <Button
            variant='subtle'
            onClick={() =>
              setText(
                'Designator,Quantity,Comment,Footprint,MPN,LCSC\n' +
                  'R1,1,10k,0603,RC0603FR-0710KL,\n' +
                  'C1 C2,2,100nF,0603,GRM188R71C104KA01D,\n' +
                  'C3 C4,2,10uF,0603,CL10A106MQ8NNNC,\n' +
                  'U1,1,AMS1117-3.3,SOT-223,AMS1117-3.3,'
              )
            }
          >
            填入示例
          </Button>
        </Group>
        <Textarea
          mt='sm'
          label='粘贴表格'
          autosize
          minRows={3}
          maxRows={8}
          value={text}
          onChange={(event) => setText(event.currentTarget.value)}
          placeholder='Designator,Quantity,Comment,Footprint,MPN'
        />
        {!!Object.keys(mapping).length && (
          <Text size='xs' c='dimmed' mt={6}>
            识别列：{Object.entries(mapping).map(([key, value]) => `${key} → 第${value + 1}列`).join('，')}
          </Text>
        )}
      </Paper>

      {summary && (
        <Group>
          <Badge color='blue' variant='light'>总行数 {summary.total}</Badge>
          <Badge color='green' variant='light'>已匹配 {summary.matched}</Badge>
          <Badge color='yellow' variant='light'>待确认 {summary.review}</Badge>
          <Badge color='red' variant='light'>未匹配 {summary.unmatched}</Badge>
          <Badge color='grape' variant='light'>总需求 {summary.total_quantity}</Badge>
          <Badge color={summary.shortage ? 'orange' : 'green'} variant='light'>
            缺料 {summary.shortage}
          </Badge>
        </Group>
      )}

      {!!rows.length && (
        <Paper withBorder p={0}>
          <ScrollArea>
            <Table striped highlightOnHover withTableBorder style={{ minWidth: 1500 }}>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th w={40} />
                  <Table.Th>行</Table.Th>
                  <Table.Th>位号</Table.Th>
                  <Table.Th>数量</Table.Th>
                  <Table.Th>规格 / Value</Table.Th>
                  <Table.Th>封装</Table.Th>
                  <Table.Th>MPN / LCSC</Table.Th>
                  <Table.Th style={{ minWidth: 220 }}>匹配元件</Table.Th>
                  <Table.Th style={{ minWidth: 180 }}>库位 / 仓库</Table.Th>
                  <Table.Th>匹配度</Table.Th>
                  <Table.Th>库存</Table.Th>
                  <Table.Th>缺料</Table.Th>
                  <Table.Th w={90}>倍率</Table.Th>
                  <Table.Th>状态</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {rows.map((row, index) => {
                  const candidate = row.candidates?.[0];
                  return (
                    <Table.Tr key={`${row.line}-${index}`}>
                      <Table.Td>
                        <Checkbox
                          checked={!!selected[index]}
                          disabled={!candidate}
                          onChange={(event) => {
                            const checked = event.currentTarget.checked;
                            setSelected((current) => ({
                              ...current,
                              [index]: checked
                            }));
                          }}
                        />
                      </Table.Td>
                      <Table.Td>{row.line || index + 1}</Table.Td>
                      <Table.Td>{row.designators}</Table.Td>
                      <Table.Td>{row.quantity}</Table.Td>
                      <Table.Td>{row.comment}</Table.Td>
                      <Table.Td>{row.footprint}</Table.Td>
                      <Table.Td>{row.mpn || row.lcsc}</Table.Td>
                      <Table.Td>
                        {candidate ? (
                          <Stack gap={2}>
                            <Anchor
                              href={`/web/part/${candidate.pk}/details`}
                              target='_blank'
                              size='sm'
                              fw={600}
                            >
                              {candidate.name}
                            </Anchor>
                            <Text size='xs' c='dimmed'>{candidate.ipn}</Text>
                            {row.candidates.length > 1 && (
                              <Select
                                size='xs'
                                data={row.candidates.map((item, candidateIndex) => ({
                                  value: String(candidateIndex),
                                  label: `${item.name}${item.ipn ? ` / ${item.ipn}` : ''} · ${Math.round(item.score * 100)}%`
                                }))}
                                value='0'
                                onChange={(value) => {
                                  const selectedCandidate = row.candidates[Number(value) || 0];
                                  if (selectedCandidate) updateCandidate(index, selectedCandidate);
                                }}
                              />
                            )}
                          </Stack>
                        ) : (
                          <Text size='sm' c='dimmed'>— 无候选 —</Text>
                        )}
                      </Table.Td>
                      <Table.Td>
                        {(candidate?.stock_locations || []).slice(0, 4).map((location) => (
                          <Text size='xs' key={`${location.path}-${location.quantity}`}>
                            {location.path} ({Math.round(location.quantity)})
                          </Text>
                        ))}
                        {!candidate?.stock_locations?.length && (
                          <Text size='xs' c='dimmed'>—</Text>
                        )}
                      </Table.Td>
                      <Table.Td>
                        <Text size='sm'>{Math.round((row.score || 0) * 100)}%</Text>
                        <Text size='xs' c='dimmed'>
                          {(candidate?.matched_on || []).join(', ')}
                        </Text>
                      </Table.Td>
                      <Table.Td>{Math.round(row.stock || 0)}</Table.Td>
                      <Table.Td>{Math.round(row.shortage || 0)}</Table.Td>
                      <Table.Td>
                        <NumberInput
                          size='xs'
                          w={76}
                          min={1}
                          allowDecimal={false}
                          value={multipliers[index] ?? 1}
                          onChange={(value) =>
                            setMultipliers((current) => ({ ...current, [index]: value }))
                          }
                        />
                      </Table.Td>
                      <Table.Td>
                        <Badge color={statusColor(row.status)} variant='light'>
                          {statusLabel(row.status)}
                        </Badge>
                      </Table.Td>
                    </Table.Tr>
                  );
                })}
              </Table.Tbody>
            </Table>
          </ScrollArea>
        </Paper>
      )}
    </Stack>
  );
}