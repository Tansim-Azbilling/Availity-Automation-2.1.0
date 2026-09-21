"""URLs, selectors, payer config, and Excel column contracts."""

AVAILITY_LOGIN_URL = "https://essentials.availity.com/static/public/onb/onboarding-ui-apps/availity-fr-ui/#/login"
AVAILITY_NAVIGATION_HOME = (
    "https://essentials.availity.com/static/web/onb/onboarding-ui-apps/navigation/#/"
)
CDP_ENDPOINT = "http://localhost:9222"
# After this many harvested invoices, remount the claim-search iframe (home hop)
# instead of Edit Search + Clear Form, and recycle an open remittance tab.
# Keep-table patient-group harvests count toward this interval so a large
# cluster cannot skip the remount.
CLAIM_SPA_REMOUNT_EVERY = 25
# Default Provider NPI filled on claim search. Override per payer with
# ``'npi': '<alternate>'`` in PAYER_CONFIG. GUI "Leave NPI blank" uses this
# default instead of a payer-specific NPI.
NPI = "1730244294"

# UI state machine identifiers used by `_set_ui_state` to drive button enabling.
UI_STATE_IDLE = "idle"
UI_STATE_OPENING = "opening_browser"
UI_STATE_READY = "ready"
UI_STATE_RUNNING = "running"


# ============================================================================
# SECTION 2 — CONFIGURATION
# ============================================================================

# ---------------------------------------------------------------------------
# CSS / XPath selectors
# ---------------------------------------------------------------------------
# Selector profiles support payers on different Availity UI generations.
# Set ``selector_profile`` on a PAYER_CONFIG entry ('mui' default, 'legacy' for
# older HIPAA-tab forms, 'aetna' for Aetna Better Health).  Keys with a
# ``_selectors`` suffix hold fallback lists tried in order.

_SHARED_SELECTOR_KEYS = {
    # The SPA renders content inside a named iframe
    'iframe':           'iframe#newBodyFrame',
    'member_id':        '#subscriberMemberId',
    'last_name':        '#patientLastName',
    'first_name':       '#patientFirstName',
    'provider_npi':     '#providerNpi',
    'submit_btn':       '#submit-by276',
    'clear_form':        '#clear-by276',
    'clear_form_fallback': 'button:has-text("Clear Form")',
    'edit_search':      'a:has-text("Edit Search")',
    'back_to_results':  'a:has-text("Back to Results")',
    'results_table':    '#claimsTable',
    'breadcrumb_search':  'a[aria-label="Search"]',
    'breadcrumb_results': 'a[aria-label="Results"]',
    'claims_finalized_date_header': (
        '#claimsTable th[role="columnheader"][title="Toggle SortBy"]:has-text("Finalized Date")'
    ),
    'line_table':       '#lineLevelTable',
    'codes_table':      '#codesTable',
    'remark_codes_grid': (
        '#lineLevelTable .MuiGrid-root:has(p:has-text("Reason/Remark Codes"))'
    ),
    'remark_codes_value': 'p:last-of-type',
    'claim_not_found_ack': (
        'div.MuiAlert-colorError[role="alert"] li:has-text("Acknowledgement/Not Found")'
    ),
    'claim_not_found_warning': (
        'div.MuiAlert-colorWarning[role="alert"] li:has-text("The payer could not find any results")'
    ),
    'invalid_member_id_alert': (
        'div.MuiAlert-root[role="alert"] .MuiAlert-message'
        ':has-text("Subscriber and subscriber id not found")'
    ),
    # Any visible payer alert after search (message text is classified at runtime).
    'payer_search_alert': 'div.MuiAlert-root[role="alert"]',
    'claim_status_panel':   '[data-testid="testClaim StatusPanel"] span.badge',
    # Read only by invoice-alignment payers, to bound the claim's service span.
    'service_dates_panel_selectors': [
        '[data-testid="testService DatesPanel"] p:last-of-type',
        '[id="Service Dates"] p:last-of-type',
    ],
    'remit_claim_tab':    'a.nav-link:has(span:has-text("Claim"))',
    'remit_search_input': '#claimSearchInput',
    'remit_search_btn':   '#claimSearchButton',
    'remit_table':        'div[role="table"][aria-label="Remits"]',
    # Data rows contain claimNumber links; the card-header row does not.
    'remit_data_row':     'div[role="row"]:has(a[id^="claimNumber"])',
    'remit_claim_link':   'a[id^="claimNumber"]',
    'remit_check_link':   'a[id^="checkNumber"]',
    'remit_check_date':   'a[id^="checkNumber"] + div',
    'remit_adj_table':    'table[aria-label^="Adjustments"]',
    'remit_not_found': (
        'strong:has-text("We did not find remittance data matching your search criteria")'
    ),
    'remit_back_link': (
        'a.text-primary[href="#/?tab=claim"]:has-text("Back")'
    ),
    'remit_applied_filters': (
        'div[role="group"][aria-label="Applied Search Terms"]'
    ),
    'remit_remove_filter': 'button[aria-label="Remove Filter"]',
}

