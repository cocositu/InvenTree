import { i18n } from '@lingui/core';
import { I18nProvider } from '@lingui/react';
import { LoadingOverlay, Text } from '@mantine/core';
import { type JSX, useEffect, useRef, useState } from 'react';

import { useStoredTableState } from '@lib/states/StoredTableState';
import { useShallow } from 'zustand/react/shallow';
import { api } from '../App';
import { markLocaleReady } from '../functions/localeReady';
import { useLocalState } from '../states/LocalState';
import { useServerApiState } from '../states/ServerApiState';
import { fetchGlobalStates } from '../states/states';

export const defaultLocale = 'en';

/*
 * Function which returns a record of supported languages.
 * Note that this is not a constant, as it is used in the LanguageSelect component
 */
export const getSupportedLanguages = (): Record<string, string> => {
  return {
    ar: 'العربية',
    bg: 'Български',
    cs: 'Čeština',
    da: 'Dansk',
    de: 'Deutsch',
    el: 'Ελληνικά',
    en: 'English',
    es: 'Español',
    es_MX: 'Español (México)',
    et: 'Eesti',
    fa: 'فارسی',
    fi: 'Suomi',
    fr: 'Français',
    he: 'עברית',
    hi: 'हिन्दी',
    hu: 'Magyar',
    it: 'Italiano',
    ja: '日本語',
    ko: '한국어',
    lt: 'Lietuvių',
    lv: 'Latviešu',
    nl: 'Nederlands',
    no: 'Norsk',
    pl: 'Polski',
    pt: 'Português',
    pt_BR: 'Português (Brasil)',
    ro: 'Română',
    ru: 'Русский',
    sk: 'Slovenčina',
    sl: 'Slovenščina',
    sr: 'Српски',
    sv: 'Svenska',
    th: 'ไทย',
    tr: 'Türkçe',
    uk: 'Українська',
    vi: 'Tiếng Việt',
    zh_Hans: '中文（简体）',
    zh_Hant: '中文（繁體）'
  };
};

export function LanguageContext({
  children
}: Readonly<{ children: JSX.Element }>) {
  const [language] = useLocalState(useShallow((state) => [state.language]));
  const [server] = useServerApiState(useShallow((state) => [state.server]));

  const [activeLocale, setActiveLocale] = useState<string | null>(null);

  useEffect(() => {
    // Update the locale based on prioritization:
    // 1. Locally selected locale
    // 2. Server default locale
    // 3. English (fallback)

    let locale: string | null = activeLocale;

    if (!!language) {
      locale = language;
    } else if (!!server.default_locale) {
      locale = server.default_locale;
    } else {
      locale = defaultLocale;
    }

    if (locale != activeLocale) {
      setActiveLocale(locale);
      activateLocale(locale);
    }
  }, [activeLocale, language, server.default_locale, defaultLocale]);

  const [loadedState, setLoadedState] = useState<
    'loading' | 'loaded' | 'error'
  >('loading');
  const isMounted = useRef(true);
  // 供超时回调读取最新状态：闭包里的 state 会拿到旧值
  const loadedStateRef = useRef(loadedState);
  loadedStateRef.current = loadedState;

  useEffect(() => {
    isMounted.current = true;

    let lang: string = language || defaultLocale;

    // Ensure that the selected language is supported
    if (!Object.keys(getSupportedLanguages()).includes(lang)) {
      lang = defaultLocale;
    }

    /*
     * 超时兜底。
     *
     * 本组件包裹整个应用，loadedState 停在 'loading' 时会渲染全屏
     * LoadingOverlay。而 activateLocale() 内部是对语言包 chunk 的动态
     * import —— 一旦该请求迟迟不返回（服务器卡顿、资源被中断），
     * 应用就会永久停在全屏遮罩上，只能刷新恢复。
     *
     * 这里加一道保险：超时后直接放行，界面按回退语言渲染，绝不卡死。
     */
    const loadTimeout = setTimeout(() => {
      if (isMounted.current && loadedStateRef.current === 'loading') {
        console.warn(
          'Locale bundle load timed out; continuing with the fallback locale.'
        );
        setLoadedState('loaded');
      }
    }, 8000);

    activateLocale(lang)
      .then(() => {
        clearTimeout(loadTimeout);
        if (isMounted.current) setLoadedState('loaded');

        /*
         * Configure the default Accept-Language header for all requests.
         * - Locally selected locale
         * - Server default locale
         * - en-us (backup)
         */
        const locales: (string | undefined)[] = [];

        if (!!lang && lang != 'pseudo-LOCALE') {
          locales.push(lang);
        }

        if (!!server.default_locale) {
          locales.push(server.default_locale);
        }

        if (locales.indexOf('en-us') < 0) {
          locales.push('en-us');
        }

        // Ensure that the locales are properly formatted
        const new_locales = locales
          .map((locale) => locale?.replaceAll('_', '-').toLowerCase())
          .join(', ');

        if (new_locales == api.defaults.headers.common['Accept-Language']) {
          return;
        }

        // Update default Accept-Language headers
        api.defaults.headers.common['Accept-Language'] = new_locales;

        // Reload server state (and refresh status codes). Forced: the
        // Accept-Language header actually changed (initial set, or a real
        // locale change), so this must not be skipped by the "already
        // fetched" guard even if another caller already fetched once.
        fetchGlobalStates(true);

        // Clear out cached table column names
        useStoredTableState.getState().clearTableColumnNames();
      })
      /* istanbul ignore next */
      .catch((err) => {
        clearTimeout(loadTimeout);
        console.error('ERR: Failed loading translations', err);
        if (isMounted.current) setLoadedState('error');
      });

    return () => {
      clearTimeout(loadTimeout);
      isMounted.current = false;
    };
  }, [language]);

  if (loadedState === 'loading') {
    return <LoadingOverlay visible={true} />;
  }

  /* istanbul ignore next */
  if (loadedState === 'error') {
    return (
      <Text>
        An error occurred while loading translations, see browser console for
        details.
      </Text>
    );
  }

  // only render the i18n Provider if the locales are fully activated, otherwise we end
  // up with an error in the browser console
  return <I18nProvider i18n={i18n}>{children}</I18nProvider>;
}

