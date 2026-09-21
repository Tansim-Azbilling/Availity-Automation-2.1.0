"""Tests for save performance, throttle, and async queue coalescing."""
import os
import tempfile
import time
from unittest.mock import patch

import pandas as pd

from availity_app import async_save, automation, state
from availity_app.automation import (
    _LOCKED_OUTPUT_FILENAME,
    _replace_results_file,
    _should_save_progress,
    _write_results_file,
    load_excel,
    save_progress_results,
)
from availity_app.constants import OUTPUT_COLUMNS, STATUS_COLUMNS
from availity_app.excel_export import (
    dataframe_multiline_excel_rows,
    write_formatted_workbook,
)


def _sample_export_df(rows=3, multiline_row=1):
    data = {
        'AltPatientID': ['M1'] * rows,
        'PatientName': ['Doe, Jane'] * rows,
        'DOB': ['01/01/1990'] * rows,
        'StartDate': ['01/01/2025'] * rows,
        'EndDate': ['01/31/2025'] * rows,
        'InvoiceNumber': [f'INV{i}' for i in range(rows)],
        'VisitDate': ['01/05/2025'] * rows,
    }
    for col in OUTPUT_COLUMNS + STATUS_COLUMNS:
        data[col] = [''] * rows
    data['Claim Status'][multiline_row] = '1. Paid\n2. Denied'
    return pd.DataFrame(data)


def test_dataframe_multiline_excel_rows():
    df = _sample_export_df(rows=3, multiline_row=1)
    assert dataframe_multiline_excel_rows(df) == [3]


def test_progress_and_final_saves_use_formatted_xlsxwriter():
    df = _sample_export_df()
    assert not hasattr(automation, '_finalize_output_workbook')
    with tempfile.TemporaryDirectory() as tmp:
        with patch(
            'availity_app.automation.write_formatted_workbook',
            wraps=write_formatted_workbook,
        ) as mock_write:
            with patch('openpyxl.load_workbook') as mock_load:
                progress_path = _write_results_file(df, tmp, finalize=False)
                final_path = _write_results_file(df, tmp, finalize=True)
        assert progress_path is not None
        assert final_path is not None
        assert mock_write.call_count == 2
        mock_load.assert_not_called()
        for call in mock_write.call_args_list:
            assert call.kwargs.get('formatting_enabled') is True
            assert call.kwargs.get('uppercase_enabled') is False


def test_force_progress_save_enqueues_async_for_small_files():
    df = _sample_export_df()
    state.dirty_row_indices = set()
    state.rows_since_last_save = 0
    state.last_save_monotonic = 0.0
    with patch('availity_app.automation.save_sync_or_async') as mock_save:
        save_progress_results(df, '/tmp/out', force=True)
    mock_save.assert_called_once()
    _args, kwargs = mock_save.call_args
    assert kwargs.get('force_async') is True
    assert kwargs.get('finalize') is False


def test_force_progress_save_does_not_enqueue_excel_on_large_file():
    df = _sample_export_df(rows=2000)
    state.dirty_row_indices = set()
    state.rows_since_last_save = 0
    state.last_save_monotonic = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp:
        with patch(
            'availity_app.automation.get_large_file_save_interval', return_value=100,
        ):
            with patch('availity_app.automation.save_sync_or_async') as mock_save:
                save_progress_results(df, tmp, force=True)
            mock_save.assert_not_called()
    assert state.rows_since_last_save == 1


def test_small_file_saves_every_row():
    assert _should_save_progress(50, 0, 0.0) is True
    assert _should_save_progress(100, 0, 0.0) is True


def test_medium_file_saves_every_ten_rows_or_twenty_seconds():
    assert _should_save_progress(200, 0, 0.0) is False
    assert _should_save_progress(200, 9, 0.0) is False
    assert _should_save_progress(200, 10, 0.0) is True
    assert _should_save_progress(200, 3, 20.0) is True


def test_large_file_saves_every_hundred_rows_or_two_minutes():
    with patch('availity_app.automation.get_large_file_save_interval', return_value=100):
        assert _should_save_progress(2000, 0, 0.0) is False
        assert _should_save_progress(2000, 99, 0.0) is False
        assert _should_save_progress(2000, 100, 0.0) is True
        assert _should_save_progress(2000, 1, 120.0) is True


