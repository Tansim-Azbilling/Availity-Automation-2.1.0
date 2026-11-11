import json
import os
import sys
import tempfile
import threading

import pytest
from openpyxl import Workbook, load_workbook

from availity_app.automation import save_progress_results
from availity_app.run_export import export_run
from availity_app.run_store import RunStore, import_recheck_edits, open_or_import, run_path
from availity_app.search_grouping import resolve_patient_cluster_search_dates
from availity_app.utils import resolve_bundle_search_dates


HEADERS = [
    'AltPatientID', 'PatientName', 'DOB', 'StartDate', 'EndDate',
    'InvoiceNumber', 'VisitDate', 'Amount', 'Recheck',
]


def _input(path, count):
    book = Workbook()
    sheet = book.active
    sheet.append(HEADERS)
    for i in range(count):
        sheet.append([
            f'M{i}', 'Doe, Jane', '01/02/1990', '01/01/2026', '01/31/2026',
            f'I{i // 2}', '01/10/2026', 100, '',
        ])
    book.save(path)


def test_streamed_run_batch_export_resume_and_recheck(monkeypatch):
    with tempfile.TemporaryDirectory() as root:
        monkeypatch.setenv('LOCALAPPDATA', root)
        source = os.path.join(root, 'collection.xlsx')
        output = os.path.join(root, 'output')
        os.makedirs(output)
        _input(source, 1000)
        path = open_or_import(source, output, 'Healthfirst')
        store = RunStore(path)
        assert len(store) == 1000
        assert len(store.pending_indices(200)) == 200
        assert store.invoice_indices('I0') == [0, 1]
        assert resolve_bundle_search_dates(store, 'I0') == ('01/10/2026', '01/10/2026')
        for pos in range(200):
            store.set(pos, 'Claim Status', 'Paid')
            store.set(pos, 'AutomationStatus', 'Done')
        save_progress_results(store, output)
        store.close()
        assert open_or_import(source, output, 'Healthfirst') == path

        exported = export_run(path, output)
        book = load_workbook(exported, read_only=True)
        sheet = book.worksheets[0]
        headers = [cell.value for cell in sheet[1]]
        status = headers.index('AutomationStatus')
        assert sheet.max_row == 1001
        assert sheet.cell(201, status + 1).value == 'Done'
        assert sheet.cell(202, status + 1).value == 'Pending'
        book.close()

        book = load_workbook(exported)
        col = [cell.value for cell in book.worksheets[0][1]].index('Recheck') + 1
        book.worksheets[0].cell(2, col, 'yes')
        book.save(exported)
        assert import_recheck_edits(path, exported) == 1
        store = RunStore(path)
        assert store.pending_indices(1) == [0]
        assert store.at[0, 'Claim Status'] == 'Paid'
        store.close()


def test_prior_output_migration_rejects_mismatch(monkeypatch):
    with tempfile.TemporaryDirectory() as root:
        monkeypatch.setenv('LOCALAPPDATA', root)
        source = os.path.join(root, 'source.xlsx')
        output = os.path.join(root, 'output')
        os.makedirs(output)
        _input(source, 2)
        path = open_or_import(source, output, 'Healthfirst')
        store = RunStore(path)
        store.set(0, 'Claim Status', 'Paid')
        store.set(0, 'AutomationStatus', 'Done')
        store.commit()
        store.close()
        exported = export_run(path, output)
        os.remove(path)
        imported = open_or_import(source, output, 'Healthfirst')
        store = RunStore(imported)
        assert store.at[0, 'Claim Status'] == 'Paid'
        assert store.pending_indices(2) == [1]
        store.close()
        os.remove(imported)
        book = load_workbook(exported)
        book.worksheets[0]['A2'] = 'different member'
        book.save(exported)
        with pytest.raises(ValueError, match='differs'):
            open_or_import(source, output, 'Healthfirst')
        assert not os.path.exists(imported)


