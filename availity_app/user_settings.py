"""Persist GUI settings and overlay them onto payer URLs and config."""
import json
import os
import re
import sys
import threading
from copy import deepcopy
from urllib.parse import unquote

from availity_app.constants import (
    DEFAULT_SELECTOR_PROFILE,
    LINE_LEVEL_CONFIG,
    NPI,
    PAYER_CONFIG,
    REMIT_CHECK_DATE_GRACE_DAYS,
    get_default_gui_payer,
    get_gui_payer_names,
)

_SETTINGS_VERSION = 1
_SETTINGS_FILENAME = 'settings.json'
_ID_PATTERN = re.compile(r'^[A-Za-z0-9_-]+$')
_NPI_PATTERN = re.compile(r'^\d{10}$')
_ORG_ENCODED_RE = re.compile(r'(orgId%3D)([^%&]*)', re.IGNORECASE)
_ORG_PLAIN_RE = re.compile(r'(orgId=)([^&]*)', re.IGNORECASE)
_PAYER_ENCODED_RE = re.compile(r'(payerId%3D)([^%&]*)', re.IGNORECASE)
_PAYER_PLAIN_RE = re.compile(r'(payerId=)([^&]*)', re.IGNORECASE)

SELECTOR_PROFILES_ALLOWED = ('mui', 'legacy', 'aetna')
MATCH_MODES_ALLOWED = ('exact', 'within_range', 'contains')
SEARCH_MISS_OUTCOMES = ('not_found', 'invalid_member_id')
SEARCH_MISS_OUTCOME_LABELS = {
    'not_found': 'Claim Not Found',
    'invalid_member_id': 'Invalid Member ID',
}
_MAX_SEARCH_MISS_PHRASES = 30
_MAX_SEARCH_MISS_PHRASE_LEN = 200
PAYER_BOOL_FLAGS = (
    'uses_hipaa_tab',
    'uses_line_level',
    'uses_remittance',
    'remittance_when_remarks_blank',
    'skip_remittance_when_unposted',
    'uses_results_finalized_date',
    'reset_via_edit_search',
    'read_hipaa_codes',
    'canned_paid_denial',
    'detect_adjustment_pairs',
    'denial_description_only',
    'trim_denial_to_first_sentence',
    'match_results_by_visit_and_amount',
    'supports_search_grouping',
    'group_search_by_patient',
    'uses_remittance_billed',
    'reconcile_finalized_date_conflicts',
    'filter_remit_disclaimer_text',
)
LINE_LEVEL_KEYS = (
    'match_mode',
    'filter_by_visit_date',
    'fallback_to_sole_line',
    'filter_results_by_visit_date',
    'match_by_invoice_alignment',
    'enable_pool_tiers',
    'allow_split_tiebreak',
    'fallback_within_range',
    'range_match_by_day_position',
    'max_combo',
)
# Line-level keys holding a number rather than a bool or a match mode.
LINE_LEVEL_INT_KEYS = ('max_combo',)
# Extraction-reliability knobs, stored under a payer's ``result_validation``.
# Only payers that already declare the block get one; the GUI never adds it to
# a payer that does not, so other payers keep their single-read behavior.
RESULT_VALIDATION_KEYS = (
    'field_read_attempts',
    'field_retry_delay_s',
    'detail_identity_timeout_ms',
    'remittance_attempts',
    'remittance_retry_delay_s',
)
RESULT_VALIDATION_INT_KEYS = (
    'field_read_attempts',
    'detail_identity_timeout_ms',
    'remittance_attempts',
)

# Fallbacks matching automation.py / resilience.py when JSON has no override.
_DEFAULT_OUTPUT_FORMATTING = True
_DEFAULT_UPPERCASE_EXPORT = False
_DEFAULT_SESSION_ERROR_LIMIT = 3
_DEFAULT_TRANSIENT_ERROR_LIMIT = 5
_DEFAULT_MEDIUM_FILE_SAVE_INTERVAL = 10
_DEFAULT_LARGE_FILE_SAVE_INTERVAL = 100

_lock = threading.Lock()
_settings_path_override = None
_last_load_error = None