def test_enqueue_save_coalesces_progress_snapshots():
    async_save._queued_item = None
    async_save._worker_processing = False
    async_save._worker_thread = None

    with patch.object(async_save, 'start_save_worker'):
        folder = '/tmp/out'
        async_save.enqueue_save(pd.DataFrame({'a': [1]}), folder, finalize=False)
        async_save.enqueue_save(pd.DataFrame({'a': [2]}), folder, finalize=False)
        async_save.enqueue_save(pd.DataFrame({'a': [3]}), folder, finalize=False)

    assert async_save._queued_item is not None
    assert async_save._queued_item[0]['a'].iloc[0] == 3
    assert async_save._queued_item[2] is False


def test_enqueue_save_finalize_replaces_progress():
    async_save._queued_item = None
    async_save._worker_processing = False

    with patch.object(async_save, 'start_save_worker'):
        folder = '/tmp/out'
        async_save.enqueue_save(pd.DataFrame({'a': [1]}), folder, finalize=False)
        async_save.enqueue_save(pd.DataFrame({'a': [9]}), folder, finalize=True)

    assert async_save._queued_item[0]['a'].iloc[0] == 9
    assert async_save._queued_item[2] is True


def test_enqueue_save_does_not_replace_pending_finalize_with_progress():
    async_save._queued_item = None
    async_save._worker_processing = False

    with patch.object(async_save, 'start_save_worker'):
        folder = '/tmp/out'
        async_save.enqueue_save(pd.DataFrame({'a': [9]}), folder, finalize=True)
        async_save.enqueue_save(pd.DataFrame({'a': [1]}), folder, finalize=False)

    assert async_save._queued_item[0]['a'].iloc[0] == 9
    assert async_save._queued_item[2] is True


def test_replace_retries_then_overwrites_locked_file():
    with tempfile.TemporaryDirectory() as tmp:
        dest = os.path.join(tmp, 'Automated.xlsx')
        src = os.path.join(tmp, 'tmp_src.xlsx')
        with open(src, 'w', encoding='utf-8') as handle:
            handle.write('ok')
        with patch.object(automation, '_SAVE_REPLACE_ATTEMPTS', 3):
            with patch.object(automation, '_SAVE_REPLACE_RETRY_SECONDS', 0):
                with patch(
                    'availity_app.automation.os.replace',
                    side_effect=[PermissionError(13, 'Access is denied'), None],
                ) as mock_replace:
                    saved = _replace_results_file(src, dest)
        assert saved == dest
        assert mock_replace.call_count == 2


def test_replace_writes_autosave_when_destination_stays_locked():
    with tempfile.TemporaryDirectory() as tmp:
        dest = os.path.join(tmp, 'Automated.xlsx')
        src = os.path.join(tmp, 'tmp_src.xlsx')
        fallback = os.path.join(tmp, _LOCKED_OUTPUT_FILENAME)
        with open(src, 'w', encoding='utf-8') as handle:
            handle.write('ok')

        def _replace(src_path, dest_path):
            if os.path.normcase(dest_path) == os.path.normcase(dest):
                raise PermissionError(13, 'Access is denied')
            os.rename(src_path, dest_path)

        with patch.object(automation, '_SAVE_REPLACE_ATTEMPTS', 2):
            with patch.object(automation, '_SAVE_REPLACE_RETRY_SECONDS', 0):
                with patch('availity_app.automation.log_to_gui'):
                    with patch(
                        'availity_app.automation.os.replace', side_effect=_replace,
                    ):
                        saved = _replace_results_file(src, dest)
        assert saved == fallback
        assert os.path.exists(fallback)


def test_load_excel_releases_file_so_automated_xlsx_can_be_overwritten():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'Automated.xlsx')
        pd.DataFrame({
            'PatientName': ['Doe, Jane'],
            'InvoiceNumber': ['1'],
        }).to_excel(path, index=False)
        df, error = load_excel(path)
        assert error is None
        assert df is not None
        fd, temp_path = tempfile.mkstemp(suffix='.xlsx', dir=tmp)
        os.close(fd)
        pd.DataFrame({'PatientName': ['Roe, John']}).to_excel(temp_path, index=False)
        os.replace(temp_path, path)
        assert os.path.exists(path)
