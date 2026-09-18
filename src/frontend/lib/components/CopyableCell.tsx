import { Group } from '@mantine/core';
import { useState } from 'react';
import { CopyButton } from './CopyButton';

/**
 * A wrapper component that adds a copy button to cell content on hover
 * This component is used to make table cells copyable without adding visual clutter
 *
 * Layout notes
 * ------------
 * The button participates in the flex layout rather than being absolutely
 * positioned over the cell. The previous implementation placed it with
 * `position: absolute; right: 0; transform: translateY(-50%)` and no `top`,
 * which took it out of flow entirely - so it was painted *on top of* the
 * cell text instead of sitting beside it, and any value long enough to reach
 * the right edge had its tail covered by the button.
 *
 * The slot is now always reserved (`visibility: hidden` when not hovered) so
 * that hovering neither overlaps the text nor reflows it.
 *
 * @param children - The cell content to render
 * @param value - The value to copy when the copy button is clicked
 */
export function CopyableCell({
  children,
  value
}: Readonly<{
  children: React.ReactNode;
  value: string;
}>) {
  const [isHovered, setIsHovered] = useState(false);

  // The Clipboard API is only exposed in a secure context (HTTPS, or
  // localhost). Over plain HTTP - e.g. a LAN address such as
  // http://192.168.1.10:8000 - the browser does not provide it, so there is
  // nothing to render.
  const copyAvailable = window.isSecureContext && value != null;

  return (
    <Group
      gap={4}
      p={0}
      wrap='nowrap'
      onMouseEnter={() => setIsHovered(true)}
      onMouseLeave={() => setIsHovered(false)}
      justify='space-between'
      align='center'
      w='100%'
    >
      {/* minWidth:0 + overflow:hidden lets long values shrink and clip
          instead of pushing the button out of the cell */}
      <div style={{ flex: 1, minWidth: 0, overflow: 'hidden' }}>
        {children}
      </div>
      {copyAvailable && (
        <div
          style={{
            flexShrink: 0,
            // Reserve the space permanently: the button fades in without
            // moving the text and without covering it.
            visibility: isHovered ? 'visible' : 'hidden'
          }}
          onClick={(e) => e.stopPropagation()}
          onKeyDown={(e) => e.stopPropagation()}
        >
          <CopyButton value={value} variant={'default'} />
        </div>
      )}
    </Group>
  );
}