def set_settings_path_for_tests(path):
    """Point persistence at ``path`` (tests only). Pass None to restore default."""
    global _settings_path_override, _last_load_error
    _settings_path_override = path
    _last_load_error = None


def app_install_dir():
    """Return the folder that contains the app (exe directory or project root)."""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def settings_file_path():
    """Return the JSON path used for saved OrgID / Payer ID overrides."""
    if _settings_path_override:
        return _settings_path_override
    return os.path.join(app_install_dir(), _SETTINGS_FILENAME)


def settings_load_error():
    """Return the last settings.json load error, or None when the last load succeeded."""
    return _last_load_error


def clear_settings_load_error():
    """Clear a recorded load error after the GUI has shown it."""
    global _last_load_error
    _last_load_error = None


def _empty_settings():
    return {'version': _SETTINGS_VERSION, 'global': {}, 'payers': {}}


def normalize_id(value):
    """Strip whitespace from an OrgID or Payer ID string."""
    return str(value or '').strip()


def is_valid_id(value):
    """True when ``value`` is a non-empty OrgID / Payer ID token."""
    text = normalize_id(value)
    return bool(text) and bool(_ID_PATTERN.fullmatch(text))


def is_valid_npi(value):
    """True when ``value`` is a 10-digit Provider NPI."""
    return bool(_NPI_PATTERN.fullmatch(normalize_id(value)))


def parse_org_payer_ids(url):
    """Return ``(org_id, payer_id)`` parsed from a payer claim-search URL."""
    text = url or ''
    org_id = _match_id(_ORG_ENCODED_RE, text) or _match_id(_ORG_PLAIN_RE, text)
    payer_id = _match_id(_PAYER_ENCODED_RE, text) or _match_id(_PAYER_PLAIN_RE, text)
    return org_id, payer_id


def default_org_payer_ids(payer_name):
    """Return hardcoded OrgID / Payer ID for ``payer_name`` from PAYER_CONFIG."""
    cfg = PAYER_CONFIG.get(payer_name) or {}
    org_id = normalize_id(cfg.get('org_id'))
    payer_id = normalize_id(cfg.get('payer_id'))
    if org_id or payer_id:
        return org_id, payer_id
    return parse_org_payer_ids(cfg.get('url', ''))


def apply_org_payer_ids(url, org_id=None, payer_id=None):
    """Return ``url`` with OrgID and/or Payer ID replaced when provided."""
    result = url or ''
    org_text = normalize_id(org_id)
    payer_text = normalize_id(payer_id)
    if org_text:
        if '{orgId}' in result:
            result = result.replace('{orgId}', _encode_id(org_text))
        else:
            result = _replace_id(result, _ORG_ENCODED_RE, _ORG_PLAIN_RE, org_text)
    if payer_text:
        if '{payerId}' in result:
            result = result.replace('{payerId}', _encode_id(payer_text))
        else:
            result = _replace_id(result, _PAYER_ENCODED_RE, _PAYER_PLAIN_RE, payer_text)
    return result


def load_settings():
    """Return the settings document, or an empty default structure."""
    global _last_load_error
    path = settings_file_path()
    if not path or not os.path.isfile(path):
        _last_load_error = None
        return _empty_settings()
    try:
        with open(path, encoding='utf-8') as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            _last_load_error = (
                f'{path} is not a valid settings file. Using built-in defaults.'
            )
            return _empty_settings()
        data.setdefault('payers', {})
        if not isinstance(data['payers'], dict):
            data['payers'] = {}
        data.setdefault('global', {})
        if not isinstance(data['global'], dict):
            data['global'] = {}
        _last_load_error = None
        return data
    except (OSError, json.JSONDecodeError) as e:
        _last_load_error = (
            f'{path} could not be read ({e}). Using built-in defaults.'
        )
        return _empty_settings()


def load_payer_entry(payer_name):
    """Return the raw saved dict for ``payer_name``, or empty dict."""
    if not payer_name:
        return {}
    with _lock:
        entry = load_settings().get('payers', {}).get(payer_name) or {}
    return dict(entry) if isinstance(entry, dict) else {}