SELECTOR_PROFILES = {
    'mui': {
        **_SHARED_SELECTOR_KEYS,
        'date_fill_mode': 'mui_picker',
        # MUI date pickers — legend labels (hidden input ids like :r2b: are dynamic)
        'patient_dob_label':       'Patient Date of Birth',
        'service_from_date_label': 'Service From Date',
        'service_to_date_label':   'Service To Date',
        'claim_id_panel':       '[data-testid="testClaim NumberPanel"] p:last-of-type',
        'finalized_date_panel': '[data-testid="testFinalized DatePanel"] p:last-of-type',
        'effective_date_selectors': [
            '[data-testid="testEffective DatePanel"] p:last-of-type',
            '[id="Effective Date"] p:last-of-type',
        ],
        'check_number_panel':   '[data-testid="testCheck NumberPanel"] p:last-of-type',
        'check_date_panel':     '[data-testid="testCheck DatePanel"] p:last-of-type',
        'billed_amount_selectors': [
            '[data-testid="testBilled AmountPanel"] p:last-of-type',
            '[id="Billed Amount"] p:last-of-type',
            '[data-testid="testBilledAmountPanel"] p:last-of-type',
            'xpath=//*[contains(@data-testid,"Billed") and contains(@data-testid,"Panel")]'
            '//p[last()]',
        ],
        'paid_amount_selectors': [
            '[data-testid="testPaid AmountPanel"] p:last-of-type',
            '[id="Paid Amount"] p:last-of-type',
            '[data-testid="testPaidAmountPanel"] p:last-of-type',
            'xpath=//*[contains(@data-testid,"Paid") and contains(@data-testid,"Panel")]'
            '//p[last()]',
        ],
        'hipaa_tab': (
            '[role="tablist"][aria-label="search tabs"] button[role="tab"]:has-text("HIPAA Standard")'
        ),
    },
    'legacy': {
        **_SHARED_SELECTOR_KEYS,
        'date_fill_mode': 'legacy_input',
        'dob':        '#patientBirthDate',
        'date_from':  '#fromDate',
        'date_to':    '#toDate',
        'claim_id_panel':       '[data-testid="testClaim NumberPanel"] p.text-right',
        'finalized_date_panel': '[data-testid="testFinalized DatePanel"] p.text-right',
        'check_number_panel':   '[data-testid="testCheck NumberPanel"] p.text-right',
        'check_date_panel':     '[data-testid="testCheck DatePanel"] p.text-right',
        'billed_amount_selectors': [
            '[data-testid="testBilled AmountPanel"] p.text-right',
            '[data-testid="testBilledAmountPanel"] p.text-right',
            'xpath=//*[contains(@data-testid,"Billed") and contains(@data-testid,"Panel")]'
            '//p[contains(@class,"text-right")]',
        ],
        'paid_amount_selectors': [
            '[data-testid="testPaid AmountPanel"] p.text-right',
            '[data-testid="testPaidAmountPanel"] p.text-right',
            'xpath=//*[contains(@data-testid,"Paid") and contains(@data-testid,"Panel")]'
            '//p[contains(@class,"text-right")]',
        ],
        'hipaa_tab': 'a[id="HIPAA Standard"][role="button"]',
    },
    'aetna': {
        'iframe':           'iframe#newBodyFrame',
        'member_id':        '#patientMemberId',
        'last_name':        '#patientLastName',
        'first_name':       '#patientFirstName',
        'provider_npi':     '#providerNpi',
        'submit_btn':       '#submit-by276',
        'results_table':    '#claimsTable',
        'claims_finalized_date_header': (
            '#claimsTable th[role="columnheader"][title="Toggle SortBy"]:has-text("Finalized Date")'
        ),
        'line_table':       '#lineLevelTable',
        'line_table_body_row': '#lineLevelTable tbody tr[role="row"]',
        # Service Dates=0, Billed Amount=7, Paid Amount=12; expand in last column
        'line_table_col_defaults': {
            'service_dates': 0,
            'billed': 7,
            'paid': 12,
            'remark_codes': 6,
        },
        'line_expand_button': 'button[title="Toggle Row Expanded"]',
        'codes_table':      '#codesTable',
        'remark_codes_grid': (
            '#lineLevelTable .MuiGrid-root:has(p:has-text("Reason/Remark Codes"))'
        ),
        'remark_codes_value': 'p:last-of-type',
        'claim_not_found_ack': (
            'div.MuiAlert-colorError[role="alert"] li:has-text("Acknowledgement/Not Found")'
        ),
        'claim_not_found_warning': (
            'div.MuiAlert-colorWarning[role="alert"] li:has-text("The payer could not find any results")'
        ),
        'invalid_member_id_alert': (
            'div.MuiAlert-root[role="alert"] .MuiAlert-message'
            ':has-text("Subscriber and subscriber id not found")'
        ),
        'payer_search_alert': 'div.MuiAlert-root[role="alert"]',
        # Claim detail info panels — div[id="…"] with value in p.text-right
        'claim_detail_ready':   '[id="Claim Number"]',
        'claim_id_panel':       '[id="Claim Number"] p.text-right',
        'claim_status_selectors': [
            '[id="Status"] p.text-right',
            '[id="Claim Status"] span.badge',
        ],
        'finalized_date_panel': '[id="Finalized Date"] p.text-right',
        'check_number_panel':   '[id="Check Number"] p.text-right',
        'check_date_panel':     '[id="Check Date"] p.text-right',
        'billed_amount_selectors': [
            '[id="Billed Amount"] p.text-right',
            '[data-testid="testBilled AmountPanel"] p.text-right',
        ],
        'paid_amount_selectors': [
            '[id="Paid Amount"] p.text-right',
            '[data-testid="testPaid AmountPanel"] p.text-right',
        ],
        'remit_claim_tab':    'a.nav-link:has(span:has-text("Claim"))',
        'remit_search_input': '#claimSearchInput',
        'remit_search_btn':   '#claimSearchButton',
        'remit_table':        'div[role="table"][aria-label="Remits"]',
        'remit_data_row':     'div[role="row"]:has(a[id^="claimNumber"])',
        'remit_claim_link':   'a[id^="claimNumber"]',
        'remit_check_link':   'a[id^="checkNumber"]',
        'remit_check_date':   'a[id^="checkNumber"] + div',
        'remit_adj_table':    'table[aria-label^="Adjustments"]',
        'remit_not_found': (
            'strong:has-text("We did not find remittance data matching your search criteria")'
        ),
        'date_fill_mode': 'plain_input',
        # Patient is subscriber — id suffix is dynamic (e.g. patientIsSubscriber-18)
        'patient_is_subscriber': 'input[id^="patientIsSubscriber"]',
        'dob':        '#patientBirthDate',
        'date_from':  '#serviceDates-start',
        'date_to':    '#serviceDates-end',
        'edit_search': '#editSearch',
        'clear_form':  '#clearForm',
        'clear_form_fallback': 'button:has-text("Clear Form")',
    },
}

