"""JSONL write-ahead log append, replay, and truncate after formatted save."""
import json
import os
import tempfile
from unittest.mock import patch

import pandas as pd

from availity_app.automation import _write_results_file
from availity_app.constants import OUTPUT_COLUMNS, STATUS_COLUMNS
from availity_app.progress_wal import (
    WAL_FILENAME,
    append_wal_rows,
    clear_wal,
    replay_wal,
    wal_path,
)


def _df(rows=2):
    data = {
        'AltPatientID': ['M1', 'M2'][:rows] + ['M1'] * max(0, rows - 2),
        'PatientName': ['Doe, Jane'] * rows,
        'DOB': ['01/01/1990'] * rows,
        'StartDate': ['01/01/2025'] * rows,
        'EndDate': ['01/31/2025'] * rows,
        'InvoiceNumber': [f'INV{i}' for i in range(rows)],
        'VisitDate': ['01/05/2025'] * rows,
    }
    for col in OUTPUT_COLUMNS + STATUS_COLUMNS:
        data[col] = [''] * rows
    return pd.DataFrame(data)


def test_append_and_replay_onto_fresh_dataframe():
    source = _df()
    source.at[0, 'Claim Status'] = 'Paid'
    source.at[0, 'AutomationStatus'] = 'Done'
    source.at[0, 'LastError'] = ''
    with tempfile.TemporaryDirectory() as tmp:
        append_wal_rows(source, tmp, [0])
        path = wal_path(tmp)
        assert os.path.exists(path)
        with open(path, encoding='utf-8') as handle:
            rec = json.loads(handle.readline())
        assert rec['idx'] == 0
        assert rec['invoice'] == 'INV0'
        assert rec['fields']['Claim Status'] == 'Paid'

        fresh = _df()
        applied = replay_wal(fresh, tmp)
        assert applied == 1
        assert fresh.at[0, 'Claim Status'] == 'Paid'
        assert fresh.at[0, 'AutomationStatus'] == 'Done'
        assert fresh.at[1, 'Claim Status'] == ''


def test_replay_skips_mismatched_identity():
    source = _df()
    source.at[0, 'Claim Status'] = 'Paid'
    with tempfile.TemporaryDirectory() as tmp:
        append_wal_rows(source, tmp, [0])
        fresh = _df()
        fresh.at[0, 'InvoiceNumber'] = 'OTHER'
        fresh.at[0, 'AltPatientID'] = 'OTHER'
        applied = replay_wal(fresh, tmp)
        assert applied == 0
        assert fresh.at[0, 'Claim Status'] == ''


def test_replay_skips_ambiguous_identity():
    source = _df(rows=1)
    source.at[0, 'Claim Status'] = 'Paid'
    with tempfile.TemporaryDirectory() as tmp:
        append_wal_rows(source, tmp, [0])
        fresh = pd.concat([_df(rows=1), _df(rows=1)], ignore_index=True)
        # Force idx out of range so replay must search by identity; both rows match.
        path = wal_path(tmp)
        with open(path, encoding='utf-8') as handle:
            rec = json.loads(handle.readline())
        rec['idx'] = 99
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write(json.dumps(rec) + '\n')
        applied = replay_wal(fresh, tmp)
        assert applied == 0


def test_successful_formatted_save_clears_wal():
    df = _df()
    df.at[0, 'Claim Status'] = 'Paid'
    df.at[0, 'AutomationStatus'] = 'Done'

    def _fake_write(
        export_df, path, sheet_name='Sheet1', *,
        formatting_enabled=True, uppercase_enabled=False,
    ):
        with open(path, 'wb') as handle:
            handle.write(b'xlsx')

    with tempfile.TemporaryDirectory() as tmp:
        append_wal_rows(df, tmp, [0])
        assert os.path.exists(os.path.join(tmp, WAL_FILENAME))
        with patch(
            'availity_app.automation.write_formatted_workbook',
            side_effect=_fake_write,
        ):
            saved = _write_results_file(df, tmp, finalize=True)
        assert saved is not None
        assert not os.path.exists(os.path.join(tmp, WAL_FILENAME))


def test_failed_save_keeps_wal():
    df = _df()
    with tempfile.TemporaryDirectory() as tmp:
        append_wal_rows(df, tmp, [0])
        with patch(
            'availity_app.automation.write_formatted_workbook',
            side_effect=OSError('disk full'),
        ):
            saved = _write_results_file(df, tmp, finalize=False)
        assert saved is None
        assert os.path.exists(os.path.join(tmp, WAL_FILENAME))


def test_clear_wal_missing_file_is_ok():
    with tempfile.TemporaryDirectory() as tmp:
        clear_wal(tmp)
        clear_wal('')