def load_payer_ids(payer_name):
    """Return saved ``{'org_id', 'payer_id'}`` for a payer, omitting blanks."""
    entry = load_payer_entry(payer_name)
    org_id = normalize_id(entry.get('org_id'))
    payer_id = normalize_id(entry.get('payer_id'))
    result = {}
    if org_id:
        result['org_id'] = org_id
    if payer_id:
        result['payer_id'] = payer_id
    return result


def ids_for_payer(payer_name):
    """Return effective ``(org_id, payer_id)`` — saved override, else URL default."""
    default_org, default_payer = default_org_payer_ids(payer_name)
    saved = load_payer_ids(payer_name)
    return saved.get('org_id') or default_org, saved.get('payer_id') or default_payer


def save_payer_ids(payer_name, org_id, payer_id):
    """Merge OrgID and Payer ID into the saved entry for ``payer_name``."""
    if not payer_name:
        raise ValueError('Select a payer before saving OrgID and Payer ID.')
    org_text = normalize_id(org_id)
    payer_text = normalize_id(payer_id)
    if not org_text:
        raise ValueError('Please enter an OrgID.')
    if not payer_text:
        raise ValueError('Please enter a Payer ID.')
    if not is_valid_id(org_text):
        raise ValueError(
            'OrgID can only contain letters, numbers, hyphens, and underscores.'
        )
    if not is_valid_id(payer_text):
        raise ValueError(
            'Payer ID can only contain letters, numbers, hyphens, and underscores.'
        )
    with _lock:
        data = load_settings()
        payers = data.setdefault('payers', {})
        entry = dict(payers.get(payer_name) or {})
        if not isinstance(entry, dict):
            entry = {}
        entry['org_id'] = org_text
        entry['payer_id'] = payer_text
        payers[payer_name] = entry
        _write_settings(data)
    return org_text, payer_text


def clear_payer_ids(payer_name):
    """Remove a saved payer override so hardcoded PAYER_CONFIG values are used."""
    if not payer_name:
        return default_org_payer_ids(payer_name)
    with _lock:
        data = load_settings()
        payers = data.setdefault('payers', {})
        payers.pop(payer_name, None)
        _write_settings(data)
    return default_org_payer_ids(payer_name)


def save_all_settings(global_values, payer_name, payer_values):
    """Validate and persist global settings plus one payer's overrides."""
    sanitized_global = _sanitize_global_values(global_values or {})
    if not payer_name:
        raise ValueError('Select a payer before saving settings.')
    sanitized_payer = _sanitize_payer_values(payer_values or {})
    with _lock:
        data = load_settings()
        merged_global = dict(data.get('global') or {})
        merged_global.update(sanitized_global)
        data['global'] = merged_global
        payers = data.setdefault('payers', {})
        entry = dict(payers.get(payer_name) or {})
        if not isinstance(entry, dict):
            entry = {}
        entry.update(sanitized_payer)
        if sanitized_payer.get('npi_blank'):
            entry.pop('npi', None)
        payers[payer_name] = entry
        _write_settings(data)
    return sanitized_global, sanitized_payer


