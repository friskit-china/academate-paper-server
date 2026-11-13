// Browser preference keys are a versioned UI-data contract, independent of product branding.
const UI_STORAGE_NAMESPACE = "paper-reader-ui:v1";

export const BILINGUAL_MODE_CHANGE_EVENT = "paper-reader-ui:bilingual-mode-change";

export const UI_STORAGE_KEYS = {
  libraryVisibleColumns: `${UI_STORAGE_NAMESPACE}:library:visible-columns`,
  libraryColumnWidths: `${UI_STORAGE_NAMESPACE}:library:column-widths`,
  libraryColumnOrder: `${UI_STORAGE_NAMESPACE}:library:column-order`,
  libraryColumnSchemaVersion: `${UI_STORAGE_NAMESPACE}:library:column-schema-version`,
  libraryPreviewLength: `${UI_STORAGE_NAMESPACE}:library:preview-length`,
  readingMode: `${UI_STORAGE_NAMESPACE}:reader:reading-mode`,
  readerLeftSidebarWidth: `${UI_STORAGE_NAMESPACE}:reader:left-sidebar-width`,
  readerRightSidebarWidth: `${UI_STORAGE_NAMESPACE}:reader:right-sidebar-width`,
  readerLeftSidebarCollapsed: `${UI_STORAGE_NAMESPACE}:reader:left-sidebar-collapsed`,
  readerRightSidebarCollapsed: `${UI_STORAGE_NAMESPACE}:reader:right-sidebar-collapsed`,
} as const;
