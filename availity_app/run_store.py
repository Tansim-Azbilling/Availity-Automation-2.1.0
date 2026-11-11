"""Durable, bounded row storage for one HHAExchange claim run."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from collections import OrderedDict
from contextlib import closing
from datetime import date, datetime

import pandas as pd
from openpyxl import load_workbook

from availity_app.constants import STATUS_COLUMNS, resolved_output_columns
from availity_app.utils import (
    format_invoice_number, normalize_search_date, safe_alt_patient_id,
    safe_invoice_number, validate_dataframe,
)


def _cell(value):
    if value is None:
        return ''
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    try:
        if pd.isna(value):
            return ''
    except (TypeError, ValueError):
        pass
    return value.item() if hasattr(value, 'item') else value


def _identity(data):
    return (
        safe_invoice_number(data, 'InvoiceNumber', ''),
        normalize_search_date(data.get('VisitDate')) or str(data.get('VisitDate') or ''),
        safe_alt_patient_id(data, 'AltPatientID'),
    )


def _eligible(data):
    from availity_app.automation import (
        _billing_review_skip_values, _normalize_recheck_value,
        _row_has_prior_error_values,
    )
    recheck = data.get('Recheck')
    claim = data.get('Claim Status')
    if _billing_review_skip_values(recheck, claim, data.get('Denial Reason')):
        return 0
    if _row_has_prior_error_values(
        claim, data.get('LastError'), data.get('AutomationStatus'),
    ):
        return 1
    kind = _normalize_recheck_value(recheck)
    if kind == 'yes':
        return 1
    if kind == 'no':
        return 0
    return int(str(data.get('AutomationStatus') or '').lower() not in ('done', 'skipped'))


def _database_dir():
    root = os.environ.get('LOCALAPPDATA')
    if not root:
        raise OSError('LOCALAPPDATA is unavailable; a persistent VDI user profile is required.')
    root = os.path.abspath(root)
    if root.startswith('\\\\'):
        raise OSError('LOCALAPPDATA is a network share; the live SQLite run needs local storage.')
    if sys.platform == 'win32':
        import ctypes
        drive = os.path.splitdrive(root)[0] + os.sep
        if ctypes.windll.kernel32.GetDriveTypeW(drive) == 4:
            raise OSError('LOCALAPPDATA is a mapped network drive; the live SQLite run needs local storage.')
    path = os.path.join(root, 'AvailityAutomation', 'runs')
    os.makedirs(path, exist_ok=True)
    return path


def _fingerprint(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def run_path(source_path, output_folder, payer_name):
    identity = '\0'.join((
        os.path.normcase(os.path.abspath(source_path)),
        os.path.normcase(os.path.abspath(output_folder)), payer_name,
    ))
    return os.path.join(
        _database_dir(), hashlib.sha256(identity.encode('utf-8')).hexdigest() + '.sqlite',
    )


def _connect(path):
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA busy_timeout=30000')
    return db


def _sheet_rows(path):
    if not path.lower().endswith('.xlsx'):
        raise ValueError('Only .xlsx collection workbooks are supported.')
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook.worksheets[0]
        iterator = sheet.iter_rows(values_only=True)
        raw_headers = next(iterator, None)
        if raw_headers is None:
            raise ValueError('The collection workbook has no header row.')
        headers = [str(value).strip() if value is not None else f'Unnamed: {i}'
                   for i, value in enumerate(raw_headers)]
        if len(headers) != len(set(headers)):
            raise ValueError('The collection workbook has duplicate column headers.')
        yield sheet.title, headers, iterator
    finally:
        workbook.close()


def _schema(db):
    db.executescript('''
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE rows (
            pos INTEGER PRIMARY KEY, source_row INTEGER NOT NULL,
            invoice TEXT NOT NULL, member TEXT NOT NULL,
            visit_iso TEXT NOT NULL, patient_key TEXT NOT NULL,
            status TEXT NOT NULL, eligible INTEGER NOT NULL,
            data TEXT NOT NULL
        );
        CREATE INDEX rows_work ON rows(eligible, pos);
        CREATE INDEX rows_invoice ON rows(invoice, pos);
        CREATE INDEX rows_patient ON rows(patient_key, visit_iso);
    ''')


def _meta(db, key, value):
    db.execute('INSERT INTO meta VALUES (?, ?)', (key, json.dumps(value)))


def _row_record(pos, data, source_row=None):
    from availity_app.search_grouping import patient_identity_key
    from availity_app.utils import safe_field
    visit = normalize_search_date(data.get('VisitDate'))
    try:
        visit_iso = datetime.strptime(visit, '%m/%d/%Y').date().isoformat() if visit else ''
    except ValueError:
        visit_iso = ''
    return (
        pos, source_row if source_row is not None else pos + 2,
        safe_invoice_number(data, 'InvoiceNumber', ''),
        safe_alt_patient_id(data, 'AltPatientID'), visit_iso,
        json.dumps(patient_identity_key(data, safe_field)),
        str(data.get('AutomationStatus') or 'Pending'), _eligible(data),
        json.dumps(data, ensure_ascii=False, default=str),
    )


def open_or_import(source_path, output_folder, payer_name, payer_config=None,
                   progress=None):
    """Return a complete run path. Incomplete imports never replace a run."""
    source_path = os.path.abspath(source_path)
    output_folder = os.path.abspath(output_folder)
    source_hash = _fingerprint(source_path)
    destination = run_path(source_path, output_folder, payer_name)
    if os.path.exists(destination):
        with closing(_connect(destination)) as db:
            saved = db.execute("SELECT value FROM meta WHERE key='source_hash'").fetchone()
            if saved and json.loads(saved[0]) == source_hash:
                return destination
        if os.path.basename(source_path).lower() == 'automated.xlsx':
            import_recheck_edits(destination, source_path)
            with closing(_connect(destination)) as db, db:
                db.execute('UPDATE meta SET value=? WHERE key=?',
                           (json.dumps(source_hash), 'source_hash'))
            return destination
        raise ValueError('The selected collection file changed since this run was imported. Choose a new output folder or restore the original file.')

    fd, temporary = tempfile.mkstemp(suffix='.sqlite', dir=_database_dir())
    os.close(fd)
    try:
        with closing(_connect(temporary)) as db, db:
            _schema(db)
            generator = _sheet_rows(source_path)
            sheet_name, headers, iterator = next(generator)
            empty = pd.DataFrame(columns=headers)
            missing = validate_dataframe(empty, payer_config)
            if missing:
                raise ValueError(f"Missing columns: {', '.join(missing)}")
            from availity_app.automation import (
                add_output_columns, apply_claim_not_found_completed_rows,
                apply_in_billing_review_rows, mark_recheck_skipped_rows,
                queue_error_rows_for_retry, queue_recheck_yes_rows,
            )
            from availity_app.excel_export import prepare_export_df
            from availity_app.utils import normalize_invoice_column
            columns = None
            export_columns = None
            total = 0
            recheck_yes = 0
            recheck_empty = 0
            source_row = 1
            try:
                while True:
                    batch = []
                    end_of_sheet = False
                    for _ in range(1000):
                        raw = next(iterator, None)
                        if raw is None:
                            end_of_sheet = True
                            break
                        source_row += 1
                        if not any(value is not None for value in raw):
                            continue
                        batch.append((source_row, [_cell(v) for v in raw[:len(headers)]]))
                    if not batch:
                        if end_of_sheet:
                            break
                        continue
                    frame = pd.DataFrame((values for _, values in batch), columns=headers)
                    if 'PatientName' in frame:
                        parts = frame['PatientName'].astype(str).str.split(',', n=1, expand=True)
                        frame['Last_Name'] = parts[0].str.strip()
                        frame['First_Name'] = parts[1].str.strip() if len(parts.columns) > 1 else ''
                    frame = normalize_invoice_column(frame)
                    frame = add_output_columns(frame, payer_config)
                    frame = apply_in_billing_review_rows(frame)
                    frame = mark_recheck_skipped_rows(frame)
                    frame = apply_claim_not_found_completed_rows(frame)
                    frame = queue_recheck_yes_rows(frame)
                    frame = queue_error_rows_for_retry(frame)
                    if columns is None:
                        columns = list(frame.columns)
                        export_columns = list(prepare_export_df(frame).columns)
                    records = []
                    for (excel_row, _), row in zip(batch, frame.to_dict(orient='records')):
                        data = {key: _cell(value) for key, value in row.items()}
                        records.append(_row_record(total, data, excel_row))
                        recheck = str(data.get('Recheck') or '').strip().lower()
                        recheck_yes += recheck == 'yes'
                        recheck_empty += recheck == ''
                        total += 1
                    db.executemany('INSERT INTO rows VALUES (?,?,?,?,?,?,?,?,?)', records)
                    db.commit()
                    if progress and total % 20_000 == 0:
                        progress(total)
            finally:
                generator.close()
            if columns is None:
                columns = list(add_output_columns(empty, payer_config).columns)
                export_columns = list(prepare_export_df(pd.DataFrame(columns=columns)).columns)
            _meta(db, 'source_hash', source_hash)
            _meta(db, 'source_path', source_path)
            _meta(db, 'output_folder', output_folder)
            _meta(db, 'payer', payer_name)
            _meta(db, 'sheet_name', sheet_name)
            _meta(db, 'columns', columns)
            _meta(db, 'export_columns', export_columns)
            _meta(db, 'stats', {'total': total, 'recheck_yes': recheck_yes,
                                'recheck_empty': recheck_empty})
            prior = os.path.join(output_folder, 'Automated.xlsx')
            if os.path.normcase(prior) != os.path.normcase(source_path) and os.path.isfile(prior):
                _import_prior_results(db, prior, payer_config)
            _import_prior_wal(db, output_folder, payer_config)
        if _fingerprint(source_path) != source_hash:
            raise ValueError('The collection file changed during import. Please import it again.')
        with closing(_connect(temporary)) as ready:
            ready.execute('PRAGMA journal_mode=WAL')
            ready.execute('PRAGMA synchronous=FULL')
        os.replace(temporary, destination)
        return destination
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def _import_prior_results(db, path, payer_config=None):
    generator = _sheet_rows(path)
    _, headers, iterator = next(generator)
    output_fields = set(resolved_output_columns(payer_config)) | set(STATUS_COLUMNS)
    try:
        count = 0
        for pos, raw in enumerate(iterator):
            record = db.execute('SELECT data FROM rows WHERE pos=?', (pos,)).fetchone()
            if record is None:
                raise ValueError('Existing Automated.xlsx has more rows than the collection file.')
            original = json.loads(record[0])
            previous = dict(zip(headers, (_cell(v) for v in raw)))
            if _identity(original) != _identity(previous):
                raise ValueError(f'Existing Automated.xlsx differs from the collection file at row {pos + 2}.')
            for field in output_fields:
                if field in previous:
                    original[field] = previous[field]
            _update(db, pos, original)
            count += 1
        expected = db.execute('SELECT count(*) FROM rows').fetchone()[0]
        if count != expected:
            raise ValueError('Existing Automated.xlsx has fewer rows than the collection file.')
    finally:
        generator.close()


def _import_prior_wal(db, output_folder, payer_config=None):
    """Replay the previous app's newer JSONL progress after its Excel snapshot."""
    from availity_app.progress_wal import wal_path
    path = wal_path(output_folder)
    if not os.path.isfile(path):
        return
    allowed = set(resolved_output_columns(payer_config)) | set(STATUS_COLUMNS)
    with open(path, encoding='utf-8') as handle:
        for line_number, raw in enumerate(handle, 1):
            if not raw.strip():
                continue
            try:
                record = json.loads(raw)
                identity = (str(record.get('invoice') or ''),
                            str(record.get('visit') or ''),
                            str(record.get('member') or ''))
                fields = record['fields']
                if not isinstance(fields, dict):
                    raise ValueError('fields must be an object')
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                raise ValueError(f'Legacy progress log line {line_number} is invalid: {exc}') from exc
            pos = record.get('idx')
            try:
                pos = int(pos)
            except (TypeError, ValueError):
                pos = -1
            found = db.execute('SELECT data FROM rows WHERE pos=?', (pos,)).fetchone()
            if found is None or _identity(json.loads(found[0])) != identity:
                candidates = []
                for candidate in db.execute('SELECT pos, data FROM rows WHERE invoice=? AND member=?',
                                            (identity[0], identity[2])):
                    if _identity(json.loads(candidate['data'])) == identity:
                        candidates.append(candidate)
                if len(candidates) != 1:
                    raise ValueError(f'Legacy progress log line {line_number} does not identify one collection row.')
                pos = candidates[0]['pos']
                found = candidates[0]
            data = json.loads(found['data'])
            for field, value in fields.items():
                if field in allowed:
                    data[field] = _cell(value)
            _update(db, pos, data)


