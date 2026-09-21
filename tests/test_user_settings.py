"""OrgID / Payer ID settings persistence and URL rewriting."""
import json
import os
import sys

import pytest

from availity_app.constants import CLAIM_SEARCH_URL_TEMPLATE, PAYER_CONFIG, resolve_provider_npi
from availity_app.user_settings import (
    apply_org_payer_ids,
    app_install_dir,
    clear_payer_ids,
    default_org_payer_ids,
    get_default_npi,
    get_large_file_save_interval,
    get_medium_file_save_interval,
    get_output_formatting_enabled,
    get_remit_check_date_grace_days,
    get_session_error_limit,
    get_startup_payer,
    get_transient_error_limit,
    get_uppercase_export_enabled,
    global_form_values,
    ids_for_payer,
    is_valid_id,
    load_payer_ids,
    load_settings,
    parse_org_payer_ids,
    payer_form_values,
    resolve_payer_config,
    save_all_settings,
    save_payer_ids,
    set_settings_path_for_tests,
    settings_file_path,
    settings_load_error,
)


HEALTHFIRST_URL = PAYER_CONFIG['Healthfirst']['url']
ENCODED_CLAIM_SEARCH_URL = (
    "https://essentials.availity.com/static/web/onb/onboarding-ui-apps/navigation/#/loadApp/"
    "?appUrl=%2Fstatic%2Fweb%2Fpost%2Fcs%2Fenhanced-claim-status-ui%2F%23%2Felement"
    "%2Fdashboard%3ForgId%3D317526%26payerId%3D80141T"
)


@pytest.fixture
def settings_file(tmp_path):
    path = tmp_path / 'settings.json'
    set_settings_path_for_tests(str(path))
    yield path
    set_settings_path_for_tests(None)


def test_parse_org_payer_ids_from_encoded_url():
    org_id, payer_id = parse_org_payer_ids(ENCODED_CLAIM_SEARCH_URL)
    assert org_id == '317526'
    assert payer_id == '80141T'


def test_parse_org_payer_ids_from_plain_query():
    org_id, payer_id = parse_org_payer_ids(
        'https://example.com/dashboard?orgId=11255438&payerId=AETNA'
    )
    assert org_id == '11255438'
    assert payer_id == 'AETNA'


def test_apply_org_payer_ids_rewrites_encoded_url():
    updated = apply_org_payer_ids(ENCODED_CLAIM_SEARCH_URL, '99999', 'NEWPAY')
    org_id, payer_id = parse_org_payer_ids(updated)
    assert org_id == '99999'
    assert payer_id == 'NEWPAY'
    assert 'orgId%3D99999' in updated
    assert 'payerId%3DNEWPAY' in updated
    assert 'orgId%3D317526' not in updated
    assert 'payerId%3D80141T' not in updated


def test_apply_org_payer_ids_fills_placeholders():
    updated = apply_org_payer_ids(CLAIM_SEARCH_URL_TEMPLATE, '34909954', '303')
    org_id, payer_id = parse_org_payer_ids(updated)
    assert org_id == '34909954'
    assert payer_id == '303'
    assert '{orgId}' not in updated
    assert '{payerId}' not in updated


def test_apply_org_payer_ids_skips_blank_values():
    updated = apply_org_payer_ids(HEALTHFIRST_URL, '', None)
    assert updated == HEALTHFIRST_URL
    encoded = apply_org_payer_ids(ENCODED_CLAIM_SEARCH_URL, '', None)
    assert encoded == ENCODED_CLAIM_SEARCH_URL


def test_is_valid_id():
    assert is_valid_id('317526')
    assert is_valid_id('80141T')
    assert is_valid_id('AETNA')
    assert not is_valid_id('')
    assert not is_valid_id('bad id')
    assert not is_valid_id('a/b')


def test_save_load_and_clear_payer_ids(settings_file):
    save_payer_ids('Healthfirst', '111', 'ABC')
    assert load_payer_ids('Healthfirst') == {'org_id': '111', 'payer_id': 'ABC'}
    assert ids_for_payer('Healthfirst') == ('111', 'ABC')

    data = json.loads(settings_file.read_text(encoding='utf-8'))
    assert data['payers']['Healthfirst']['org_id'] == '111'

    org_id, payer_id = clear_payer_ids('Healthfirst')
    assert (org_id, payer_id) == default_org_payer_ids('Healthfirst')
    assert load_payer_ids('Healthfirst') == {}
    assert ids_for_payer('Healthfirst') == default_org_payer_ids('Healthfirst')


def test_save_payer_ids_requires_payer_and_values(settings_file):
    with pytest.raises(ValueError, match='Select a payer'):
        save_payer_ids('', '111', 'ABC')
    with pytest.raises(ValueError, match='OrgID'):
        save_payer_ids('Healthfirst', '  ', 'ABC')
    with pytest.raises(ValueError, match='Payer ID'):
        save_payer_ids('Healthfirst', '111', '')
    with pytest.raises(ValueError, match='OrgID can only contain'):
        save_payer_ids('Healthfirst', '111 222', 'ABC')