DEFAULT_SELECTOR_PROFILE = 'mui'

SELECTORS = SELECTOR_PROFILES[DEFAULT_SELECTOR_PROFILE]


def get_selector_profile(payer_config=None):
    """Return the selector dict for a payer (defaults to the MUI profile)."""
    profile_name = DEFAULT_SELECTOR_PROFILE
    if payer_config:
        profile_name = payer_config.get('selector_profile', DEFAULT_SELECTOR_PROFILE)
    return SELECTOR_PROFILES[profile_name]

# Denial reason written when the remittance viewer returns no matching data.
REMIT_NOT_FOUND_DENIAL = 'Remittance not found'

# Remits Check/EFT Date may match the claim-detail Check Date exactly, or fall
# this many calendar days after it (not before). Example: claim-detail
# 12/23/2025 also matches remits 12/24/2025 through 12/30/2025 when this is 7.
REMIT_CHECK_DATE_GRACE_DAYS = 7

# HIPAA Status values that trigger a Reason/Remark expand for Integra / Anthem / Empire.
HIPAA_REMARK_FALLBACK_STATUSES = (
    ('585', 'Denied Charge or Non-covered Charge'),
    ('101', 'Claim was processed as adjustment to previous claim'),
)
HIPAA_REMARK_FALLBACK_STATUS_CODE, HIPAA_REMARK_FALLBACK_STATUS_DESC = (
    HIPAA_REMARK_FALLBACK_STATUSES[0]
)