def test_failed_export_preserves_previous_workbook(monkeypatch):
    with tempfile.TemporaryDirectory() as root:
        monkeypatch.setenv('LOCALAPPDATA', root)
        source = os.path.join(root, 'source.xlsx')
        output = os.path.join(root, 'output')
        _input(source, 2)
        path = open_or_import(source, output, 'Healthfirst')
        exported = export_run(path, output)
        with open(exported, 'rb') as handle:
            original = handle.read()
        def fail(*_args):
            raise OSError('simulated export failure')
        monkeypatch.setattr('availity_app.run_export._write', fail)
        with pytest.raises(OSError, match='simulated'):
            export_run(path, output)
        with open(exported, 'rb') as handle:
            assert handle.read() == original


def test_unformatted_export_keeps_excel_dates(monkeypatch):
    with tempfile.TemporaryDirectory() as root:
        monkeypatch.setenv('LOCALAPPDATA', root)
        source = os.path.join(root, 'source.xlsx')
        output = os.path.join(root, 'output')
        _input(source, 1)
        path = open_or_import(source, output, 'Healthfirst')
        monkeypatch.setattr('availity_app.run_export.get_output_formatting_enabled',
                            lambda: False)
        exported = export_run(path, output)
        book = load_workbook(exported, read_only=True)
        headers = [cell.value for cell in book.worksheets[0][1]]
        dob = book.worksheets[0].cell(2, headers.index('DOB') + 1).value
        assert dob.year == 1990
        book.close()


def test_interrupted_import_does_not_publish_partial_run(monkeypatch):
    with tempfile.TemporaryDirectory() as root:
        monkeypatch.setenv('LOCALAPPDATA', root)
        source = os.path.join(root, 'source.xlsx')
        output = os.path.join(root, 'output')
        _input(source, 2)
        def fail(*_args):
            raise InterruptedError('simulated interruption')
        monkeypatch.setattr('availity_app.run_store._row_record', fail)
        with pytest.raises(InterruptedError):
            open_or_import(source, output, 'Healthfirst')
        assert not os.path.exists(run_path(source, output, 'Healthfirst'))


def test_pending_selection_matches_existing_rules(monkeypatch):
    from availity_app.automation import _collect_pending_indices, load_and_prepare_excel
    with tempfile.TemporaryDirectory() as root:
        monkeypatch.setenv('LOCALAPPDATA', root)
        source = os.path.join(root, 'source.xlsx')
        output = os.path.join(root, 'output')
        _input(source, 4)
        book = load_workbook(source)
        book.active['I3'] = 'no'
        book.active['I4'] = 'yes'
        book.save(source)
        expected, _ = load_and_prepare_excel(source)
        path = open_or_import(source, output, 'Healthfirst')
        store = RunStore(path)
        assert store.pending_indices(None) == _collect_pending_indices(expected)
        store.close()


def test_patient_dates_include_rows_outside_selected_batch(monkeypatch):
    with tempfile.TemporaryDirectory() as root:
        monkeypatch.setenv('LOCALAPPDATA', root)
        source = os.path.join(root, 'source.xlsx')
        output = os.path.join(root, 'output')
        _input(source, 2)
        book = load_workbook(source)
        book.active['A3'] = 'M0'
        book.active['G3'] = '01/15/2026'
        book.save(source)
        path = open_or_import(source, output, 'Villagecaremax')
        store = RunStore(path)
        assert store.pending_indices(1) == [0]
        assert resolve_patient_cluster_search_dates(store, store.iloc[0]) == (
            '01/10/2026', '01/15/2026',
        )
        store.close()


def test_export_snapshot_allows_claim_commit(monkeypatch):
    with tempfile.TemporaryDirectory() as root:
        monkeypatch.setenv('LOCALAPPDATA', root)
        source = os.path.join(root, 'source.xlsx')
        output = os.path.join(root, 'output')
        _input(source, 2)
        path = open_or_import(source, output, 'Healthfirst')
        from availity_app import run_export
        original_write = run_export._write
        ready = threading.Event()
        release = threading.Event()
        errors = []
        def paused_write(*args):
            ready.set()
            assert release.wait(10)
            return original_write(*args)
        monkeypatch.setattr(run_export, '_write', paused_write)
        def worker():
            try:
                export_run(path, output)
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=worker)
        thread.start()
        assert ready.wait(10)
        store = RunStore(path)
        store.set(0, 'AutomationStatus', 'Done')
        store.commit()
        store.close()
        release.set()
        thread.join(10)
        assert not thread.is_alive() and not errors
        book = load_workbook(os.path.join(output, 'Automated.xlsx'), read_only=True)
        columns = [cell.value for cell in book.worksheets[0][1]]
        assert book.worksheets[0].cell(2, columns.index('AutomationStatus') + 1).value == 'Pending'
        book.close()


