"""One-pass formatted Excel export for Automated.xlsx (xlsxwriter)."""
import math
import re
from datetime import date, datetime

import pandas as pd

from availity_app.constants import STATUS_COLUMNS, resolved_output_columns

RECHECK_LIST_SHEET = '_AvailityLists'
AUTOFIT_MIN_COL_WIDTH = 8
AUTOFIT_MAX_COL_WIDTH = 60
# Long/multiline cells wrap in-place. Sizing the column to the unwrapped line
# (Excel AutoFit after selecting a whole row) creates a huge empty-looking gap.
AUTOFIT_WRAPPED_MAX_COL_WIDTH = 28
AUTOFIT_COL_PADDING = 2
AUTOFIT_ROW_HEIGHT_PER_LINE = 16.5
AUTOFIT_ROW_HEIGHT_PADDING = 6
AUTOFIT_MAX_ROW_HEIGHT = 409  # Excel maximum
AUTOFIT_WRAP_CHAR_FACTOR = 0.82
AUTOFIT_SAMPLE_ROWS = 100
CELL_BORDER_COLOR = '8F9AA8'
HEADER_FILL_COLOR = '3D5A73'
HEADER_FONT_COLOR = 'F8FAFC'
HEADER_ROW_HEIGHT = 22
DATE_NUM_FORMAT = 'mm/dd/yyyy'
DATE_COLUMNS = frozenset({
    'DOB',
    'VisitDate',
    'StartDate',
    'EndDate',
    'Finalized Date',
    'Check Date',
})
SKIP_UPPERCASE_COLUMNS = frozenset({
    'Recheck',
    'AltPatientID',
    'InvoiceNumber',
    'Claim ID',
    'Check Number',
    'Billed Amount',
    'Paid Amount',
    'Amount',
    'Match Confidence',
}) | DATE_COLUMNS

_INVALID_SHEET_CHARS = re.compile(r'[:\\/?*\[\]]')
# format_multi_claim_results numbers every field ("1. --", "1. 03/27/2026").
# Those strings must stay text; pandas treats the leading "1." as year 1 and
# Excel then shows ############ for the out-of-range date.
_NUMBERED_EXPORT_DATE_RE = re.compile(r'^\d+\.\s')
_EXCEL_MIN_YEAR = 1900
_EXCEL_MAX_YEAR = 9999


def _keep_export_date_as_text(text):
    """True when a date cell is merged, numbered, or a placeholder — not one date."""
    if text == '--':
        return True
    if '\n' in text or '\r' in text:
        return True
    return bool(_NUMBERED_EXPORT_DATE_RE.match(text))


def _excel_in_range_datetime(value):
    """Return a naive datetime only when Excel can display it; else None."""
    if isinstance(value, pd.Timestamp):
        if pd.isna(value):
            return None
        value = _timestamp_to_datetime(value)
    elif isinstance(value, datetime):
        value = value.replace(tzinfo=None) if value.tzinfo else value
    elif isinstance(value, date):
        value = datetime(value.year, value.month, value.day)
    else:
        return None
    if value.year < _EXCEL_MIN_YEAR or value.year > _EXCEL_MAX_YEAR:
        return None
    return value


def cell_text(value):
    """Return a display string for a cell, treating None/NaN as empty."""
    if value is None:
        return ''
    try:
        if pd.isna(value):
            return ''
    except (TypeError, ValueError):
        pass
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
    if isinstance(value, datetime):
        return value.strftime('%m/%d/%Y')
    if isinstance(value, date):
        return value.strftime('%m/%d/%Y')
    dtype = getattr(value, 'dtype', None)
    if dtype is not None and str(dtype).startswith('datetime64'):
        ts = pd.Timestamp(value)
        if pd.isna(ts):
            return ''
        return ts.strftime('%m/%d/%Y')
    text = str(value)
    if text.lower() == 'nan':
        return ''
    return text