# Denial Reason values filled when extraction left the field blank ("--").
# Applied for every payer from Claim Status:
#   Paid                 → Fully Paid
#   Paid (Overpayment)   → Fully Paid with Overpayment
#   Pending              → In Process
#   Finalized            → In Process, or Fully Paid when billed == paid > 0
FULLY_PAID_DENIAL_REASON = 'Fully Paid'
FULLY_PAID_OVERPAYMENT_DENIAL_REASON = 'Fully Paid with Overpayment'
IN_PROCESS_DENIAL_REASON = 'In Process'
CLAIM_NOT_FOUND_DENIAL_REASON = 'Claim Not Found'

# ---------------------------------------------------------------------------
# Remittance Viewer search results — div[role="table"][aria-label="Remits"]
# ---------------------------------------------------------------------------
# Bootstrap ARIA grid (role=row / role=cell), not a <table>. Header lives in
# .card-header; data rows live in .card-body. The same Claim # can appear on
# more than one row with different Check/EFT dates — matching uses Claim #
# plus Check/EFT Date (not Check/EFT #). Remits dates up to
# REMIT_CHECK_DATE_GRACE_DAYS after the claim-detail Check Date also match.
# When claim-detail Check Date is missing, a Claim #-only match is used
# only if exactly one remits row has that Claim #.
#
# Stable cell hooks (used at runtime):
#   Claim #            a[id^="claimNumber"]      e.g. claimNumber0
#   Check/EFT #        a[id^="checkNumber"]      e.g. checkNumber0
#   Check/EFT Date     a[id^="checkNumber"] + div   e.g. (07/16/2026)
#   Patient            div[id^="patientNameId"]  name / control # / member ID
#   Download           button[id^="claimdownload"]
REMIT_TABLE_COLUMNS = {
    'service_dates': {
        'header': 'Service Dates',
    },
    'claim_number': {
        'header': 'Claim #',
        'selector': 'a[id^="claimNumber"]',
    },
    'payer': {
        'header': 'Payer',
    },
    'check_number': {
        'header': 'Check/EFT #',
        'selector': 'a[id^="checkNumber"]',
    },
    'check_date': {
        'header': 'Check/EFT Date',
        'selector': 'a[id^="checkNumber"] + div',
    },
    'patient_name': {
        'header': 'Patient Name (Patient Control #) (ID)',
        'selector': 'div[id^="patientNameId"]',
    },
    'patient_amount': {
        'header': 'Patient Amt',
    },
    'total_charged': {
        'header': 'Total Charged Amt',
    },
    'total_paid': {
        'header': 'Total Paid Amt',
    },
    'actions': {
        'header': 'Actions',
        'selector': 'button[id^="claimdownload"]',
    },
}

# ---------------------------------------------------------------------------
# Line-level extraction (service-date rows in #lineLevelTable)
# ---------------------------------------------------------------------------
# filter_by_visit_date — when True, only rows matching the input VisitDate are
#                        processed for amounts and denial codes.
# match_mode — 'exact' (default): normalised range must equal VisitDate
#              (01/03/2025 → 01/03/2025-01/03/2025);
#              'within_range': only within-range matching (no exact pass);
#              'contains': legacy — VisitDate equals either bound of the line.
# filter_results_by_visit_date — when True, narrow #claimsTable invoice matches
#                         to rows whose Service Dates contain the spreadsheet
#                         VisitDate.
# fallback_to_sole_line — when no match but the claim has exactly one service
#                         line, use that line so data is still captured.
# read_hipaa_codes — when True, Denial Reason comes from the collapsed
#                    HIPAA Codes column (first code only) looked up in
#                    #codesTable as type Status. Expand Reason/Remark Codes
#                    only when that Status is 585 Denied Charge or
#                    Non-covered Charge, or 101 Claim was processed as
#                    adjustment to previous claim. Output is the description only.
#
# Per-payer overrides: add a nested ``line_level`` dict to any PAYER_CONFIG entry.

LINE_LEVEL_CONFIG = {
    'filter_by_visit_date': True,
    'match_mode': 'exact',
    'fallback_to_sole_line': True,
}

