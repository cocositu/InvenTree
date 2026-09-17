import { msg } from '@lingui/core/macro';
import { Trans } from '@lingui/react';
import {
  MantineProvider,
  type MantineThemeOverride,
  createTheme
} from '@mantine/core';
import { ModalsProvider } from '@mantine/modals';
import { Notifications } from '@mantine/notifications';
import { ContextMenuProvider } from 'mantine-contextmenu';
import type { JSX } from 'react';
import { useShallow } from 'zustand/react/shallow';
import { AboutInvenTreeModal } from '../components/modals/AboutInvenTreeModal';
import { HotkeyModal } from '../components/modals/HotkeyModal';
import { LicenseModal } from '../components/modals/LicenseModal';
import { QrModal } from '../components/modals/QrModal';
import { ServerInfoModal } from '../components/modals/ServerInfoModal';
import { useLocalState } from '../states/LocalState';
import { LanguageContext } from './LanguageContext';
import { colorSchema } from './colorSchema';

/**
 * 界面字体栈（中英文混排优化）
 *
 * InvenTree 原先未指定任何字体，中文环境下会回退到系统默认衬线字体
 * （Windows 上通常是「宋体」），小字号时笔画发虚、观感陈旧 —— 这是
 * 中文界面显得"落后"的主要原因之一。
 *
 * 策略：拉丁字体前置保证英文/数字字形质量，中文黑体后置兜底。
 * 全部使用系统已装字体，不加载 Web Font，零网络开销。
 */
const UI_FONT_STACK = [
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

/** 等宽字体栈：用于料号、序列号、代码等需要逐位对齐的场景 */
const MONO_FONT_STACK = [
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

export function ThemeContext({
  children
}: Readonly<{ children: JSX.Element }>) {
  const [userTheme] = useLocalState(useShallow((state) => [state.userTheme]));

  let customUserTheme: MantineThemeOverride | undefined = undefined;

  // Theme
  try {
    customUserTheme = createTheme({
      primaryColor: userTheme.primaryColor,
      white: userTheme.whiteColor,
      black: userTheme.blackColor,
      defaultRadius: userTheme.radius,
      breakpoints: {
        xs: '30em',
        sm: '48em',
        md: '64em',
        lg: '74em',
        xl: '90em'
      },

      // ---- 现代化设计令牌 ----
      // ---- 中性色板 ----
      // Mantine 默认灰阶偏蓝（#F1F3F5 / #E9ECEF），观感偏"企业软件"。
      // 换成纯中性灰后，层次完全靠「细边框 + 背景明度差」建立，
      // 视觉上接近 ChatGPT / Claude 的做法：干净、不抢内容、无彩色污染。
      colors: {
        // 浅色模式：0 最浅（背景），9 最深（文字）
        gray: [
          '#FAFAFA', '#F5F5F5', '#E5E5E5', '#D4D4D4', '#A3A3A3',
          '#737373', '#525252', '#404040', '#262626', '#171717'
        ],
        // 深色模式：0 最亮（文字），9 最暗（背景）
        dark: [
          '#E8E8E8', '#D4D4D4', '#B0B0B0', '#8A8A8A', '#3F3F3F',
          '#333333', '#2A2A2A', '#212121', '#1A1A1A', '#141414'
        ]
      },

      // 中英文混排字体栈（详见文件顶部的 UI_FONT_STACK 说明）
      fontFamily: UI_FONT_STACK,
      fontFamilyMonospace: MONO_FONT_STACK,
      // 标题字重降到 600：默认 700 在中文下偏粗、发糊
      headings: {
        fontFamily: UI_FONT_STACK,
        fontWeight: '600'
      },
      // 开启抗锯齿，中文笔画边缘更干净
      fontSmoothing: true,
      // 所有可交互元素统一为手型光标，交互意图更明确
      cursorType: 'pointer'
    });
  } catch (error) {
    console.error('Error creating theme with user settings:', error);
    // Fallback to default theme if there's an error
    customUserTheme = undefined;
  }

  return (
    <MantineProvider theme={customUserTheme} colorSchemeManager={colorSchema}>
      <ContextMenuProvider>
        <LanguageContext>
          <ModalsProvider
            labels={{
              confirm: <Trans id={msg`Submit`.id} />,
              cancel: <Trans id={msg`Cancel`.id} />
            }}
            modals={{
              info: ServerInfoModal,
              about: AboutInvenTreeModal,
              license: LicenseModal,
              qr: QrModal,
              hotkey: HotkeyModal
            }}
          >
            <Notifications />
            {children}
          </ModalsProvider>
        </LanguageContext>
      </ContextMenuProvider>
    </MantineProvider>
  );
}