def cell_display_length(value):
    """Return the longest line length for a cell value (handles multiline text)."""
    text = cell_text(value)
    if not text:
        return 0
    return max(len(line) for line in text.split('\n'))


def _autofit_len_for_width(value):
    """Length used for column width; long/wrapped cells do not widen the column."""
    text = cell_text(value)
    if not text:
        return 0
    longest = cell_display_length(value)
    if '\n' in text or longest > AUTOFIT_WRAPPED_MAX_COL_WIDTH:
        return min(longest, AUTOFIT_WRAPPED_MAX_COL_WIDTH)
    return longest


def _timestamp_to_datetime(ts):
    """Return a timezone-naive datetime for xlsxwriter date cells."""
    if ts.tz is not None:
        ts = ts.tz_convert('UTC').tz_localize(None)
    return ts.to_pydatetime()


def _excel_writable(value):
    """Convert a dataframe value to something xlsxwriter will accept.

    Dates stay dates. numpy datetime64 must not go through ``.item()``, which
    can return an Excel serial integer and break DOB / VisitDate columns.
    """
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass

    if isinstance(value, str):
        return None if value.lower() == 'nan' else value

    if isinstance(value, pd.Timestamp):
        return _timestamp_to_datetime(value)
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)

    dtype = getattr(value, 'dtype', None)
    if dtype is not None and str(dtype).startswith('datetime64'):
        ts = pd.Timestamp(value)
        if pd.isna(ts):
            return None
        return _timestamp_to_datetime(ts)

    module = type(value).__module__ or ''
    if module.startswith('numpy') and hasattr(value, 'item'):
        try:
            value = value.item()
        except (ValueError, AttributeError):
            return value
        return _excel_writable(value)
    return value


def _uppercase_cell_text(value):
    """Return ALL CAPS for letter text; leave non-strings unchanged."""
    if not isinstance(value, str):
        return value
    if value.lower() == 'nan':
        return ''
    return value.upper()


def _parse_export_date(value):
    """Coerce a cell to a naive datetime for Excel date columns.

    Placeholders (``--``), numbered multi-claim values, and unparseable
    text stay as text so Excel does not render ``############``. Empty/NaN
    become ``None`` so the cell is written blank.
    """
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass

    in_range = _excel_in_range_datetime(value)
    if in_range is not None:
        return in_range
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return cell_text(value) or str(value)

    if isinstance(value, str):
        text = value.strip()
        if not text or text.lower() == 'nan':
            return None
        if _keep_export_date_as_text(text):
            return text
        ts = pd.to_datetime(text, errors='coerce', dayfirst=False)
        parsed = _excel_in_range_datetime(ts)
        if parsed is None:
            return value
        return parsed

    ts = pd.to_datetime(value, errors='coerce', dayfirst=False)
    parsed = _excel_in_range_datetime(ts)
    if parsed is None:
        return value
    return parsed


def format_export_values(df, *, uppercase_enabled=False):
    """Apply optional ALL-CAPS text and date coercion without changing headers.

    Recheck, mixed IDs, and amount columns are left unchanged. Date columns
    become real datetimes (``mm/dd/yyyy`` on write). ALL-CAPS is off unless
    ``uppercase_enabled`` is True (the Settings checkbox).
    """
    out = df.copy()
    for col in out.columns:
        if col in DATE_COLUMNS:
            out[col] = out[col].map(_parse_export_date)
        elif not uppercase_enabled or col in SKIP_UPPERCASE_COLUMNS:
            continue
        else:
            out[col] = out[col].map(_uppercase_cell_text)
    return out