# ---------------------------------------------------------------------------
# Payer configuration table
# ---------------------------------------------------------------------------
# TO ADD A NEW PAYER — only two steps are needed:
#   1. Add an entry to PAYER_CONFIG with the keys described below.
#   2. Done.  The dropdown, navigation, extraction, and denial logic all
#      read from this dict, so no other code changes are required.
#
# Config keys:
#   url              — Claim-search URL template with {orgId} and {payerId}
#                      placeholders filled from GUI settings (or org_id /
#                      payer_id defaults below)
#   org_id           — Default Availity OrgID substituted into url
#   payer_id         — Default Availity Payer ID substituted into url
#   uses_hipaa_tab   — whether to click the HIPAA Standard tab after load
#   uses_line_level  — whether to scrape line-level service-date rows for
#                      billed/paid amounts (False = read from header panels)
#   uses_remittance  — whether to open the remittance viewer for denial
#                      reasons (True = Villagecaremax path)
#   skip_remittance_when_unposted — when True, skip the remittance viewer if
#                      Check Number, Check Date, and Paid Amount are all still
#                      "--" (Villagecaremax). Denial Reason is then In Process.
#   remittance_when_remarks_blank — when True with uses_remittance, open the
#                      remittance viewer only if Reason/Remark (or HIPAA)
#                      denial is blank / "--". When False, remittance is first.
#   uses_results_finalized_date — read Finalized Date from the #claimsTable
#                      row being opened (before claim detail); False = detail
#                      panel and/or Check Date promotion
#   selector_profile — 'mui' (default), 'legacy' (HIPAA-tab plain inputs), or
#                      'aetna' (Aetna Better Health — no HIPAA tab)
#   informational_alert_phrases — optional tuple of substrings; visible MUI
#                      alerts containing any phrase are ignored (not treated as
#                      search errors). Global phrases apply to all payers.
#   npi              — optional Provider NPI for this payer's claim search form.
#                      Omit to use the default NPI (GUI Leave NPI blank).
#                      Set a string to fill an alternate NPI, e.g. 'npi': '1234567890'.
#                      Set False only in code to skip filling entirely.
#   read_hipaa_codes — when True, build Denial Reason from the line-table
#                      HIPAA Codes column (first code only, e.g. F2:585 → 585)
#                      looked up in #codesTable as type Status. Expand the
#                      line for Reason/Remark Codes only when Status is
#                      585 Denied Charge or Non-covered Charge, or 101
#                      Claim was processed as adjustment to previous claim.
#                      Denial Reason is the description only (no [CODE]
#                      prefix) (Integra / Anthem / Empire).
#   denial_description_only — when True, write Denial Reason as the
#                      description text only (no [CODE] prefix) while still
#                      expanding Reason/Remark codes (Healthfirst). Distinct
#                      from read_hipaa_codes, which also changes lookup.
#   trim_denial_to_first_sentence — when True, keep only the first sentence
#                      of Denial Reason and strip UI chrome such as
#                      "Show more..." (Anthem).
#   canned_paid_denial — legacy Healthfirst flag. Blank Denial Reason is
#                      filled from Claim Status for every payer (Paid,
#                      Paid (Overpayment), Pending, Finalized) regardless
#                      of this setting. Existing codes are never overwritten.
#   detect_adjustment_pairs — when True, treat same-date + same-billed lines
#                      with one positive Paid and one negative Paid as an
#                      original/adjustment pair (Healthfirst). Output billed
#                      is the original once, paid is the net, Claim Status is
#                      Adjusted, and Reason/Remark Codes come from the first
#                      positive-paid line, falling back to the matching
#                      negative-paid line when the original has no codes.
#   reset_via_edit_search — when True, return to a blank search form from
#                      claim detail via the iframe Edit Search button then
#                      Clear Form, instead of a full shell reload
#                      (Healthfirst, Aetna, Villagecaremax, Integra, Anthem,
#                      Empire). Used after an invoice's submissions are
#                      harvested. Falls back to the full reload on failure.
#   return_via_back_to_results — when True, return to #claimsTable only via
#                      the iframe Back to Results link (Villagecaremax,
#                      Integra, Anthem, Empire). Used between remaining
#                      Availity submissions of the same invoice, not after
#                      the invoice is finished. Does not use browser back
#                      or the Results breadcrumb.
#   match_results_by_visit_and_amount — when True, pick #claimsTable rows by
#                      Service Dates + Billed Amount matched to spreadsheet
#                      VisitDate + Amount (Aetna). Skips invoice/bundle
#                      matching and does not fan harvested claims out to
#                      other invoice sibling rows. Requires an Amount column.
#   supports_search_grouping — when True (default), rows that share a search
#                      form may reuse #claimsTable. When False, every row
#                      searches alone.
#   group_search_by_patient — when True with supports_search_grouping, Service
#                      From/To is min–max VisitDate for the patient within a
#                      90-day cluster, and work-queue rows in that cluster
#                      share one search (Villagecaremax). Default False keeps
#                      invoice-scoped consecutive grouping.
#   gui_enabled      — when False, hide this payer from the GUI dropdown
#                      (default True).

