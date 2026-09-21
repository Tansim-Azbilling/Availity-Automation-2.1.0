"""Search wait ignores wait banners; GUI miss phrases apply to every payer."""
from unittest.mock import MagicMock, patch

from availity_app.automation import (
    SEARCH_RESPONSE_TIMEOUT_MS,
    _PORTAL_SEARCH_DATE_FALLBACK_OUTCOMES,
    _classify_payer_alert_text,
    _gui_search_miss_outcome,
    _known_search_miss_from_alerts,
    _resolve_search_outcome,
    _search_response_ready,
)
from availity_app.constants import SELECTORS


WAIT_BANNER = 'The Transaction is taking longer than expected'


def _iframe_with_alerts(messages, *, table_visible=False, selector_hits=()):
    iframe = MagicMock()

    def locator(sel):
        loc = MagicMock()
        if sel == SELECTORS['results_table']:
            loc.is_visible.return_value = table_visible
            loc.count.return_value = 1 if table_visible else 0
            return loc
        if sel in selector_hits:
            loc.count.return_value = 1
            loc.first.is_visible.return_value = True
            return loc
        if sel == SELECTORS['payer_search_alert']:
            loc.count.return_value = len(messages)

            def nth(index):
                item = MagicMock()
                item.is_visible.return_value = True
                item.locator.return_value.first.text_content.return_value = (
                    messages[index]
                )
                return item

            loc.nth.side_effect = nth
            return loc
        loc.count.return_value = 0
        loc.is_visible.return_value = False
        loc.first.is_visible.return_value = False
        return loc

    iframe.locator.side_effect = locator
    return iframe


def test_wait_banner_is_not_a_known_miss():
    assert _classify_payer_alert_text(WAIT_BANNER) is None
    assert _classify_payer_alert_text(
        'Please wait while we retrieve your results'
    ) is None


def test_built_in_miss_copy_still_classifies():
    assert _classify_payer_alert_text(
        'Acknowledgement/Not Found'
    ) == 'not_found'
    assert _classify_payer_alert_text(
        'Subscriber and subscriber id not found'
    ) == 'invalid_member_id'


@patch(
    'availity_app.automation.get_search_miss_phrases',
    return_value=[],
)
def test_wait_banner_does_not_make_search_ready(_phrases):
    iframe = _iframe_with_alerts([WAIT_BANNER])
    assert _known_search_miss_from_alerts(iframe) is None
    assert _search_response_ready(iframe) is False
    assert _resolve_search_outcome(iframe) == 'failed'


@patch(
    'availity_app.automation.get_search_miss_phrases',
    return_value=[{'phrase': 'payer closed the request', 'outcome': 'not_found'}],
)
def test_gui_miss_phrase_applies_across_payers(_phrases):
    iframe = _iframe_with_alerts(['The payer closed the request unexpectedly.'])
    assert _gui_search_miss_outcome(
        'The payer closed the request unexpectedly.'
    ) == 'not_found'
    assert _known_search_miss_from_alerts(iframe) == 'not_found'
    assert _search_response_ready(iframe) is True
    assert _resolve_search_outcome(iframe) == 'not_found'


@patch(
    'availity_app.automation.get_search_miss_phrases',
    return_value=[{
        'phrase': 'member record is invalid',
        'outcome': 'invalid_member_id',
    }],
)
def test_gui_miss_phrase_can_map_to_invalid_member(_phrases):
    iframe = _iframe_with_alerts(['Member record is invalid for this search'])
    assert _known_search_miss_from_alerts(iframe) == 'invalid_member_id'
    assert _resolve_search_outcome(iframe) == 'invalid_member_id'


@patch(
    'availity_app.automation.get_search_miss_phrases',
    return_value=[{'phrase': 'closed the request', 'outcome': 'not_found'}],
)
def test_gui_miss_can_be_the_second_visible_alert(_phrases):
    iframe = _iframe_with_alerts([
        WAIT_BANNER,
        'The payer closed the request unexpectedly.',
    ])
    assert _known_search_miss_from_alerts(iframe) == 'not_found'
    assert _search_response_ready(iframe) is True


def test_date_fallback_is_only_for_known_misses():
    assert _PORTAL_SEARCH_DATE_FALLBACK_OUTCOMES == frozenset({
        'invalid_member_id', 'not_found',
    })
    assert 'payer_error' not in _PORTAL_SEARCH_DATE_FALLBACK_OUTCOMES


def test_search_wait_cap_is_bounded():
    assert SEARCH_RESPONSE_TIMEOUT_MS == 30000