def _write_used_row(worksheet, row_idx, values_row, columns, data_format, date_format):
    """Write wrap/border format only on used columns — never the entire Excel row.

    Passing a cell format to ``set_row`` styles A:XFD and Excel shows a huge
    empty gap past the last data column. Date number format is applied only
    to DATE_COLUMNS so IDs and amounts stay general cells.
    """
    for col_idx, value in enumerate(values_row):
        writable = _excel_writable(value)
        col_name = columns[col_idx] if col_idx < len(columns) else None
        if writable is None:
            worksheet.write_blank(row_idx, col_idx, None, data_format)
        elif col_name in DATE_COLUMNS and isinstance(writable, datetime):
            worksheet.write_datetime(row_idx, col_idx, writable, date_format)
        else:
            worksheet.write(row_idx, col_idx, writable, data_format)


def visual_line_count(value, col_width):
    """Count wrapped display lines for a cell given its Excel column width."""
    text = cell_text(value)
    if not text:
        return 1
    usable = max(float(col_width or AUTOFIT_MIN_COL_WIDTH) * AUTOFIT_WRAP_CHAR_FACTOR, 1.0)
    total = 0
    for line in text.split('\n'):
        chars = len(line) if line else 1
        total += max(1, math.ceil(chars / usable))
    return total


def row_height_for_visual_lines(visual_lines):
    """Return a point height that keeps wrapped/centered text fully visible."""
    lines = max(int(visual_lines or 1), 1)
    if lines <= 1:
        return None
    return min(
        AUTOFIT_ROW_HEIGHT_PER_LINE * lines + AUTOFIT_ROW_HEIGHT_PADDING,
        AUTOFIT_MAX_ROW_HEIGHT,
    )


def dataframe_multiline_excel_rows(df):
    """Return 1-based Excel row numbers (incl. header offset) with newline cells."""
    rows = []
    for idx in range(len(df)):
        row = df.iloc[idx]
        for col in df.columns:
            val = row[col]
            if isinstance(val, str) and '\n' in val:
                rows.append(idx + 2)
                break
    return rows


def prepare_export_df(df):
    """Drop derived name columns and order output/status columns as in the report."""
    export_df = df.drop(columns=['Last_Name', 'First_Name'], errors='ignore')
    if (
        'Match Confidence' in export_df.columns
        and 'Match Confidence' not in resolved_output_columns()
    ):
        export_df = export_df.drop(columns=['Match Confidence'])
    unnamed = [c for c in export_df.columns if str(c).startswith('Unnamed')]
    if unnamed:
        export_df = export_df.drop(columns=unnamed)
    output_cols = [c for c in resolved_output_columns() if c in export_df.columns]
    status_cols = [c for c in STATUS_COLUMNS if c in export_df.columns]
    extra = output_cols + status_cols
    base_cols = [c for c in export_df.columns if c not in extra]
    return export_df.loc[:, base_cols + output_cols + status_cols]


def _safe_sheet_name(name):
    """Return an Excel-legal worksheet name (max 31 chars)."""
    text = _INVALID_SHEET_CHARS.sub('_', str(name or 'Sheet1')).strip() or 'Sheet1'
    return text[:31]


def _column_widths_from_df(df, sample_rows=AUTOFIT_SAMPLE_ROWS):
    """Column widths from header + sampled used cells (not unwrapped long text)."""
    n_rows = len(df)
    scan_indices = set(range(min(n_rows, sample_rows)))
    for excel_row in dataframe_multiline_excel_rows(df):
        df_idx = excel_row - 2
        if 0 <= df_idx < n_rows:
            scan_indices.add(df_idx)

    values = df.to_numpy()
    widths = []
    for col_idx, col in enumerate(df.columns):
        max_len = cell_display_length(col)
        for row_idx in scan_indices:
            max_len = max(max_len, _autofit_len_for_width(values[row_idx, col_idx]))
        widths.append(
            min(
                max(max_len + AUTOFIT_COL_PADDING, AUTOFIT_MIN_COL_WIDTH),
                AUTOFIT_MAX_COL_WIDTH,
            )
        )
    return widths


