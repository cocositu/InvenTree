/**
 * 全局设计令牌 —— 单一事实来源
 *
 * 为什么必须抽出来单独定义：
 *   Mantine 组件消费的是 ThemeContext.tsx 里的 createTheme()，
 *   而 vanilla-extract 样式（main.css.ts / PanelGroup.css.ts / MainMenu.tsx 等
 *   11 个文件）消费的是 theme.ts 里的 createTheme()。
 *
 *   这两处原先各写一份。ThemeContext 用了中性灰阶，theme.ts 却是空的
 *   createTheme({})，于是布局层继续用 Mantine 默认的偏蓝灰（#F8F9FA），
 *   组件层用中性灰（#FAFAFA）—— 两套色彩体系并存，观感发"脏"且极难定位。
 *
 *   现在统一为「一处定义、两处引用」。
 */

/** 浅色模式灰阶：0 最浅（背景）→ 9 最深（文字） */
// Mantine 的 colors 要求固定长度元组（10 个色阶），
// 因此必须用 as const 保持字面量元组类型，否则 TS 会推断成 string[] 而报错。
export const NEUTRAL_GRAY = [
  '#FAFAFA', '#F5F5F5', '#E5E5E5', '#D4D4D4', '#A3A3A3',
  '#737373', '#525252', '#404040', '#262626', '#171717'
] as const;

/** 深色模式灰阶：0 最亮（文字）→ 9 最暗（背景） */
export const NEUTRAL_DARK = [
  '#E8E8E8', '#D4D4D4', '#B0B0B0', '#8A8A8A', '#3F3F3F',
  '#333333', '#2A2A2A', '#212121', '#1A1A1A', '#141414'
] as const;

/**
 * 界面字体栈
 *
 * 整个界面使用 Maple Mono NF CN 等宽字体（自托管子集，见 styles/modern.css）。
 * 中英文宽度比 2:1，中英混排时基线对齐更整齐，工程类界面观感统一。
 *
 * 若要改回系统无衬线字体：删掉下面的 '"Maple Mono NF CN"' 一行即可，
 * 其余为各平台现代中文黑体兜底。
 */
export const UI_FONT_STACK = [
  '"Maple Mono NF CN"',
  '-apple-system',
  'BlinkMacSystemFont',
  '"Segoe UI Variable Text"',
  '"Segoe UI"',
  '"PingFang SC"',
  '"HarmonyOS Sans SC"',
  '"Microsoft YaHei UI"',
  '"Microsoft YaHei"',
  '"Noto Sans SC"',
  '"Source Han Sans SC"',
  '"Hiragino Sans GB"',
  'Roboto',
  'Helvetica',
  'Arial',
  'sans-serif',
  '"Apple Color Emoji"',
  '"Segoe UI Emoji"'
].join(', ');

/** 等宽字体栈：料号 / IPN / 序列号 / SKU / 条码 / 代码 */
export const MONO_FONT_STACK = [
  '"Maple Mono NF CN"',
  '"JetBrains Mono"',
  '"Cascadia Mono"',
  '"Cascadia Code"',
  'Consolas',
  '"SF Mono"',
  'Menlo',
  '"Sarasa Mono SC"',
  '"Noto Sans Mono CJK SC"',
  'monospace'
].join(', ');

/** 断点 */
export const BREAKPOINTS = {
  xs: '30em',
  sm: '48em',
  md: '64em',
  lg: '74em',
  xl: '90em'
} as const;

/**
 * 阴影梯度
 * 低透明度多层叠加，比单一深阴影更贴近真实光照，也更"轻"。
 */
export const SHADOWS = {
  xs: '0 1px 2px rgba(16, 24, 40, 0.04)',
  sm: '0 1px 2px rgba(16, 24, 40, 0.04), 0 1px 3px rgba(16, 24, 40, 0.06)',
  md: '0 2px 4px rgba(16, 24, 40, 0.04), 0 4px 12px rgba(16, 24, 40, 0.06)',
  lg: '0 4px 8px rgba(16, 24, 40, 0.05), 0 12px 24px rgba(16, 24, 40, 0.08)',
  xl: '0 8px 16px rgba(16, 24, 40, 0.06), 0 24px 48px rgba(16, 24, 40, 0.10)'
} as const;
