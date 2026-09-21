"""Workbook formatting: wrap math and xlsxwriter round-trip."""
import tempfile
import zipfile
from datetime import datetime

from openpyxl import load_workbook

from availity_app.constants import OUTPUT_COLUMNS, STATUS_COLUMNS
from availity_app.excel_export import (
    AUTOFIT_COL_PADDING,
    AUTOFIT_MAX_COL_WIDTH,
    AUTOFIT_MAX_ROW_HEIGHT,
    AUTOFIT_WRAPPED_MAX_COL_WIDTH,
    CELL_BORDER_COLOR,
    DATE_COLUMNS,
    DATE_NUM_FORMAT,
    HEADER_FILL_COLOR,
    RECHECK_LIST_SHEET,
    _column_widths_from_df,
    _excel_writable,
    dataframe_multiline_excel_rows,
    format_export_values,
    prepare_export_df,
    row_height_for_visual_lines,
    visual_line_count,
    write_formatted_workbook,
)
import pandas as pd


def _border_rgb(cell, side='left'):
    color = getattr(getattr(cell.border, side), 'color', None)
    rgb = getattr(color, 'rgb', None) if color is not None else None
    return str(rgb or '')


def _sample_export_df(rows=3, multiline_row=1):
    data = {
        'AltPatientID': ['M1'] * rows,
        'PatientName': ['Doe, Jane'] * rows,
        'DOB': ['01/01/1990'] * rows,
        'StartDate': ['01/01/2025'] * rows,
        'EndDate': ['01/31/2025'] * rows,
        'InvoiceNumber': [f'INV{i}' for i in range(rows)],
        'VisitDate': ['01/05/2025'] * rows,
        'Last_Name': ['Doe'] * rows,
        'First_Name': ['Jane'] * rows,
    }
    for col in OUTPUT_COLUMNS + STATUS_COLUMNS:
        data[col] = [''] * rows
    data['Claim Status'][multiline_row] = (
        '1. [CO-45] - Charge exceeds fee schedule/maximum allowable '
        'or contracted/legislated fee arrangement\n'
        '2. [CO-16] - Claim/service lacks information or has submission/'
        'billing error(s). Usage: Do not use this code for claims attachment(s)'
    )
    return pd.DataFrame(data)


def test_visual_line_count_includes_wrapped_long_lines():
    text = (
        '1. [CO-45] - Charge exceeds fee schedule/maximum allowable '
        'or contracted/legislated fee arrangement'
    )
    assert visual_line_count(text, col_width=60) > 1
    assert visual_line_count('Paid', col_width=60) == 1
    assert visual_line_count('1. Paid\n2. Denied', col_width=60) == 2


def test_visual_line_count_adds_wraps_inside_each_numbered_line():
    long_reason = (
        '[CO-16] - Claim/service lacks information or has submission/billing '
        'error(s). Usage: Do not use this code for claims attachment(s)/other '
        'documentation'
    )
    text = f'1. {long_reason}\n2. {long_reason}\n3. {long_reason}'
    assert visual_line_count(text, col_width=60) >= 6


def test_row_height_has_padding_and_is_not_capped_at_150():
    three = row_height_for_visual_lines(3)
    twelve = row_height_for_visual_lines(12)

    assert three is not None
    assert three > 15 * 3
    assert twelve is not None
    assert twelve > 150
    assert twelve <= AUTOFIT_MAX_ROW_HEIGHT
    assert row_height_for_visual_lines(1) is None


def test_prepare_export_df_drops_derived_name_columns():
    df = _sample_export_df()
    export_df = prepare_export_df(df)
    assert 'Last_Name' not in export_df.columns
    assert 'First_Name' not in export_df.columns
    assert 'Recheck' in export_df.columns
    assert list(export_df.columns)[-2:] == ['AutomationStatus', 'LastError']


