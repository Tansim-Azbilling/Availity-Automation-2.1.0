"""Session .txt log written to the output folder."""
import os
import re

from availity_app.logging_gui import (
    close_session_log,
    get_session_log_path,
    log_to_gui,
    start_session_log,
)


def test_start_session_log_creates_timestamped_txt(tmp_path):
    path = start_session_log(str(tmp_path))
    try:
        assert path is not None
        assert os.path.isfile(path)
        assert os.path.dirname(path) == str(tmp_path)
        assert re.match(r"Availity_Log_\d{8}_\d{6}(?:_\d+)?\.txt$", os.path.basename(path))
        text = open(path, encoding="utf-8").read()
        assert "Availity Automation Activity Log" in text
        assert get_session_log_path() == path
    finally:
        close_session_log()


def test_log_to_gui_appends_timestamped_lines(tmp_path):
    path = start_session_log(str(tmp_path))
    try:
        log_to_gui("Opening Edge...\n", "info")
        log_to_gui("Search failed\nline two\n", "error")
        with open(path, encoding="utf-8") as fh:
            body = fh.read()
        lines = [ln for ln in body.splitlines() if ln.startswith("[")]
        assert lines[0].endswith("Opening Edge...")
        assert re.match(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] Opening Edge\.\.\.$", lines[0])
        assert lines[1].endswith("Search failed")
        assert lines[2].endswith("line two")
    finally:
        close_session_log()


def test_second_open_edge_starts_new_log_file(tmp_path):
    first = start_session_log(str(tmp_path))
    log_to_gui("first session\n", "info")
    close_session_log()

    second = start_session_log(str(tmp_path))
    try:
        log_to_gui("second session\n", "info")
        assert second is not None
        first_text = open(first, encoding="utf-8").read()
        second_text = open(second, encoding="utf-8").read()
        assert "first session" in first_text
        assert "second session" not in first_text
        assert "second session" in second_text
        assert "first session" not in second_text
    finally:
        close_session_log()
