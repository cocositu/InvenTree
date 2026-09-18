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
export function ThemeContext({
  children
}: Readonly<{ children: JSX.Element }>) {
  const [userTheme] = useLocalState(useShallow((state) => [state.userTheme]));

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
