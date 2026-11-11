"""Stream a SQLite run into the familiar formatted Automated.xlsx."""
import json
import os
import sqlite3
import tempfile
from datetime import date, datetime

import xlsxwriter

from availity_app.excel_export import (
    AUTOFIT_COL_PADDING, AUTOFIT_MAX_COL_WIDTH, AUTOFIT_MIN_COL_WIDTH,
    CELL_BORDER_COLOR, DATE_COLUMNS, DATE_NUM_FORMAT, HEADER_FILL_COLOR,
    HEADER_FONT_COLOR, HEADER_ROW_HEIGHT, RECHECK_LIST_SHEET,
    SKIP_UPPERCASE_COLUMNS, _autofit_len_for_width, _excel_writable,
    _parse_export_date, _safe_sheet_name, _uppercase_cell_text,
    _write_used_row, row_height_for_visual_lines, visual_line_count,
)
from availity_app import state
from availity_app.user_settings import (
    get_output_formatting_enabled, get_uppercase_export_enabled,
)


def _value(value, column, uppercase):
    if column in DATE_COLUMNS:
        if isinstance(value, str) and value:
            for fmt in ('%Y-%m-%d', '%m/%d/%Y', '%Y-%m-%dT%H:%M:%S'):
                try:
                    return datetime.strptime(value, fmt)
                except ValueError:
                    pass
        return _parse_export_date(value)
    if uppercase and column not in SKIP_UPPERCASE_COLUMNS:
        return _uppercase_cell_text(value)
    return value


def _write(path, db, meta, count):
    columns = meta['export_columns']
    formatting = get_output_formatting_enabled()
    uppercase = get_uppercase_export_enabled()
    workbook = xlsxwriter.Workbook(path, {'constant_memory': True})
    try:
        sheet = workbook.add_worksheet(_safe_sheet_name(meta['sheet_name']))
        if formatting:
            header_format = workbook.add_format({
                'bold': True, 'font_name': 'Calibri', 'font_size': 11,
                'font_color': HEADER_FONT_COLOR, 'bg_color': HEADER_FILL_COLOR,
                'align': 'center', 'valign': 'vcenter', 'text_wrap': True,
                'border': 1, 'border_color': CELL_BORDER_COLOR,
            })
            cell_style = {
                'font_name': 'Calibri', 'font_size': 11, 'valign': 'vcenter',
                'text_wrap': True, 'border': 1, 'border_color': CELL_BORDER_COLOR,
            }
            data_format = workbook.add_format(cell_style)
            date_format = workbook.add_format({**cell_style, 'num_format': DATE_NUM_FORMAT})
            widths = [max(AUTOFIT_MIN_COL_WIDTH, min(AUTOFIT_MAX_COL_WIDTH,
                      _autofit_len_for_width(col) + AUTOFIT_COL_PADDING)) for col in columns]
            for rec in db.execute('SELECT data FROM rows ORDER BY pos LIMIT 100'):
                data = json.loads(rec[0])
                for i, col in enumerate(columns):
                    widths[i] = min(AUTOFIT_MAX_COL_WIDTH, max(
                        widths[i], _autofit_len_for_width(data.get(col)) + AUTOFIT_COL_PADDING,
                    ))
            for i, width in enumerate(widths):
                sheet.set_column(i, i, width)
            if columns and len(columns) < 16384:
                sheet.set_column(len(columns), 16383, None, None, {'hidden': True})
            sheet.set_row(0, HEADER_ROW_HEIGHT)
            sheet.freeze_panes(1, 0)
        else:
            header_format = data_format = date_format = None
            plain_date_format = workbook.add_format({'num_format': DATE_NUM_FORMAT})
            widths = None
        for i, column in enumerate(columns):
            sheet.write(0, i, column, header_format)
        for row_number, rec in enumerate(db.execute('SELECT data FROM rows ORDER BY pos'), 1):
            data = json.loads(rec[0])
            values = [_value(data.get(col, ''), col, uppercase) for col in columns]
            if formatting:
                _write_used_row(sheet, row_number, values, columns, data_format, date_format)
                lines = max(visual_line_count(value, widths[i])
                            for i, value in enumerate(values)) if values else 1
                height = row_height_for_visual_lines(lines)
                if height is not None:
                    sheet.set_row(row_number, height)
            else:
                for i, value in enumerate(values):
                    writable = _excel_writable(value)
                    if isinstance(writable, (date, datetime)):
                        sheet.write_datetime(row_number, i, writable, plain_date_format)
                    elif writable is None:
                        sheet.write_blank(row_number, i, None)
                    else:
                        sheet.write(row_number, i, writable)
        if formatting and count and 'Recheck' in columns:
            list_sheet = workbook.add_worksheet(RECHECK_LIST_SHEET)
            list_sheet.write(0, 0, 'yes')
            list_sheet.write(1, 0, 'no')
            list_sheet.hide()
            col = columns.index('Recheck')
            sheet.data_validation(1, col, count, col, {
                'validate': 'list', 'source': f"='{RECHECK_LIST_SHEET}'!$A$1:$A$2",
                'ignore_blank': True,
            })
    finally:
        workbook.close()


def export_run(run_path, output_folder):
    """Export a consistent read snapshot; preserve the prior file on failure."""
    from availity_app.automation import _replace_results_file
    with state.save_lock:
        os.makedirs(output_folder, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix='Automated_', suffix='.xlsx', dir=output_folder)
        os.close(fd)
        db = sqlite3.connect(run_path, timeout=30)
        try:
            db.execute('BEGIN')
            meta = {key: json.loads(value) for key, value in db.execute('SELECT key, value FROM meta')}
            count = db.execute('SELECT count(*) FROM rows').fetchone()[0]
            _write(temp, db, meta, count)
            db.rollback()
            saved = _replace_results_file(temp, os.path.join(output_folder, 'Automated.xlsx'))
            temp = None
            return saved
        finally:
            db.close()
            if temp and os.path.exists(temp):
                os.remove(temp)