def _update(db, pos, data):
    record = _row_record(pos, data)
    db.execute('''UPDATE rows SET invoice=?, member=?, visit_iso=?, patient_key=?,
                  status=?, eligible=?, data=? WHERE pos=?''', record[2:] + (pos,))


class _Indexer:
    def __init__(self, store, cell=False):
        self.store = store
        self.cell = cell

    def __getitem__(self, key):
        if self.cell:
            pos, column = key
            return self.store.row(pos).get(column, '')
        return pd.Series(self.store.row(key))

    def __setitem__(self, key, value):
        pos, column = key
        self.store.set(pos, column, value)


class RunStore:
    """A small row view for existing claim helpers; no full-sheet materialization."""

    is_run_store = True

    def __init__(self, path):
        self.path = path
        self.db = _connect(path)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.meta = {r['key']: json.loads(r['value'])
                     for r in self.db.execute('SELECT key, value FROM meta')}
        self.columns = self.meta['columns']
        self.attrs = {}
        self.at = _Indexer(self, cell=True)
        self.iloc = _Indexer(self)
        self.cache = OrderedDict()
        self.patient_cache = OrderedDict()
        self.dirty = set()
        self.count = self.db.execute('SELECT count(*) FROM rows').fetchone()[0]

    def __len__(self):
        return self.count

    def close(self):
        self.commit()
        self.db.close()

    def row(self, pos):
        if pos in self.cache:
            self.cache.move_to_end(pos)
            return self.cache[pos]
        record = self.db.execute('SELECT data FROM rows WHERE pos=?', (int(pos),)).fetchone()
        if record is None:
            raise IndexError(pos)
        data = json.loads(record[0])
        self.cache[pos] = data
        if len(self.cache) > 1024:
            for old in list(self.cache):
                if old not in self.dirty:
                    del self.cache[old]
                    break
        return data

    def set(self, pos, column, value):
        self.row(pos)[column] = _cell(value)
        self.dirty.add(pos)

    def commit(self):
        if not self.dirty:
            return
        with self.db:
            for pos in sorted(self.dirty):
                _update(self.db, pos, self.row(pos))
        self.dirty.clear()
        while len(self.cache) > 1024:
            self.cache.popitem(last=False)

    def recover_in_progress(self):
        for rec in self.db.execute("SELECT pos FROM rows WHERE status='InProgress'"):
            self.set(rec['pos'], 'AutomationStatus', 'Pending')
        self.commit()

    def pending_indices(self, limit):
        self.commit()
        return [r['pos'] for r in self.db.execute(
            'SELECT pos FROM rows WHERE eligible=1 ORDER BY pos LIMIT ?',
            (-1 if limit is None else limit,),
        )]

    def invoice_indices(self, invoice):
        target = format_invoice_number(invoice, '')
        return [r['pos'] for r in self.db.execute(
            'SELECT pos FROM rows WHERE invoice=? ORDER BY pos', (target,),
        )]

    def invoice_date_range(self, invoice):
        target = format_invoice_number(invoice, '')
        rec = self.db.execute("""SELECT min(visit_iso) AS first, max(visit_iso) AS last
                                 FROM rows WHERE invoice=? AND visit_iso<>''""", (target,)).fetchone()
        if not rec['first']:
            return None
        first = datetime.fromisoformat(rec['first']).strftime('%m/%d/%Y')
        last = datetime.fromisoformat(rec['last']).strftime('%m/%d/%Y')
        return f'{first}-{last}'

    def patient_dates(self, patient_key):
        if patient_key in self.patient_cache:
            self.patient_cache.move_to_end(patient_key)
            return self.patient_cache[patient_key]
        dates = [datetime.fromisoformat(r['visit_iso']) for r in self.db.execute(
            "SELECT DISTINCT visit_iso FROM rows WHERE patient_key=? AND visit_iso<>'' ORDER BY visit_iso",
            (patient_key,),
        )]
        self.patient_cache[patient_key] = dates
        if len(self.patient_cache) > 128:
            self.patient_cache.popitem(last=False)
        return dates

