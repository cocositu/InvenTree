import { createTheme } from '@mantine/core';
import { themeToVars } from '@mantine/vanilla-extract';
import {
  BREAKPOINTS,
  MONO_FONT_STACK,
  NEUTRAL_DARK,
  NEUTRAL_GRAY,
  UI_FONT_STACK
} from './styles/designTokens';

/**
 * 这份主题供 vanilla-extract 的 themeToVars() 生成 CSS 变量，
 * 被 main.css.ts、PanelGroup.css.ts、MainMenu.tsx 等布局层样式消费。
 *
 * 注意：这里必须与 ThemeContext.tsx 使用同一套令牌。
 * 之前它是空的 createTheme({})，导致布局层停留在 Mantine 默认的偏蓝灰，
 * 与组件层的中性灰不一致。
 */
export const theme = createTheme({
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
  defaultRadius: 'md',
  breakpoints: BREAKPOINTS
});

export const vars = themeToVars(theme);