def test_prepare_export_df_drops_match_confidence_when_not_an_output_column():
    df = _sample_export_df()
    df['Match Confidence'] = ['Need Manual Review'] * len(df)
    export_df = prepare_export_df(df)
    assert 'Match Confidence' not in export_df.columns


def test_formatted_workbook_round_trip():
    df = prepare_export_df(_sample_export_df())
    with tempfile.TemporaryDirectory() as tmp:
        path = f'{tmp}/Automated.xlsx'
        write_formatted_workbook(df, path, sheet_name='Claims')
        wb = load_workbook(path)
        ws = wb[wb.sheetnames[0]]

        assert ws.freeze_panes == 'A2'
        assert ws.row_dimensions[1].height == 22
        header = ws['A1']
        fill = str(getattr(header.fill.fgColor, 'rgb', None) or header.fill.start_color.rgb or '')
        assert HEADER_FILL_COLOR.upper() in fill.upper()
        assert header.font.bold is True
        assert header.alignment.wrap_text is True
        assert header.border.left.style == 'thin'
        assert CELL_BORDER_COLOR.upper() in _border_rgb(header).upper()

        data_cell = ws['A2']
        assert data_cell.alignment.wrap_text is True
        assert data_cell.alignment.vertical == 'center'
        assert data_cell.border.left.style == 'thin'
        assert CELL_BORDER_COLOR.upper() in _border_rgb(data_cell).upper()

        status_col = None
        for cell in ws[1]:
            if cell.value == 'Claim Status':
                status_col = cell.column
                break
        assert status_col is not None
        multiline_cell = ws.cell(row=3, column=status_col)
        assert ws.row_dimensions[3].height is not None
        assert ws.row_dimensions[3].height > 30
        assert multiline_cell.alignment.wrap_text is True

        assert RECHECK_LIST_SHEET in wb.sheetnames
        assert wb[RECHECK_LIST_SHEET].sheet_state == 'hidden'
        assert wb[RECHECK_LIST_SHEET]['A1'].value == 'yes'
        assert wb[RECHECK_LIST_SHEET]['A2'].value == 'no'
        assert len(ws.data_validations.dataValidation) >= 1
        dv = ws.data_validations.dataValidation[0]
        assert dv.type == 'list'
        assert RECHECK_LIST_SHEET in str(dv.formula1)


def test_dataframe_multiline_excel_rows():
    df = _sample_export_df(rows=3, multiline_row=1)
    assert dataframe_multiline_excel_rows(prepare_export_df(df)) == [3]


def test_wrapped_claim_status_does_not_max_out_column_width():
    df = prepare_export_df(_sample_export_df())
    widths = _column_widths_from_df(df)
    status_idx = list(df.columns).index('Claim Status')
    assert widths[status_idx] <= AUTOFIT_WRAPPED_MAX_COL_WIDTH + AUTOFIT_COL_PADDING
    assert widths[status_idx] < AUTOFIT_MAX_COL_WIDTH


def test_formatted_workbook_used_range_stays_in_data_cells():
    df = prepare_export_df(_sample_export_df())
    with tempfile.TemporaryDirectory() as tmp:
        path = f'{tmp}/Automated.xlsx'
        write_formatted_workbook(df, path, sheet_name='Claims')
        wb = load_workbook(path)
        ws = wb[wb.sheetnames[0]]
        n_cols = len(df.columns)
        n_rows = len(df) + 1

        assert ws.dimensions == f'A1:{ws.cell(n_rows, n_cols).coordinate}'
        assert ws.max_column == n_cols
        assert ws.max_row == n_rows

        status_col = None
        for cell in ws[1]:
            if cell.value == 'Claim Status':
                status_col = cell.column
                break
        assert status_col is not None
        assert ws.column_dimensions[ws.cell(1, status_col).column_letter].width < AUTOFIT_MAX_COL_WIDTH

        beyond = ws.cell(row=1, column=n_cols + 1)
        assert beyond.value is None
        assert beyond.border.left.style is None

        below = ws.cell(row=n_rows + 1, column=1)
        assert below.value is None
        assert below.alignment.wrap_text is not True
        assert below.border.left.style is None

        with zipfile.ZipFile(path) as zipped:
            sheet_xml = zipped.read('xl/worksheets/sheet1.xml').decode('utf-8')
        assert 'customFormat' not in sheet_xml
        cols_xml = sheet_xml.split('<sheetData>')[0]
        assert 'style="' not in cols_xml
        assert 'hidden="1"' in cols_xml
        assert 'max="16384"' in cols_xml

        multiline = ws.cell(row=3, column=status_col)
        assert 'CO-45' in str(multiline.value)
        assert multiline.alignment.wrap_text is True