def test_resolve_payer_config_uses_saved_ids(settings_file):
    original = PAYER_CONFIG['Healthfirst']['url']
    save_payer_ids('Healthfirst', '55555', 'ZZZ')
    cfg = resolve_payer_config('Healthfirst')
    assert cfg is not PAYER_CONFIG['Healthfirst']
    assert PAYER_CONFIG['Healthfirst']['url'] == original
    assert parse_org_payer_ids(cfg['url']) == ('55555', 'ZZZ')
    assert cfg['uses_hipaa_tab'] is True


def test_resolve_payer_config_defaults_without_save(settings_file):
    cfg = resolve_payer_config('Healthfirst')
    assert parse_org_payer_ids(cfg['url']) == default_org_payer_ids('Healthfirst')


def test_ids_for_payer_are_independent(settings_file):
    save_payer_ids('Healthfirst', '111', 'AAA')
    save_payer_ids('Fidelis', '222', 'BBB')
    assert ids_for_payer('Healthfirst') == ('111', 'AAA')
    assert ids_for_payer('Fidelis') == ('222', 'BBB')
    assert ids_for_payer('Integra') == default_org_payer_ids('Integra')


def test_resolve_without_settings_keeps_hardcoded_flags(settings_file):
    hf = resolve_payer_config('Healthfirst')
    assert hf['uses_hipaa_tab'] is True
    assert hf['uses_remittance'] is True
    assert hf['remittance_when_remarks_blank'] is True
    assert hf.get('skip_remittance_when_unposted', False) is False
    assert hf['detect_adjustment_pairs'] is True
    assert 'npi' not in hf
    assert parse_org_payer_ids(hf['url']) == default_org_payer_ids('Healthfirst')
    aetna = resolve_payer_config('Aetna Better Health')
    assert aetna['selector_profile'] == 'aetna'
    empire = resolve_payer_config('Empire')
    assert empire['line_level']['fallback_to_sole_line'] is False
    from availity_app.search_grouping import (
        supports_patient_search_grouping,
        supports_search_grouping,
    )
    assert supports_search_grouping(resolve_payer_config('Fidelis')) is True
    assert supports_patient_search_grouping(resolve_payer_config('Healthfirst')) is False
    assert supports_patient_search_grouping(resolve_payer_config('Villagecaremax')) is True


def test_save_payer_ids_merges_without_dropping_flags(settings_file):
    values = payer_form_values('Healthfirst')
    values['uses_remittance'] = False
    values['remittance_when_remarks_blank'] = False
    save_all_settings(global_form_values(), 'Healthfirst', values)
    save_payer_ids('Healthfirst', '111', 'ABC')
    cfg = resolve_payer_config('Healthfirst')
    assert cfg['uses_remittance'] is False
    assert cfg['remittance_when_remarks_blank'] is False
    assert parse_org_payer_ids(cfg['url']) == ('111', 'ABC')


def test_skip_remittance_when_unposted_saves_and_overlays(settings_file):
    vcm = payer_form_values('Villagecaremax')
    assert vcm['skip_remittance_when_unposted'] is True
    vcm['skip_remittance_when_unposted'] = False
    save_all_settings(global_form_values(), 'Villagecaremax', vcm)
    cfg = resolve_payer_config('Villagecaremax')
    assert cfg['skip_remittance_when_unposted'] is False
    hf = payer_form_values('Healthfirst')
    assert hf['skip_remittance_when_unposted'] is False


def test_npi_blank_defaults_true_and_uses_default_npi(settings_file):
    values = payer_form_values('Healthfirst')
    assert values['npi_blank'] is True
    assert values['npi'] == ''
    values['npi_blank'] = True
    g = global_form_values()
    g['default_npi'] = '1234567890'
    save_all_settings(g, 'Healthfirst', values)
    cfg = resolve_payer_config('Healthfirst')
    assert 'npi' not in cfg
    assert resolve_provider_npi(cfg) == '1234567890'


def test_npi_blank_overrides_payer_specific_npi(settings_file):
    values = payer_form_values('SWHNY')
    assert values['npi_blank'] is False
    values['npi_blank'] = True
    g = global_form_values()
    g['default_npi'] = '1234567890'
    save_all_settings(g, 'SWHNY', values)
    cfg = resolve_payer_config('SWHNY')
    assert 'npi' not in cfg
    assert resolve_provider_npi(cfg) == '1234567890'


def test_global_default_npi_used_when_payer_omits_npi(settings_file):
    settings_file.write_text(
        json.dumps({
            'version': 1,
            'global': {'default_npi': '1234567890'},
            'payers': {},
        }),
        encoding='utf-8',
    )
    assert get_default_npi() == '1234567890'
    assert resolve_provider_npi({}) == '1234567890'
    assert resolve_provider_npi(PAYER_CONFIG['Healthfirst']) == '1234567890'