def import_recheck_edits(path, workbook_path):
    """Import only Recheck cells from an identity-matched exported workbook."""
    store = RunStore(path)
    generator = _sheet_rows(workbook_path)
    try:
        _, headers, iterator = next(generator)
        if 'Recheck' not in headers:
            raise ValueError('The selected workbook has no Recheck column.')
        changes = []
        count = 0
        yes_count = 0
        empty_count = 0
        for pos, raw in enumerate(iterator):
            if pos >= len(store):
                raise ValueError('The selected workbook has extra rows.')
            candidate = dict(zip(headers, (_cell(v) for v in raw)))
            if _identity(store.row(pos)) != _identity(candidate):
                raise ValueError(f'The selected workbook differs at row {pos + 2}.')
            value = str(candidate.get('Recheck') or '').strip().lower()
            if value not in ('', 'yes', 'no'):
                raise ValueError(f'Invalid Recheck value at row {pos + 2}.')
            yes_count += value == 'yes'
            empty_count += value == ''
            if value != str(store.row(pos).get('Recheck') or '').strip().lower():
                changes.append((pos, value))
            count += 1
        if count != len(store):
            raise ValueError('The selected workbook has too few rows.')
        for row_pos, value in changes:
            store.set(row_pos, 'Recheck', value)
            if value == 'yes':
                store.set(row_pos, 'AutomationStatus', 'Pending')
        store.meta['stats'].update(recheck_yes=yes_count, recheck_empty=empty_count)
        try:
            with store.db:
                for row_pos in sorted(store.dirty):
                    _update(store.db, row_pos, store.row(row_pos))
                store.db.execute('UPDATE meta SET value=? WHERE key=?',
                                 (json.dumps(store.meta['stats']), 'stats'))
        finally:
            store.dirty.clear()
        return len(changes)
    finally:
        generator.close()
        store.close()