def test_prepare_export_df_drops_unnamed_columns():
    df = _sample_export_df(rows=1, multiline_row=0)
    df['Unnamed: 12'] = ['']
    df['Unnamed: 13'] = [None]
    export_df = prepare_export_df(df)
    assert all(not str(c).startswith('Unnamed') for c in export_df.columns)


def test_excel_writable_keeps_dates_as_datetime():
    ts = pd.Timestamp('1990-01-15')
    assert _excel_writable(ts).year == 1990
    assert not isinstance(_excel_writable(ts), int)

    as_numpy = pd.to_datetime(['1990-01-15']).to_numpy()[0]
    converted = _excel_writable(as_numpy)
    assert converted is not None
    assert not isinstance(converted, int)
    assert getattr(converted, 'year', None) == 1990


def test_datetime_dob_is_not_written_as_excel_serial():
    df = prepare_export_df(_sample_export_df(rows=2, multiline_row=0))
    df['DOB'] = pd.to_datetime(['1990-01-15', '1991-02-16'])
    with tempfile.TemporaryDirectory() as tmp:
        path = f'{tmp}/Automated.xlsx'
        write_formatted_workbook(df, path)
        wb = load_workbook(path)
        ws = wb[wb.sheetnames[0]]
        dob_col = None
        for cell in ws[1]:
            if cell.value == 'DOB':
                dob_col = cell.column
                break
        assert dob_col is not None
        dob_cell = ws.cell(row=2, column=dob_col)
        assert not isinstance(dob_cell.value, int)
        assert dob_cell.value is not None
        assert '1990' in str(dob_cell.value) or getattr(dob_cell.value, 'year', None) == 1990
        assert DATE_NUM_FORMAT.lower() in str(dob_cell.number_format).lower()


def _header_col(ws, name):
    for cell in ws[1]:
        if cell.value == name:
            return cell.column
    return None


def test_format_export_values_uppercases_letter_text_only():
    df = pd.DataFrame({
        'PatientName': ['Doe, Jane'],
        'Claim Status': ['Paid'],
        'Denial Reason': ['Charge exceeds fee schedule'],
        'Last Action Taken': ['Reviewed'],
        'AutomationStatus': ['Done'],
        'LastError': ['Timeout'],
        'ClaimStatus': ['original status'],
        'AltPatientID': ['abc123'],
        'InvoiceNumber': ['inv99'],
        'Claim ID': ['clm-aa1'],
        'Check Number': ['chk99x'],
        'Billed Amount': ['$100.00'],
        'Paid Amount': ['$50.00'],
        'Amount': ['$25.00'],
        'Recheck': ['yes'],
        'DOB': ['01/15/1990'],
        'VisitDate': ['1/5/2025'],
        'StartDate': ['01/01/2025'],
        'EndDate': ['01/31/2025'],
        'Finalized Date': ['02/01/2025'],
        'Check Date': ['02/02/2025'],
    })
    out = format_export_values(df, uppercase_enabled=True)

    assert list(out.columns) == list(df.columns)
    assert out.at[0, 'PatientName'] == 'DOE, JANE'
    assert out.at[0, 'Claim Status'] == 'PAID'
    assert out.at[0, 'Denial Reason'] == 'CHARGE EXCEEDS FEE SCHEDULE'
    assert out.at[0, 'Last Action Taken'] == 'REVIEWED'
    assert out.at[0, 'AutomationStatus'] == 'DONE'
    assert out.at[0, 'LastError'] == 'TIMEOUT'
    assert out.at[0, 'ClaimStatus'] == 'ORIGINAL STATUS'

    assert out.at[0, 'AltPatientID'] == 'abc123'
    assert out.at[0, 'InvoiceNumber'] == 'inv99'
    assert out.at[0, 'Claim ID'] == 'clm-aa1'
    assert out.at[0, 'Check Number'] == 'chk99x'
    assert out.at[0, 'Billed Amount'] == '$100.00'
    assert out.at[0, 'Paid Amount'] == '$50.00'
    assert out.at[0, 'Amount'] == '$25.00'
    assert out.at[0, 'Recheck'] == 'yes'

    for col in DATE_COLUMNS:
        value = out.at[0, col]
        assert isinstance(value, datetime), col
        assert value.tzinfo is None