def test_getters_match_code_defaults_without_json(settings_file):
    assert get_remit_check_date_grace_days() == 7
    assert get_session_error_limit() == 3
    assert get_transient_error_limit() == 5
    assert get_medium_file_save_interval() == 10
    assert get_large_file_save_interval() == 100
    assert get_output_formatting_enabled() is True
    assert get_uppercase_export_enabled() is False
    assert get_startup_payer() == 'Healthfirst'
    from availity_app.user_settings import get_search_miss_phrases
    assert get_search_miss_phrases() == []
    from availity_app.automation import _should_save_progress
    assert _should_save_progress(200, 10, 0.0) is True
    assert _should_save_progress(200, 9, 0.0) is False


def test_save_interval_and_circuit_overrides(settings_file):
    g = global_form_values()
    g['medium_file_save_interval'] = 3
    g['session_error_limit'] = 2
    save_all_settings(g, 'Healthfirst', payer_form_values('Healthfirst'))
    assert get_medium_file_save_interval() == 3
    from availity_app.automation import _should_save_progress
    assert _should_save_progress(200, 3, 0.0) is True
    assert _should_save_progress(200, 2, 0.0) is False
    from availity_app.resilience import BatchCircuitBreaker
    breaker = BatchCircuitBreaker()
    breaker.record_session_failure()
    assert not breaker.tripped
    breaker.record_session_failure()
    assert breaker.tripped


def test_uppercase_export_saves_globally(settings_file):
    assert get_uppercase_export_enabled() is False
    g = global_form_values()
    g['uppercase_export_enabled'] = True
    save_all_settings(g, 'Healthfirst', payer_form_values('Healthfirst'))
    assert get_uppercase_export_enabled() is True
    assert global_form_values()['uppercase_export_enabled'] is True
    saved = json.loads(settings_file.read_text(encoding='utf-8'))
    assert saved['global']['uppercase_export_enabled'] is True


def test_clear_payer_keeps_globals(settings_file):
    g = global_form_values()
    g['default_npi'] = '1234567890'
    p = payer_form_values('Healthfirst')
    p['org_id'] = '111'
    p['payer_id'] = 'ABC'
    save_all_settings(g, 'Healthfirst', p)
    clear_payer_ids('Healthfirst')
    assert get_default_npi() == '1234567890'
    assert load_payer_ids('Healthfirst') == {}
    cfg = resolve_payer_config('Healthfirst')
    assert cfg['uses_remittance'] is True
    assert cfg['remittance_when_remarks_blank'] is True
    assert 'npi' not in cfg


def test_settings_file_path_override_wins(tmp_path):
    path = str(tmp_path / 'custom.json')
    set_settings_path_for_tests(path)
    assert settings_file_path() == path


def test_settings_file_path_source_is_project_root():
    set_settings_path_for_tests(None)
    path = settings_file_path()
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert os.path.basename(path) == 'settings.json'
    assert os.path.dirname(path) == project_root
    assert app_install_dir() == project_root


def test_settings_file_path_frozen_uses_executable_dir(monkeypatch, tmp_path):
    set_settings_path_for_tests(None)
    exe = tmp_path / 'Availity_Automation.exe'
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(exe))
    assert settings_file_path() == str(tmp_path / 'settings.json')
    assert app_install_dir() == str(tmp_path)


def test_missing_settings_is_not_a_load_error(settings_file):
    assert not settings_file.exists()
    data = load_settings()
    assert data == {'version': 1, 'global': {}, 'payers': {}}
    assert settings_load_error() is None


def test_corrupt_settings_returns_empty_and_records_error(settings_file):
    settings_file.write_text('{not json', encoding='utf-8')
    data = load_settings()
    assert data == {'version': 1, 'global': {}, 'payers': {}}
    error = settings_load_error()
    assert error
    assert 'could not be read' in error
    assert str(settings_file) in error


def test_non_object_settings_returns_empty_and_records_error(settings_file):
    settings_file.write_text('[]', encoding='utf-8')
    data = load_settings()
    assert data == {'version': 1, 'global': {}, 'payers': {}}
    error = settings_load_error()
    assert error
    assert 'not a valid settings file' in error


def test_search_miss_phrases_round_trip(settings_file):
    from availity_app.user_settings import get_search_miss_phrases

    g = global_form_values()
    g['search_miss_phrases'] = [
        {'phrase': '  Payer closed the request  ', 'outcome': 'Claim Not Found'},
        {'phrase': 'member record is invalid', 'outcome': 'invalid_member_id'},
        {'phrase': 'payer closed the request', 'outcome': 'not_found'},
        {'phrase': '', 'outcome': 'not_found'},
    ]
    save_all_settings(g, 'Healthfirst', payer_form_values('Healthfirst'))
    saved = get_search_miss_phrases()
    assert saved == [
        {'phrase': 'Payer closed the request', 'outcome': 'not_found'},
        {'phrase': 'member record is invalid', 'outcome': 'invalid_member_id'},
    ]
    assert global_form_values()['search_miss_phrases'] == saved