CLAIM_SEARCH_URL_TEMPLATE = (
    "https://essentials.availity.com/static/web/onb/onboarding-ui-apps/navigation/#/loadApp/"
    "?appUrl=%2Fstatic%2Fweb%2Fpost%2Fcs%2Fenhanced-claim-status-ui%2F%23%2Felement"
    "%2Fdashboard%3ForgId%3D{orgId}%26payerId%3D{payerId}"
)
AETNA_CLAIM_SEARCH_URL_TEMPLATE = (
    "https://essentials.availity.com/static/web/onb/onboarding-ui-apps/navigation/#/loadApp/"
    "?appUrl=%2Fstatic%2Fweb%2Fpost%2Fcs%2Fenhanced-claim-status-ui%2F%23%2Fdashboard"
    "%3ForgId%3D{orgId}%26payerId%3D{payerId}"
)

PAYER_CONFIG = {
    'Healthfirst': {
        'url': CLAIM_SEARCH_URL_TEMPLATE,
        'org_id': '317526',
        'payer_id': '80141T',
        'uses_hipaa_tab':  True,
        'uses_line_level': True,
        'uses_remittance': True,
        'remittance_when_remarks_blank': True,
        'uses_results_finalized_date': False,
        'detect_adjustment_pairs': True,
        'canned_paid_denial': True,
        'denial_description_only': True,
        'reset_via_edit_search': True,
        'gui_enabled': True,
    },
    'Integra': {
        'url': CLAIM_SEARCH_URL_TEMPLATE,
        'org_id': '36045893',
        'payer_id': '303',
        'uses_hipaa_tab':  True,
        'uses_line_level': True,
        'uses_remittance': True,
        'remittance_when_remarks_blank': True,
        'uses_results_finalized_date': False,
        # 'npi': 1164078929,
        'read_hipaa_codes': True,
        'reset_via_edit_search': True,
        'return_via_back_to_results': True,
        'gui_enabled': True,
    },
    'Villagecaremax': {
        'url': CLAIM_SEARCH_URL_TEMPLATE,
        'org_id': '34909954',
        'payer_id': '26545',
        'uses_hipaa_tab':  False,
        'uses_line_level': False,
        'uses_remittance': True,
        'skip_remittance_when_unposted': True,
        'remittance_when_remarks_blank': False,
        'uses_results_finalized_date': True,
        'reset_via_edit_search': True,
        'return_via_back_to_results': True,
        'group_search_by_patient': True,
        'gui_enabled': True,
    },
    'Aetna Better Health': {
        'url': AETNA_CLAIM_SEARCH_URL_TEMPLATE,
        'org_id': '11255438',
        'payer_id': 'AETNA',
        'uses_hipaa_tab':  False,
        'uses_line_level': True,
        'uses_remittance': True,  # Denial Reason from remittance viewer, not remark codes
        'remittance_when_remarks_blank': False,
        'uses_results_finalized_date': False,
        'selector_profile': 'aetna',
        'match_results_by_visit_and_amount': True,
        'reset_via_edit_search': True,
        'gui_enabled': True,
    },
    'Anthem BCBS': {
        'url': CLAIM_SEARCH_URL_TEMPLATE,
        'org_id': '35022783',
        'payer_id': '303',
        'uses_hipaa_tab':  True,
        'uses_line_level': True,
        'uses_remittance': True,
        'remittance_when_remarks_blank': True,
        'uses_results_finalized_date': False,
        # 'npi': '1164078929',  # payer-specific NPI; omit to use default
        'read_hipaa_codes': True,
        'trim_denial_to_first_sentence': True,
        'reset_via_edit_search': True,
        'return_via_back_to_results': True,
        'gui_enabled': True,
    },
    'Fidelis': {
        'url': CLAIM_SEARCH_URL_TEMPLATE,
        'org_id': '36045893',
        'payer_id': '11315',
        'uses_hipaa_tab': True,
        'uses_line_level': True,
        'uses_remittance': False,
        'remittance_when_remarks_blank': False,
        'uses_results_finalized_date': False,
        'informational_alert_phrases': (
            'complete provider record in the provider list',
        ),
        'gui_enabled': True,
    },
    'Empire': {
        'url': CLAIM_SEARCH_URL_TEMPLATE,
        'org_id': '36601809',
        'payer_id': '303',
        'uses_hipaa_tab':  True,
        'uses_line_level': True,
        'uses_remittance': True,
        'remittance_when_remarks_blank': True,
        'uses_results_finalized_date': False,
        'read_hipaa_codes': True,
        'line_level': {
            'match_mode': 'exact',
            'filter_by_visit_date': True,
            'fallback_to_sole_line': False,
        },
        'reset_via_edit_search': True,
        'return_via_back_to_results': True,
        'gui_enabled': True,
    },
    'SWHNY': {
        'url': CLAIM_SEARCH_URL_TEMPLATE,
        'org_id': '34909954',
        'payer_id': 'A0281',
        'npi': '1376097303',
        'uses_hipaa_tab':  True,
        'uses_line_level': True,
        'uses_remittance': True,  # Denial Reason from remittance viewer, not remark codes
        'remittance_when_remarks_blank': False,
        'uses_results_finalized_date': False,
        'informational_alert_phrases': (
            'allow up to 5 business days for it to appear',
            'poa (present on admission) indicator issue',
        ),
        # SWHNY repeats the claim service span on every line, so VisitDate
        # cannot pick a line; the whole invoice is matched at once instead.
        'required_extra_columns': ('Amount', 'Units'),
        'line_level': {
            'match_by_invoice_alignment': True,
            'fallback_within_range': False,
            'range_match_by_day_position': False,
            'enable_pool_tiers': True,
            'allow_split_tiebreak': False,
            'max_combo': 3,
        },
        'gui_enabled': True,
    },
    'Molina Home Health': {
        'url': CLAIM_SEARCH_URL_TEMPLATE,
        'org_id': '11726857',
        'payer_id': '16146',
        'npi': '1679606735',
        'uses_hipaa_tab':  True,
        'uses_line_level': True,
        'uses_remittance': True,
        'uses_remittance_billed': False,
        'uses_results_finalized_date': False,
        'reconcile_finalized_date_conflicts': True,
        'filter_remit_disclaimer_text': True,
        'informational_alert_phrases': (
            'ny affinity member',
            'poa (present on admission) indicator issue',
        ),
        'line_level': {
            'fallback_within_range': True,
            'range_match_by_day_position': True,
        },
        'result_validation': {
            'field_read_attempts': 3,
            'field_retry_delay_s': 0.35,
            'detail_identity_timeout_ms': 15000,
            'remittance_attempts': 3,
            'remittance_retry_delay_s': 0.8,
        },
        'gui_enabled': True,
    },
}