def write_formatted_workbook(
    export_df, path, sheet_name='Sheet1', *,
    formatting_enabled=True, uppercase_enabled=False,
):
    """Write ``export_df`` to ``path`` with header, wrap, widths, and Recheck dropdown."""
    df = format_export_values(
        prepare_export_df(export_df),
        uppercase_enabled=uppercase_enabled,
    )
    sheet_name = _safe_sheet_name(sheet_name)
    columns = list(df.columns)
    n_cols = len(df.columns)
    n_rows = len(df)

    with pd.ExcelWriter(path, engine='xlsxwriter') as writer:
        if not formatting_enabled:
            df.to_excel(
                writer,
                sheet_name=sheet_name,
                index=False,
                header=False,
                startrow=1,
            )
            worksheet = writer.sheets[sheet_name]
            for col_idx, name in enumerate(df.columns):
                worksheet.write(0, col_idx, str(name))
            return

        workbook = writer.book
        worksheet = workbook.add_worksheet(sheet_name)
        writer.sheets[sheet_name] = worksheet

        header_format = workbook.add_format({
            'bold': True,
            'font_name': 'Calibri',
            'font_size': 11,
            'font_color': HEADER_FONT_COLOR,
            'bg_color': HEADER_FILL_COLOR,
            'align': 'center',
            'valign': 'vcenter',
            'text_wrap': True,
            'border': 1,
            'border_color': CELL_BORDER_COLOR,
        })
        cell_style = {
            'font_name': 'Calibri',
            'font_size': 11,
            'valign': 'vcenter',
            'text_wrap': True,
            'border': 1,
            'border_color': CELL_BORDER_COLOR,
        }
        data_format = workbook.add_format(cell_style)
        date_format = workbook.add_format({**cell_style, 'num_format': DATE_NUM_FORMAT})

        widths = _column_widths_from_df(df)
        for col_idx in range(n_cols):
            width = widths[col_idx] if col_idx < len(widths) else AUTOFIT_MIN_COL_WIDTH
            # Width only. A wrap/border format here styles all 1,048,576 rows
            # and Excel treats that as used empty space below the data.
            worksheet.set_column(col_idx, col_idx, width)
        # Hide AJ:XFD (and the rest) so Excel does not show a blank grid
        # after LastError. Width None + hidden does not expand used range.
        if n_cols and n_cols < 16384:
            worksheet.set_column(n_cols, 16383, None, None, {'hidden': True})

        for col_idx, name in enumerate(df.columns):
            worksheet.write(0, col_idx, str(name), header_format)
        # Height only. A cell format on set_row styles every column (A:XFD).
        worksheet.set_row(0, HEADER_ROW_HEIGHT)
        worksheet.freeze_panes(1, 0)

        if n_rows and n_cols:
            values = df.to_numpy(dtype=object)
            for row_idx in range(n_rows):
                _write_used_row(
                    worksheet, row_idx + 1, values[row_idx], columns,
                    data_format, date_format,
                )
                max_lines = 1
                for col_idx in range(n_cols):
                    col_w = widths[col_idx]
                    max_lines = max(
                        max_lines,
                        visual_line_count(values[row_idx, col_idx], col_w),
                    )
                height = row_height_for_visual_lines(max_lines)
                if height is not None:
                    worksheet.set_row(row_idx + 1, height)

        if 'Recheck' in df.columns and n_rows >= 1:
            list_ws = workbook.add_worksheet(RECHECK_LIST_SHEET)
            list_ws.write(0, 0, 'yes')
            list_ws.write(1, 0, 'no')
            list_ws.hide()
            recheck_col = list(df.columns).index('Recheck')
            worksheet.data_validation(1, recheck_col, n_rows, recheck_col, {
                'validate': 'list',
                'source': f"='{RECHECK_LIST_SHEET}'!$A$1:$A$2",
                'ignore_blank': True,
                'error_title': 'Invalid Recheck',
                'error_message': 'Select yes or no, or leave blank for new rows.',
                'input_title': 'Recheck',
                'input_message': 'yes = reprocess row; no = skip; blank = new row',
            })