def test_format_export_values_leaves_mixed_case_by_default():
    df = pd.DataFrame({
        'PatientName': ['Doe, Jane'],
        'Claim Status': ['Paid'],
        'Denial Reason': ['Charge exceeds fee schedule'],
        'DOB': ['01/15/1990'],
    })
    out = format_export_values(df)

    assert out.at[0, 'PatientName'] == 'Doe, Jane'
    assert out.at[0, 'Claim Status'] == 'Paid'
    assert out.at[0, 'Denial Reason'] == 'Charge exceeds fee schedule'
    assert isinstance(out.at[0, 'DOB'], datetime)


def test_format_export_values_keeps_date_placeholder():
    df = pd.DataFrame({
        'Finalized Date': ['--'],
        'Check Date': ['--'],
        'VisitDate': [''],
    })
    out = format_export_values(df)
    assert out.at[0, 'Finalized Date'] == '--'
    assert out.at[0, 'Check Date'] == '--'
    assert out.at[0, 'VisitDate'] is None or pd.isna(out.at[0, 'VisitDate'])


def test_format_export_values_keeps_numbered_multi_claim_dates():
    numbered_dates = '1. 03/27/2026\n2. 06/25/2026'
    numbered_missing = '1. --\n2. --'
    df = pd.DataFrame({
        'Finalized Date': [numbered_dates],
        'Check Date': [numbered_missing],
    })
    out = format_export_values(df)
    assert out.at[0, 'Finalized Date'] == numbered_dates
    assert out.at[0, 'Check Date'] == numbered_missing
    assert isinstance(out.at[0, 'Finalized Date'], str)
    assert isinstance(out.at[0, 'Check Date'], str)


