"""
Thread-safe job lifecycle, cancellation, and subprocess tracking manager.
Allows cancelling active pipeline runs mid-flight and terminating subprocesses (e.g. ffmpeg).
"""

from __future__ import annotations

import logging
import subprocess
import threading
from typing import Any

logger = logging.getLogger(__name__)


class JobStoppedError(Exception):
    """Raised when a job has been stopped/cancelled by the user."""
    pass


_lock = threading.RLock()
_active_runs: dict[int, str] = {}  # run_id -> topic
_stopped_runs: set[int] = set()
_stopped_topics: set[str] = set()
_stop_all_flag: bool = False
_run_processes: dict[int, set[subprocess.Popen[Any]]] = {}

# Thread-local storage to associate the current thread with a run_id
_tls = threading.local()


def set_current_run_id(run_id: int | None) -> None:
    """Set the active run_id for the current thread."""
    _tls.current_run_id = run_id


def get_current_run_id() -> int | None:
    """Get the active run_id for the current thread, if any."""
    return getattr(_tls, "current_run_id", None)


def clear_stop(run_id: int | None = None, topic: str | None = None) -> None:
    """Clear stop requests for a run or topic (e.g. when starting a retry)."""
    with _lock:
        if run_id is not None:
            _stopped_runs.discard(run_id)
        if topic is not None:
            _stopped_topics.discard(topic.strip().lower())


def register_run(run_id: int, topic: str = "") -> None:
    """Register an active run."""
    norm_topic = topic.strip().lower()
    with _lock:
        _active_runs[run_id] = norm_topic
        _run_processes.setdefault(run_id, set())
    set_current_run_id(run_id)
    logger.info("job_control: Registered active run_id=%d for topic=%r", run_id, topic)


def unregister_run(run_id: int) -> None:
    """Unregister an active run and clean up its process list and stop flag."""
    with _lock:
        _active_runs.pop(run_id, None)
        _run_processes.pop(run_id, None)
        _stopped_runs.discard(run_id)
    if get_current_run_id() == run_id:
        set_current_run_id(None)
    logger.debug("job_control: Unregistered run_id=%d", run_id)


def register_process(run_id: int, proc: subprocess.Popen[Any]) -> None:
    """Track a subprocess (e.g. ffmpeg) created during run_id."""
    with _lock:
        if is_stop_requested(run_id):
            try:
                proc.terminate()
            except Exception:
                pass
            return
        if run_id in _run_processes:
            _run_processes[run_id].add(proc)


def unregister_process(run_id: int, proc: subprocess.Popen[Any]) -> None:
    """Untrack a completed subprocess."""
    with _lock:
        if run_id in _run_processes:
            _run_processes[run_id].discard(proc)


def terminate_processes_for_run(run_id: int) -> None:
    """Terminate all active subprocesses tracked for run_id."""
    with _lock:
        procs = list(_run_processes.get(run_id, []))
    for proc in procs:
        try:
            if proc.poll() is None:
                logger.info("job_control: Terminating subprocess PID %d for run_id=%d", proc.pid, run_id)
                proc.terminate()
        except Exception as err:
            logger.warning("job_control: Error terminating subprocess: %s", err)


def request_stop_run(run_id: int) -> bool:
    """
    Request cancellation of a specific run_id.
    Returns True if the run was actively tracked or marked stopped.
    """
    with _lock:
        _stopped_runs.add(run_id)
        terminate_processes_for_run(run_id)
        logger.info("job_control: Requested stop for run_id=%d", run_id)
        return True


def request_stop_topic(topic: str) -> list[int]:
    """
    Request cancellation of all active runs for the given topic.
    Returns the list of run_ids that were marked stopped.
    """
    norm = topic.strip().lower()
    with _lock:
        _stopped_topics.add(norm)
        matched_runs = [
            r_id for r_id, top in _active_runs.items() if top == norm
        ]
        for r_id in matched_runs:
            _stopped_runs.add(r_id)
            terminate_processes_for_run(r_id)
    logger.info("job_control: Requested stop for topic=%r, affected run_ids=%s", topic, matched_runs)
    return matched_runs


def request_stop_all() -> list[int]:
    """
    Request cancellation of all active runs.
    Returns the list of run_ids that were marked stopped.
    """
    global _stop_all_flag
    with _lock:
        _stop_all_flag = True
        all_runs = list(_active_runs.keys())
        for r_id in all_runs:
            _stopped_runs.add(r_id)
            terminate_processes_for_run(r_id)
    logger.info("job_control: Requested stop all runs, affected run_ids=%s", all_runs)
    return all_runs


def reset_stop_all() -> None:
    """Reset the global stop_all flag."""
    global _stop_all_flag
    with _lock:
        _stop_all_flag = False


def is_stop_requested(run_id: int | None = None, topic: str | None = None) -> bool:
    """Check if cancellation has been requested for this run, topic, or globally."""
    if _stop_all_flag:
        return True
    with _lock:
        if run_id is not None and run_id in _stopped_runs:
            return True
        if topic is not None and topic.strip().lower() in _stopped_topics:
            return True
        if run_id is not None and run_id in _active_runs:
            top = _active_runs[run_id]
            if top in _stopped_topics:
                return True
    return False


def check_stop(run_id: int | None = None, topic: str | None = None) -> None:
    """
    Raise JobStoppedError if cancellation has been requested.
    If run_id is not specified, uses current thread's active run_id.
    """
    if run_id is None:
        run_id = get_current_run_id()
    if is_stop_requested(run_id, topic):
        raise JobStoppedError(
            f"Job execution cancelled by stop request (run_id={run_id}, topic={topic})"
        )


# Hook subprocess.Popen to automatically capture any subprocess created during a tracked run
_orig_popen_init = subprocess.Popen.__init__


def _hooked_popen_init(self: subprocess.Popen[Any], *args: Any, **kwargs: Any) -> None:
    _orig_popen_init(self, *args, **kwargs)
    cur_run_id = get_current_run_id()
    if cur_run_id is not None:
        register_process(cur_run_id, self)


subprocess.Popen.__init__ = _hooked_popen_init  # type: ignore[method-assign]
