import {
  CellStyleModule,
  ClientSideRowModelModule,
  ColumnAutoSizeModule,
  CsvExportModule,
  DateFilterModule,
  NumberFilterModule,
  PinnedRowModule,
  QuickFilterModule,
  RowApiModule,
  RowStyleModule,
  TextFilterModule,
  TooltipModule,
  ValidationModule,
  themeBalham,
  type Module,
} from "ag-grid-community";

/**
 * AG Grid Community only (never Enterprise modules), Balham theme tuned to the Merxa
 * tokens in globals.css. Colours are literal values because AG Grid compiles the theme
 * to CSS at runtime.
 */
export const merxaGridTheme = themeBalham.withParams({
  fontFamily: ["Source Sans 3 Variable", "Source Sans 3", "Segoe UI", "system-ui", "sans-serif"],
  fontSize: 13,
  headerFontSize: 12,
  headerFontWeight: 600,
  accentColor: "#0d6271",
  backgroundColor: "#ffffff",
  foregroundColor: "#22201c",
  textColor: "#22201c",
  borderColor: "#dcd8d0",
  headerBackgroundColor: "#ece9e3",
  headerTextColor: "#4a463f",
  oddRowBackgroundColor: "#fbfaf8",
  rowHoverColor: "#edf6f8",
  selectedRowBackgroundColor: "#d7ebef",
  borderRadius: 2,
  wrapperBorderRadius: 4,
  spacing: 4,
  cellHorizontalPadding: 8,
  browserColorScheme: "light",
});

export const gridModules: Module[] = [
  ClientSideRowModelModule,
  TextFilterModule,
  NumberFilterModule,
  DateFilterModule,
  QuickFilterModule,
  CsvExportModule,
  RowApiModule, // getDisplayedRowCount, redrawRows
  RowStyleModule,
  CellStyleModule,
  ColumnAutoSizeModule,
  TooltipModule,
  PinnedRowModule,
  ...(process.env.NODE_ENV === "production" ? [] : [ValidationModule]),
];