def resolve_provider_npi(payer_config=None):
    """Return the Provider NPI to fill on claim search, or None to skip.

    Reads optional ``npi`` from a PAYER_CONFIG entry:
      - omitted → saved default NPI, else the ``NPI`` constant
        (GUI Leave NPI blank uses this path)
      - string  → that alternate NPI
      - False   → skip filling (code-only; the GUI no longer maps here)
    """
    if payer_config is not None and 'npi' in payer_config:
        override = payer_config['npi']
        if override is False or override is None:
            return None
        text = str(override).strip()
        return text or None
    try:
        from availity_app.user_settings import get_default_npi
        return get_default_npi()
    except Exception:
        return NPI


def get_gui_payer_names():
    """Return payer names shown in the GUI dropdown."""
    return [
        name
        for name, cfg in PAYER_CONFIG.items()
        if cfg.get('gui_enabled', True)
    ]


def get_default_gui_payer():
    """Return the first GUI-enabled payer, or an empty string if none."""
    names = get_gui_payer_names()
    return names[0] if names else ''

REMITTANCE_URLS = {
    'search': (
        "https://essentials.availity.com/static/web/onb/onboarding-ui-apps/navigation/"
        "#/loadApp/?appUrl=%2Fstatic%2Fweb%2Fpost%2Frm%2Fremitviewerui%2F"
    ),
    # Navigate here after reading denial data to reset the remit viewer state
    'home': (
        "https://essentials.availity.com/static/web/onb/onboarding-ui-apps/navigation/"
        "#/loadApp/?appUrl=%2Fstatic%2Fweb%2Fpost%2Frm%2Fremitviewerui%2F%23%2F"
    ),
}

