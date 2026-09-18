import { Box } from '@mantine/core';

export default function BomImport() {
  return (
    <Box style={{ height: 'calc(100vh - 150px)', minHeight: 620 }}>
      <iframe
        title='BOM Import'
        src='/plugin/bom-import/'
        style={{
          width: '100%',
          height: '100%',
          border: '1px solid var(--mantine-color-gray-3)',
          borderRadius: 8,
          background: '#fff'
        }}
      />
    </Box>
  );
}