def resolve_payer_config(payer_name):
    """Return a copy of PAYER_CONFIG with saved settings overlaid."""
    base = PAYER_CONFIG.get(payer_name)
    if not base:
        return None
    cfg = deepcopy(base)
    org_id, payer_id = ids_for_payer(payer_name)
    cfg['url'] = apply_org_payer_ids(cfg.get('url', ''), org_id, payer_id)
    saved = load_payer_entry(payer_name)
    if saved.get('npi_blank'):
        # Use the global default NPI rather than a payer-specific override.
        cfg.pop('npi', None)
    elif normalize_id(saved.get('npi')):
        cfg['npi'] = normalize_id(saved.get('npi'))
    for flag in PAYER_BOOL_FLAGS:
        if flag in saved:
            cfg[flag] = bool(saved[flag])
    profile = saved.get('selector_profile')
    if profile in SELECTOR_PROFILES_ALLOWED:
        cfg['selector_profile'] = profile
    saved_ll = saved.get('line_level')
    if isinstance(saved_ll, dict) and saved_ll:
        merged = dict(cfg.get('line_level') or {})
        for key in LINE_LEVEL_KEYS:
            if key not in saved_ll:
                continue
            if key == 'match_mode':
                mode = saved_ll[key]
                if mode in MATCH_MODES_ALLOWED:
                    merged[key] = mode
            elif key in LINE_LEVEL_INT_KEYS:
                try:
                    merged[key] = int(saved_ll[key])
                except (TypeError, ValueError):
                    pass
            else:
                merged[key] = bool(saved_ll[key])
        cfg['line_level'] = merged
    saved_rv = saved.get('result_validation')
    # Only overlay onto payers that ship a result_validation block, so a stale
    # settings file cannot switch strict validation on for another payer.
    if isinstance(saved_rv, dict) and saved_rv and cfg.get('result_validation'):
        merged_rv = dict(cfg['result_validation'])
        for key in RESULT_VALIDATION_KEYS:
            if key not in saved_rv:
                continue
            try:
                merged_rv[key] = (
                    int(saved_rv[key]) if key in RESULT_VALIDATION_INT_KEYS
                    else float(saved_rv[key])
                )
            except (TypeError, ValueError):
                pass
        cfg['result_validation'] = merged_rv
    return cfg


def default_payer_form_values(payer_name):
    """Return GUI field values for ``payer_name`` from PAYER_CONFIG (no overlay)."""
    cfg = PAYER_CONFIG.get(payer_name) or {}
    org_id, payer_id = default_org_payer_ids(payer_name)
    line_level = dict(LINE_LEVEL_CONFIG)
    line_level.update(cfg.get('line_level') or {})
    override = cfg.get('npi')
    npi_blank = override is False or not override
    if npi_blank:
        npi = ''
    else:
        npi = str(override).strip()
    values = {
        'org_id': org_id,
        'payer_id': payer_id,
        'npi': npi,
        'npi_blank': npi_blank,
        'selector_profile': cfg.get('selector_profile', DEFAULT_SELECTOR_PROFILE),
        'supports_search_grouping': cfg.get('supports_search_grouping', True),
        'match_mode': line_level.get('match_mode', 'exact'),
        'filter_by_visit_date': bool(line_level.get('filter_by_visit_date', True)),
        'fallback_to_sole_line': bool(line_level.get('fallback_to_sole_line', True)),
        'filter_results_by_visit_date': bool(
            line_level.get('filter_results_by_visit_date')
        ),
        'match_by_invoice_alignment': bool(
            line_level.get('match_by_invoice_alignment')
        ),
        'enable_pool_tiers': bool(line_level.get('enable_pool_tiers')),
        'allow_split_tiebreak': bool(line_level.get('allow_split_tiebreak')),
        'fallback_within_range': bool(line_level.get('fallback_within_range')),
        'range_match_by_day_position': bool(
            line_level.get('range_match_by_day_position')
        ),
        'max_combo': str(line_level.get('max_combo', 3)),
    }
    for flag in PAYER_BOOL_FLAGS:
        if flag == 'supports_search_grouping':
            continue
        values[flag] = bool(cfg.get(flag, False))
    validation = cfg.get('result_validation') or {}
    values['has_result_validation'] = bool(validation)
    for key in RESULT_VALIDATION_KEYS:
        values[key] = (
            '' if not validation else str(validation.get(key, ''))
        )
    return values


