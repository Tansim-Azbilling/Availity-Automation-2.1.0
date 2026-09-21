from availity_app.constants import NPI, PAYER_CONFIG, resolve_provider_npi


def test_default_npi_when_omitted():
    assert resolve_provider_npi(None) == NPI
    assert resolve_provider_npi({}) == NPI
    assert resolve_provider_npi(PAYER_CONFIG['Healthfirst']) == NPI


def test_alternate_npi_override():
    assert resolve_provider_npi({'npi': '1234567890'}) == '1234567890'
    assert resolve_provider_npi({'npi': ' 9876543210 '}) == '9876543210'


def test_skip_npi_when_false():
    assert resolve_provider_npi({'npi': False}) is None
    assert 'npi' not in PAYER_CONFIG['Anthem BCBS']
    assert resolve_provider_npi(PAYER_CONFIG['Anthem BCBS']) == NPI
