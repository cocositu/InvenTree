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
import type { UserTheme } from '@lib/types/Core';
import {
  BREAKPOINTS,
  MONO_FONT_STACK,
  NEUTRAL_DARK,
  NEUTRAL_GRAY,
  SHADOWS,
  UI_FONT_STACK
} from '../styles/designTokens';
import { LanguageContext } from './LanguageContext';
import { colorSchema } from './colorSchema';

/**
 * 应用 Mantine 主题。
 *
 * 所有设计令牌统一来自 styles/designTokens.ts —— 与 theme.ts（供
 * vanilla-extract 布局层使用）共用同一份定义，避免出现两套色板。
 */
/**
 * Mantine 内置颜色键。primaryColor 必须是其中之一，否则 MantineProvider 会
 * 直接抛错、整个应用白屏。
 */
const VALID_COLORS = [
  'dark', 'gray', 'red', 'pink', 'grape', 'violet', 'indigo', 'blue',
  'cyan', 'teal', 'green', 'lime', 'yellow', 'orange'
];

/** Mantine 圆角档位 */
const VALID_RADII = ['xs', 'sm', 'md', 'lg', 'xl'];

/**
 * 校验并净化用户主题。
 *
 * 为什么必须做防御：
 *   用户主题会持久化到 localStorage，并由 auth.tsx 的 observeProfile()
 *   用服务端的 profile.theme 覆盖。上游那份覆盖逻辑是「按 userTheme 的全部
 *   键取值」，一旦服务端存的是残缺对象，缺失的键就会变成 undefined，
 *   zustand 的 persist 又是整体替换 —— 最终 primaryColor 为 undefined，
 *   MantineProvider 校验失败，页面变成一片空白，且控制台只有一行提示。
 *
 *   宁可用默认值兜底，也不能让整个应用起不来。
 */
function sanitizeUserTheme(raw: Partial<UserTheme> | undefined | null) {
  const t = raw ?? {};
  const asString = (v: unknown): string | undefined =>
    typeof v === 'string' && v.trim() !== '' ? v : undefined;
  const primary = asString(t.primaryColor);
  const radius = asString(t.radius);
  return {
    primaryColor:
      primary && VALID_COLORS.includes(primary) ? primary : 'indigo',
    whiteColor: asString(t.whiteColor) ?? '#fff',
    blackColor: asString(t.blackColor) ?? '#000',
    radius: radius && VALID_RADII.includes(radius) ? radius : 'md',
    loader: asString(t.loader) ?? 'oval'
  };
}

export function ThemeContext({
  children
}: Readonly<{ children: JSX.Element }>) {
  const [userThemeRaw] = useLocalState(
    useShallow((state) => [state.userTheme])
  );
  // 净化后再交给 Mantine，避免残缺主题导致白屏
  const userTheme = sanitizeUserTheme(userThemeRaw);

  let customUserTheme: MantineThemeOverride | undefined = undefined;

  try {
    customUserTheme = createTheme({
      // ---- 用户可自定义部分 ----
      primaryColor: userTheme.primaryColor,
      white: userTheme.whiteColor,
      black: userTheme.blackColor,
      defaultRadius: userTheme.radius,

      // ---- 统一设计令牌 ----
      breakpoints: BREAKPOINTS,
      colors: {
        gray: NEUTRAL_GRAY,
        dark: NEUTRAL_DARK
      },
      fontFamily: UI_FONT_STACK,
      fontFamilyMonospace: MONO_FONT_STACK,
      headings: {
        fontFamily: UI_FONT_STACK,
        fontWeight: '600'
      },
      // 低透明度多层叠加阴影，替换 Mantine 默认的单层深阴影
      shadows: SHADOWS,

      // ---- 交互细节 ----
      fontSmoothing: true,
      cursorType: 'pointer',
      // 主色填充时文字用白色，保证对比度
      primaryShade: { light: 6, dark: 8 }
    });
  } catch (error) {
    console.error('Error creating theme with user settings:', error);
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
