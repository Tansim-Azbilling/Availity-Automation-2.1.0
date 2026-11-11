"""Shared process state (GUI refs, Playwright, batch coordination)."""
import threading

is_running = False
worker_busy = False
current_playwright = None
current_browser = None
playwright_thread_id = None
playwright_disconnect_requested = False
playwright_lock = threading.Lock()
managed_edge_process = None
browser_owned_by_app = False

cdp_port = None
cdp_endpoint = None
edge_user_data_dir = None

selected_payer_config = None
selected_payer_name = None
current_payer_name = None

resume_event = threading.Event()
resume_decision = "cancel"

current_batch_df = None
current_batch_output_folder = None
remit_page = None
invoice_searches_since_remount = 0
save_lock = threading.Lock()
df_mutate_lock = threading.Lock()
stop_save_requested = False
rows_since_last_save = 0
last_save_monotonic = 0.0
dirty_row_indices = set()
_large_autofit_notice_logged = False

loaded_df = None
loaded_excel_path = None
loaded_run_path = None
loaded_run_output_folder = None
recheck_import_busy = False
excel_row_stats = None

root = None
open_edge_button = None
start_button = None
stop_button = None
log_text = None
collection_file_var = None
output_folder_var = None
batch_size_var = None
payer_var = None
total_rows_var = None
recheck_rows_var = None
new_rows_var = None
progress_frame = None
progress_bar = None
progress_percent_var = None
progress_status_var = None
settings_panel = None
settings_body = None
settings_visible = False
org_id_var = None
payer_id_var = None
payer_npi_var = None
npi_blank_var = None
selector_profile_var = None
match_mode_var = None
settings_flag_vars = None
settings_status_var = None
payer_npi_entry = None

loaded_sheet_name = "Sheet1"
search_miss_list_frame = None
search_miss_rows = None