# Columns that MUST exist in the input Excel before any processing starts.
# Optional: Recheck — process "yes" (re-runs Done rows on resume) or empty; skip "no".
# validate_dataframe() checks these and aborts early if any are absent.
REQUIRED_COLUMNS = [
    'AltPatientID', 'PatientName', 'DOB',
    'StartDate', 'EndDate', 'InvoiceNumber', 'VisitDate',
]

# Columns appended to the dataframe to hold automation output.
OUTPUT_COLUMNS = [
    'Claim ID', 'Billed Amount', 'Paid Amount', 'Claim Status',
    'Denial Reason', 'Last Action Taken', 'Finalized Date', 'Check Number', 'Check Date',
    'Recheck',
]
STATUS_COLUMNS = ['AutomationStatus', 'LastError']


def resolved_output_columns(payer_config=None):
    """Return OUTPUT_COLUMNS with any payer-declared extra columns inserted.

    A payer opts in by setting ``extra_output_columns`` to a sequence of
    ``(column_name, insert_after_column)`` pairs.  Payers without the key get
    the stock column list, so their output files are unchanged.

    Falls back to the payer selected for the current batch, which keeps the
    call sites free of extra plumbing.
    """
    if payer_config is None:
        from availity_app import state
        payer_config = state.selected_payer_config or {}
    extras = payer_config.get('extra_output_columns') or ()
    if not extras:
        return list(OUTPUT_COLUMNS)
    cols = list(OUTPUT_COLUMNS)
    for name, after in extras:
        if name in cols:
            continue
        cols.insert(cols.index(after) + 1 if after in cols else len(cols), name)
    return cols


# Claim Status and Denial Reason when Availity has no matching claim:
# acknowledgement alert, results with no invoice/VisitDate match, or a
# search that produced neither a results table nor a known payer alert.
CLAIM_NOT_FOUND_STATUS = 'Claim Not Found'

# Legacy Claim Status values still recognized on existing workbooks.
CLAIM_NOT_FOUND_LEGACY_STATUS = 'Claim Not found'
CLAIM_NOT_FOUND_SEARCH_MANUALLY_STATUS = 'Claim not found. Search manually'
NO_CLAIMS_FOUND_ADVISED_STATUS = 'No claims found. Advised to search manually'

# Claim Status when search returns subscriber/member id not found (MUI alert, no results table).
INVALID_MEMBER_ID_STATUS = 'Invalid Member ID'

# Claim Status when the input row has no usable DOB (e.g. spreadsheet says Not Found).
DOB_NOT_FOUND_STATUS = 'DOB Not Found'

# Optional input Claim Status / Denial Reason. Recheck=no rows with this exact
# phrase in both optional columns are completed without an Availity search.
IN_BILLING_REVIEW_STATUS = 'In Billing Review'

# Legacy Claim Status written by older builds for unrecognized search alerts.
# Current runs no longer write this; wait banners are not a claim outcome.
PAYER_SEARCH_ERROR_STATUS = 'Payer search error'

# Claim Status value when paid amount exceeds billed amount.
PAID_OVERPAYMENT_STATUS = 'Paid (Overpayment)'

# Claim Status when Healthfirst original + negative-paid adjustment lines pair.
ADJUSTED_STATUS = 'Adjusted'

# Claim Status when line-level VisitDate cannot be mapped unambiguously
# (within-range day-position fallback failed count gate or index bounds).
CLAIM_UNIDENTIFIED_STATUS = (
    'Claim could not be identified. Please search manually'
)

# Claim Status when a row cannot be determined (ambiguous line join,
# incomplete harvest, unpaired reversal, mixed combo denials).
NEED_MANUAL_REVIEW_STATUS = 'Need Manual Review'

# Error labels written into Claim Status when a row cannot be processed.
# Used by _record_error(), _row_has_prior_error(), and batch retry eligibility.
# Invalid Member ID is a completed outcome (like Claim Not Found): skip on later
# runs unless Recheck=yes.
KNOWN_ERROR_STATUSES = frozenset({
    'Navigation failed', 'Form error', 'Search failed',
    NO_CLAIMS_FOUND_ADVISED_STATUS,
    CLAIM_NOT_FOUND_SEARCH_MANUALLY_STATUS, 'Critical error',
    'Missing InvoiceNumber', 'Missing Amount', 'Missing VisitDate', 'Error',
    CLAIM_UNIDENTIFIED_STATUS,
    NEED_MANUAL_REVIEW_STATUS,
})