def payer_form_values(payer_name):
    """Return GUI field values with saved overrides applied."""
    values = default_payer_form_values(payer_name)
    saved = load_payer_entry(payer_name)
    if not saved:
        return values
    org_id, payer_id = ids_for_payer(payer_name)
    values['org_id'] = org_id
    values['payer_id'] = payer_id
    if saved.get('npi_blank'):
        values['npi_blank'] = True
        values['npi'] = ''
    elif normalize_id(saved.get('npi')):
        values['npi_blank'] = False
        values['npi'] = normalize_id(saved.get('npi'))
    for flag in PAYER_BOOL_FLAGS:
        if flag in saved:
            values[flag] = bool(saved[flag])
    profile = saved.get('selector_profile')
    if profile in SELECTOR_PROFILES_ALLOWED:
        values['selector_profile'] = profile
    saved_ll = saved.get('line_level') if isinstance(saved.get('line_level'), dict) else {}
    if saved_ll.get('match_mode') in MATCH_MODES_ALLOWED:
        values['match_mode'] = saved_ll['match_mode']
    for key in LINE_LEVEL_KEYS:
        if key == 'match_mode' or key not in saved_ll:
            continue
        if key in LINE_LEVEL_INT_KEYS:
            try:
                values[key] = str(int(saved_ll[key]))
            except (TypeError, ValueError):
                pass
        else:
            values[key] = bool(saved_ll[key])
    saved_rv = (
        saved.get('result_validation')
        if isinstance(saved.get('result_validation'), dict) else {}
    )
    if values.get('has_result_validation'):
        for key in RESULT_VALIDATION_KEYS:
            if key in saved_rv:
                values[key] = str(saved_rv[key])
    return values


def global_form_values():
    """Return GUI field values for global settings, filling code defaults."""
    return {
        'default_payer': get_startup_payer(),
        'default_npi': get_default_npi(),
        'remit_check_date_grace_days': get_remit_check_date_grace_days(),
        'output_formatting_enabled': get_output_formatting_enabled(),
        'uppercase_export_enabled': get_uppercase_export_enabled(),
        'session_error_limit': get_session_error_limit(),
        'transient_error_limit': get_transient_error_limit(),
        'medium_file_save_interval': get_medium_file_save_interval(),
        'large_file_save_interval': get_large_file_save_interval(),
        'search_miss_phrases': get_search_miss_phrases(),
    }


def get_startup_payer():
    """Return saved default payer if it is GUI-enabled, else first GUI payer."""
    saved = normalize_id(_global_get('default_payer'))
    names = get_gui_payer_names()
    if saved in names:
        return saved
    return get_default_gui_payer()


def get_default_npi():
    """Return saved default Provider NPI, or the hardcoded NPI constant."""
    saved = normalize_id(_global_get('default_npi'))
    return saved or NPI


def get_remit_check_date_grace_days():
    """Return saved remit grace days, or REMIT_CHECK_DATE_GRACE_DAYS."""
    return _global_int(
        'remit_check_date_grace_days',
        REMIT_CHECK_DATE_GRACE_DAYS,
        minimum=0,
    )


def get_output_formatting_enabled():
    """Return whether formatted Excel output is enabled."""
    value = _global_get('output_formatting_enabled')
    if value is None:
        return _DEFAULT_OUTPUT_FORMATTING
    return bool(value)


def get_uppercase_export_enabled():
    """Return whether Excel cell text is written in ALL CAPS.

    Global for every payer. Off unless the Settings checkbox is saved on.
    """
    value = _global_get('uppercase_export_enabled')
    if value is None:
        return _DEFAULT_UPPERCASE_EXPORT
    return bool(value)


def get_session_error_limit():
    """Return consecutive session failures that trip the circuit breaker."""
    return _global_int(
        'session_error_limit', _DEFAULT_SESSION_ERROR_LIMIT, minimum=1,
    )


def get_transient_error_limit():
    """Return consecutive transient failures that trip the circuit breaker."""
    return _global_int(
        'transient_error_limit', _DEFAULT_TRANSIENT_ERROR_LIMIT, minimum=1,
    )


def get_medium_file_save_interval():
    """Return mid-batch save row interval for medium files."""
    return _global_int(
        'medium_file_save_interval',
        _DEFAULT_MEDIUM_FILE_SAVE_INTERVAL,
        minimum=1,
    )


def get_large_file_save_interval():
    """Return mid-batch save row interval for large files."""
    return _global_int(
        'large_file_save_interval',
        _DEFAULT_LARGE_FILE_SAVE_INTERVAL,
        minimum=1,
    )


def get_search_miss_phrases():
    """Return global alert-phrase misses that apply to every Availity payer."""
    raw = _global_get('search_miss_phrases')
    try:
        return sanitize_search_miss_phrases(raw)
    except ValueError:
        return []


