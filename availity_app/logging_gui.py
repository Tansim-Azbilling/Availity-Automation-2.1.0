"""Thread-safe append to the Tk activity log (batched for responsiveness)."""
import atexit
import os
import threading
from datetime import datetime

from availity_app import state

_LOG_FLUSH_MS = 100
_LOG_MAX_LINES = 2000

_buffer_lock = threading.Lock()
_buffer = []
_flush_scheduled = False

_file_lock = threading.Lock()
_log_file = None
_log_path = None


def _trim_log_widget():
    """Drop oldest lines when the log exceeds the cap."""
    if state.log_text is None:
        return
    line_count = int(state.log_text.index('end-1c').split('.')[0])
    if line_count > _LOG_MAX_LINES:
        excess = line_count - _LOG_MAX_LINES
        state.log_text.delete('1.0', f'{excess + 1}.0')


def _flush_buffer():
    global _flush_scheduled, _buffer
    try:
        with _buffer_lock:
            if not _buffer:
                _flush_scheduled = False
                return
            pending = _buffer
            _buffer = []

        if state.log_text is None:
            with _buffer_lock:
                _flush_scheduled = False
            return

        state.log_text.config(state="normal")
        for message, tag in pending:
            state.log_text.insert("end", message, tag)
        _trim_log_widget()
        state.log_text.see("end")
        state.log_text.config(state="disabled")

        with _buffer_lock:
            if _buffer:
                _flush_scheduled = True
                if state.root is not None:
                    state.root.after(_LOG_FLUSH_MS, _flush_buffer)
            else:
                _flush_scheduled = False
    except Exception:
        raise


def _schedule_deferred_flush():
    if state.root is not None:
        state.root.after(_LOG_FLUSH_MS, _flush_buffer)


def _unique_log_path(output_folder, stamp):
    """Return a non-colliding Availity_Log_<stamp>.txt path."""
    path = os.path.join(output_folder, f"Availity_Log_{stamp}.txt")
    if not os.path.exists(path):
        return path
    for suffix in range(2, 100):
        candidate = os.path.join(output_folder, f"Availity_Log_{stamp}_{suffix}.txt")
        if not os.path.exists(candidate):
            return candidate
    return os.path.join(output_folder, f"Availity_Log_{stamp}_{os.getpid()}.txt")


def close_session_log():
    """Flush and close the current session log file, if any."""
    global _log_file, _log_path
    with _file_lock:
        if _log_file is not None:
            try:
                _log_file.flush()
                _log_file.close()
            except OSError:
                pass
            _log_file = None
        _log_path = None


def start_session_log(output_folder):
    """Create a new timestamped .txt log in output_folder.

    Called when Open Edge starts a session. Later log_to_gui() lines are
    appended until the next start_session_log() or process exit.
    Returns the log path, or None if the file could not be created.
    """
    global _log_file, _log_path
    close_session_log()
    if not output_folder:
        return None
    handle = None
    try:
        os.makedirs(output_folder, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = _unique_log_path(output_folder, stamp)
        handle = open(path, "w", encoding="utf-8", errors="replace")
        started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        handle.write("# Availity Automation Activity Log\n")
        handle.write(f"# Started: {started}\n")
        handle.write(f"# File: {os.path.basename(path)}\n")
        handle.write("\n")
        handle.flush()
    except OSError:
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
        return None
    with _file_lock:
        _log_file = handle
        _log_path = path
    return path


def get_session_log_path():
    """Return the current session log path, or None."""
    return _log_path


def _write_session_log(message):
    """Mirror one Activity Log message into the session .txt file."""
    if not message:
        return
    with _file_lock:
        if _log_file is None:
            return
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = str(message).splitlines() or [""]
        try:
            for line in lines:
                _log_file.write(f"[{ts}] {line}\n")
            _log_file.flush()
        except OSError:
            pass


def log_to_gui(message, tag="info"):
    global _flush_scheduled
    _write_session_log(message)
    with _buffer_lock:
        _buffer.append((message, tag))
        if _flush_scheduled:
            return
        _flush_scheduled = True
    if state.root is not None:
        state.root.after(0, _schedule_deferred_flush)


atexit.register(close_session_log)