// This function is used to determine the locale to activate based on the prioritization rules.
export function getPriorityLocale(): string {
  const serverDefault = useServerApiState.getState().server.default_locale;
  const userDefault = useLocalState.getState().language;

  return userDefault || serverDefault || defaultLocale;
}

/**
 * 将语言代码归一化为前端 locale 目录名。
 *
 * 背景：后端 Django 使用连字符形式的 BCP-47 码（`zh-hans` / `es-mx`），
 * 而前端 locale 目录使用下划线形式（`zh_Hans` / `es_MX`）。
 *
 * 原实现直接 `locale.split('-')[0]`，把 `zh-hans` 截成 `zh`，但构建产物里
 * 只有 `zh_Hans` 目录 → 动态 import 抛错被 catch 吞掉 → 界面静默回退英文。
 * 这是「服务器默认语言设为中文却仍显示英文」的根因。
 */
export function normalizeLocaleDir(locale: string): string {
  const supported = getSupportedLanguages();

  // 1. 已经是受支持的键（用户在 UI 下拉里选择的格式）
  if (supported[locale]) {
    return locale;
  }

  // 2. 连字符 -> 下划线（zh-hans -> zh_Hans）
  const underscored = locale.replace(/-/g, '_');
  if (supported[underscored]) {
    return underscored;
  }

  // 3. 忽略大小写匹配（zh_hans -> zh_Hans）
  const lowered = underscored.toLowerCase();
  const ciMatch = Object.keys(supported).find(
    (key) => key.toLowerCase() === lowered
  );
  if (ciMatch) {
    return ciMatch;
  }

  // 4. 退回到基础语言（de-DE -> de）
  const base = underscored.split('_')[0].toLowerCase();
  const baseMatch = Object.keys(supported).find(
    (key) => key.split('_')[0].toLowerCase() === base
  );

  return baseMatch ?? underscored;
}

export async function activateLocale(locale: string | null) {
  if (!locale) {
    locale = getPriorityLocale();
  }

  const localeDir = normalizeLocaleDir(locale);

  try {
    const { messages } = await import(`../locales/${localeDir}/messages.ts`);
    i18n.load(locale, messages);
    i18n.activate(locale);
    markLocaleReady();
  } catch (err) {
    console.error(`Failed to load locale ${locale}:`, err);
  }
}