def test_formatted_workbook_applies_all_caps_and_date_format():
    df = prepare_export_df(_sample_export_df(rows=1, multiline_row=0))
    df.at[0, 'PatientName'] = 'Doe, Jane'
    df.at[0, 'Claim Status'] = 'Paid'
    df.at[0, 'Denial Reason'] = 'Charge exceeds fee schedule'
    df.at[0, 'AltPatientID'] = 'abc123'
    df.at[0, 'InvoiceNumber'] = 'inv99'
    df.at[0, 'Claim ID'] = 'clm-aa1'
    df.at[0, 'Check Number'] = 'chk99x'
    df.at[0, 'Billed Amount'] = '$100.00'
    df.at[0, 'Paid Amount'] = '$50.00'
    df.at[0, 'Recheck'] = 'yes'
    df.at[0, 'DOB'] = '01/15/1990'
    df.at[0, 'VisitDate'] = '1/5/2025'
    df.at[0, 'StartDate'] = '01/01/2025'
    df.at[0, 'EndDate'] = '01/31/2025'
    df.at[0, 'Finalized Date'] = '02/01/2025'
    df.at[0, 'Check Date'] = '--'
    with tempfile.TemporaryDirectory() as tmp:
        path = f'{tmp}/Automated.xlsx'
        write_formatted_workbook(df, path, uppercase_enabled=True)
        wb = load_workbook(path)
        ws = wb[wb.sheetnames[0]]

        assert _header_col(ws, 'PatientName') is not None
        assert ws.cell(1, _header_col(ws, 'PatientName')).value == 'PatientName'
        assert ws.cell(1, _header_col(ws, 'Claim Status')).value == 'Claim Status'

        assert ws.cell(2, _header_col(ws, 'PatientName')).value == 'DOE, JANE'
        assert ws.cell(2, _header_col(ws, 'Claim Status')).value == 'PAID'
        assert ws.cell(2, _header_col(ws, 'Denial Reason')).value == (
            'CHARGE EXCEEDS FEE SCHEDULE'
        )
        assert ws.cell(2, _header_col(ws, 'AltPatientID')).value == 'abc123'
        assert ws.cell(2, _header_col(ws, 'InvoiceNumber')).value == 'inv99'
        assert ws.cell(2, _header_col(ws, 'Claim ID')).value == 'clm-aa1'
        assert ws.cell(2, _header_col(ws, 'Check Number')).value == 'chk99x'
        assert ws.cell(2, _header_col(ws, 'Billed Amount')).value == '$100.00'
        assert ws.cell(2, _header_col(ws, 'Paid Amount')).value == '$50.00'
        assert ws.cell(2, _header_col(ws, 'Recheck')).value == 'yes'

        for name in ('DOB', 'VisitDate', 'StartDate', 'EndDate', 'Finalized Date'):
            cell = ws.cell(2, _header_col(ws, name))
            assert cell.value is not None
            assert not isinstance(cell.value, str), name
            assert getattr(cell.value, 'year', None) is not None
            assert DATE_NUM_FORMAT.lower() in str(cell.number_format).lower()

        check_date = ws.cell(2, _header_col(ws, 'Check Date'))
        assert check_date.value == '--'


def test_formatted_workbook_keeps_mixed_case_by_default():
    df = prepare_export_df(_sample_export_df(rows=1, multiline_row=0))
    df.at[0, 'PatientName'] = 'Doe, Jane'
    df.at[0, 'Claim Status'] = 'Paid'
    df.at[0, 'Denial Reason'] = 'Charge exceeds fee schedule'
    with tempfile.TemporaryDirectory() as tmp:
        path = f'{tmp}/Automated.xlsx'
        write_formatted_workbook(df, path)
        wb = load_workbook(path)
        ws = wb[wb.sheetnames[0]]
        assert ws.cell(2, _header_col(ws, 'PatientName')).value == 'Doe, Jane'
        assert ws.cell(2, _header_col(ws, 'Claim Status')).value == 'Paid'
        assert ws.cell(2, _header_col(ws, 'Denial Reason')).value == (
            'Charge exceeds fee schedule'
        )


def test_formatted_workbook_keeps_numbered_dates_as_text():
    df = prepare_export_df(_sample_export_df(rows=1, multiline_row=0))
    numbered_dates = '1. 03/27/2026\n2. 06/25/2026'
    numbered_missing = '1. --\n2. --'
    df.at[0, 'Finalized Date'] = numbered_dates
    df.at[0, 'Check Date'] = numbered_missing
    with tempfile.TemporaryDirectory() as tmp:
        path = f'{tmp}/Automated.xlsx'
        write_formatted_workbook(df, path)
        wb = load_workbook(path)
        ws = wb[wb.sheetnames[0]]

        finalized = ws.cell(2, _header_col(ws, 'Finalized Date'))
        check_date = ws.cell(2, _header_col(ws, 'Check Date'))
        assert finalized.value == numbered_dates
        assert check_date.value == numbered_missing
        assert isinstance(finalized.value, str)
        assert isinstance(check_date.value, str)