def search_miss_outcome_key(value):
    """Map a stored outcome or GUI label to ``not_found`` / ``invalid_member_id``."""
    text = str(value or '').strip()
    if text in SEARCH_MISS_OUTCOMES:
        return text
    for key, label in SEARCH_MISS_OUTCOME_LABELS.items():
        if text == label:
            return key
    return ''


def sanitize_search_miss_phrases(raw):
    """Validate GUI/JSON miss phrases. Empty list is allowed."""
    if raw in (None, '', []):
        return []
    if not isinstance(raw, list):
        raise ValueError('Search miss phrases must be a list of phrase/outcome rows.')
    seen = set()
    result = []
    for item in raw:
        if isinstance(item, str):
            phrase = ' '.join(item.split())
            outcome = 'not_found'
        elif isinstance(item, dict):
            phrase = ' '.join(str(item.get('phrase') or '').split())
            outcome = search_miss_outcome_key(item.get('outcome')) or 'not_found'
        else:
            continue
        if not phrase:
            continue
        if len(phrase) > _MAX_SEARCH_MISS_PHRASE_LEN:
            raise ValueError(
                'Search miss phrase is too long '
                f'(max {_MAX_SEARCH_MISS_PHRASE_LEN} characters).'
            )
        if outcome not in SEARCH_MISS_OUTCOMES:
            raise ValueError(
                'Search miss outcome must be Claim Not Found or Invalid Member ID.'
            )
        key = phrase.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append({'phrase': phrase, 'outcome': outcome})
        if len(result) > _MAX_SEARCH_MISS_PHRASES:
            raise ValueError(
                f'At most {_MAX_SEARCH_MISS_PHRASES} search miss phrases.'
            )
    return result


def _global_get(key):
    with _lock:
        data = load_settings()
    section = data.get('global') or {}
    if not isinstance(section, dict):
        return None
    return section.get(key)


def _global_int(key, default, minimum=0):
    value = _global_get(key)
    if value is None or value == '':
        return default
    try:
        resolved = int(value)
    except (TypeError, ValueError):
        return default
    return resolved if resolved >= minimum else default


def _sanitize_global_values(values):
    default_payer = normalize_id(values.get('default_payer'))
    names = get_gui_payer_names()
    if default_payer and default_payer not in names:
        raise ValueError('Please select a valid default payer.')
    default_npi = normalize_id(values.get('default_npi'))
    if not is_valid_npi(default_npi):
        raise ValueError('Default Provider NPI must be 10 digits.')
    grace = _require_int(
        values.get('remit_check_date_grace_days'),
        'Remit check-date grace days',
        minimum=0,
    )
    session_limit = _require_int(
        values.get('session_error_limit'), 'Session error limit', minimum=1,
    )
    transient_limit = _require_int(
        values.get('transient_error_limit'), 'Transient error limit', minimum=1,
    )
    medium_interval = _require_int(
        values.get('medium_file_save_interval'),
        'Medium file save interval',
        minimum=1,
    )
    large_interval = _require_int(
        values.get('large_file_save_interval'),
        'Large file save interval',
        minimum=1,
    )
    return {
        'default_payer': default_payer or get_default_gui_payer(),
        'default_npi': default_npi,
        'remit_check_date_grace_days': grace,
        'output_formatting_enabled': bool(values.get('output_formatting_enabled')),
        'uppercase_export_enabled': bool(values.get('uppercase_export_enabled')),
        'session_error_limit': session_limit,
        'transient_error_limit': transient_limit,
        'medium_file_save_interval': medium_interval,
        'large_file_save_interval': large_interval,
        'search_miss_phrases': sanitize_search_miss_phrases(
            values.get('search_miss_phrases'),
        ),
    }