def test_automated_workbook_can_be_selected_again_after_export(monkeypatch):
    with tempfile.TemporaryDirectory() as root:
        monkeypatch.setenv('LOCALAPPDATA', root)
        source = os.path.join(root, 'original.xlsx')
        output = os.path.join(root, 'output')
        os.makedirs(output)
        _input(source, 2)
        original_run = open_or_import(source, output, 'Healthfirst')
        automated = export_run(original_run, output)
        run = open_or_import(automated, output, 'Healthfirst')
        assert export_run(run, output) == automated
        assert open_or_import(automated, output, 'Healthfirst') == run


def test_legacy_progress_log_overrides_older_excel_snapshot(monkeypatch):
    with tempfile.TemporaryDirectory() as root:
        monkeypatch.setenv('LOCALAPPDATA', root)
        source = os.path.join(root, 'source.xlsx')
        output = os.path.join(root, 'output')
        os.makedirs(output)
        _input(source, 1)
        first_run = open_or_import(source, output, 'Healthfirst')
        export_run(first_run, output)
        os.remove(first_run)
        with open(os.path.join(output, 'Automated_progress.jsonl'), 'w', encoding='utf-8') as handle:
            handle.write(json.dumps({
                'idx': 0, 'invoice': 'I0', 'visit': '01/10/2026', 'member': 'M0',
                'fields': {'Claim Status': 'Paid', 'AutomationStatus': 'Done'},
            }) + '\n')
        migrated = open_or_import(source, output, 'Healthfirst')
        store = RunStore(migrated)
        assert store.at[0, 'Claim Status'] == 'Paid'
        assert store.pending_indices(1) == []
        store.close()


@pytest.mark.skipif(os.environ.get('RUN_LARGE_SQLITE_TEST') != '1',
                    reason='Run explicitly for the 200,000-row acceptance check')
def test_200000_row_import_and_export(monkeypatch):
    with tempfile.TemporaryDirectory() as root:
        monkeypatch.setenv('LOCALAPPDATA', root)
        source = os.path.join(root, 'large.xlsx')
        output = os.path.join(root, 'output')
        os.makedirs(output)
        book = Workbook(write_only=True)
        sheet = book.create_sheet('Claims')
        sheet.append(HEADERS)
        for i in range(200_000):
            sheet.append([f'M{i}', 'Doe, Jane', '01/02/1990', '01/01/2026',
                          '01/31/2026', f'I{i // 2}', '01/10/2026', 100, ''])
        book.save(source)
        path = open_or_import(source, output, 'Healthfirst')
        store = RunStore(path)
        assert len(store) == 200_000
        assert len(store.pending_indices(200)) == 200
        store.close()
        exported = export_run(path, output)
        book = load_workbook(exported, read_only=True)
        assert book.worksheets[0].max_row == 200_001
        assert book.worksheets[0].cell(200_001, 1).value == 'M199999'
        book.close()
        if sys.platform == 'win32':
            import ctypes
            from ctypes import wintypes
            class MemoryCounters(ctypes.Structure):
                _fields_ = [
                    ('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD),
                    ('PeakWorkingSetSize', ctypes.c_size_t),
                    ('WorkingSetSize', ctypes.c_size_t),
                    ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
                    ('QuotaPagedPoolUsage', ctypes.c_size_t),
                    ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
                    ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                    ('PagefileUsage', ctypes.c_size_t),
                    ('PeakPagefileUsage', ctypes.c_size_t),
                ]
            counters = MemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            ctypes.windll.kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
            memory_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(MemoryCounters), wintypes.DWORD]
            memory_info.restype = wintypes.BOOL
            ok = memory_info(
                ctypes.windll.kernel32.GetCurrentProcess(),
                ctypes.byref(counters), counters.cb,
            )
            assert ok and counters.PeakWorkingSetSize < 1_000_000_000