def _sanitize_payer_values(values):
    org_text = normalize_id(values.get('org_id'))
    payer_text = normalize_id(values.get('payer_id'))
    if not org_text:
        raise ValueError('Please enter an OrgID.')
    if not payer_text:
        raise ValueError('Please enter a Payer ID.')
    if not is_valid_id(org_text):
        raise ValueError(
            'OrgID can only contain letters, numbers, hyphens, and underscores.'
        )
    if not is_valid_id(payer_text):
        raise ValueError(
            'Payer ID can only contain letters, numbers, hyphens, and underscores.'
        )
    npi_blank = bool(values.get('npi_blank'))
    npi = normalize_id(values.get('npi'))
    if not npi_blank and not is_valid_npi(npi):
        raise ValueError('Provider NPI must be 10 digits, or check Leave NPI blank.')
    profile = values.get('selector_profile') or DEFAULT_SELECTOR_PROFILE
    if profile not in SELECTOR_PROFILES_ALLOWED:
        raise ValueError('Please select a valid selector profile.')
    match_mode = values.get('match_mode') or 'exact'
    if match_mode not in MATCH_MODES_ALLOWED:
        raise ValueError('Please select a valid line match mode.')
    sanitized = {
        'org_id': org_text,
        'payer_id': payer_text,
        'npi_blank': npi_blank,
        'selector_profile': profile,
        'line_level': {
            'match_mode': match_mode,
            'filter_by_visit_date': bool(values.get('filter_by_visit_date')),
            'fallback_to_sole_line': bool(values.get('fallback_to_sole_line')),
            'filter_results_by_visit_date': bool(
                values.get('filter_results_by_visit_date')
            ),
            'match_by_invoice_alignment': bool(
                values.get('match_by_invoice_alignment')
            ),
            'enable_pool_tiers': bool(values.get('enable_pool_tiers')),
            'allow_split_tiebreak': bool(values.get('allow_split_tiebreak')),
            'fallback_within_range': bool(values.get('fallback_within_range')),
            'range_match_by_day_position': bool(
                values.get('range_match_by_day_position')
            ),
            'max_combo': _require_int(
                values.get('max_combo', 3), 'Max line combination size',
                minimum=1,
            ),
        },
    }
    if not npi_blank:
        sanitized['npi'] = npi
    for flag in PAYER_BOOL_FLAGS:
        sanitized[flag] = bool(values.get(flag))
    if values.get('has_result_validation'):
        sanitized['result_validation'] = {
            'field_read_attempts': _require_int(
                values.get('field_read_attempts'), 'Field read attempts',
                minimum=1,
            ),
            'field_retry_delay_s': _require_float(
                values.get('field_retry_delay_s'), 'Field retry delay',
                minimum=0.0,
            ),
            'detail_identity_timeout_ms': _require_int(
                values.get('detail_identity_timeout_ms'),
                'Claim identity timeout', minimum=1000,
            ),
            'remittance_attempts': _require_int(
                values.get('remittance_attempts'), 'Remittance attempts',
                minimum=1,
            ),
            'remittance_retry_delay_s': _require_float(
                values.get('remittance_retry_delay_s'),
                'Remittance retry delay', minimum=0.0,
            ),
        }
    return sanitized


def _require_int(value, label, minimum=0):
    try:
        resolved = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f'{label} must be a whole number.') from None
    if resolved < minimum:
        raise ValueError(f'{label} must be at least {minimum}.')
    return resolved


def _require_float(value, label, minimum=0.0):
    try:
        resolved = float(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f'{label} must be a number.') from None
    if resolved < minimum:
        raise ValueError(f'{label} must be at least {minimum}.')
    return resolved


def _match_id(pattern, text):
    match = pattern.search(text or '')
    if not match:
        return ''
    return normalize_id(unquote(match.group(2)))


def _replace_id(url, encoded_re, plain_re, value):
    encoded = _encode_id(value)

    def encoded_repl(match):
        return match.group(1) + encoded

    updated, count = encoded_re.subn(encoded_repl, url, count=1)
    if count:
        return updated

    def plain_repl(match):
        return match.group(1) + value

    return plain_re.sub(plain_repl, url, count=1)


def _encode_id(value):
    """Percent-encode an ID for the nested ``appUrl`` query, keeping unreserved chars."""
    return ''.join(
        ch if ch.isalnum() or ch in '-_' else f'%{ord(ch):02X}'
        for ch in value
    )


def _write_settings(data):
    path = settings_file_path()
    if not path:
        return
    data['version'] = _SETTINGS_VERSION
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    temp_path = path + '.tmp'
    try:
        with open(temp_path, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.replace(temp_path, path)
    except OSError:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
        raise
