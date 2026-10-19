"""Isolated Calibre 9.15 native-job history smoke test and optional CPU profile."""
import cProfile
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace

from calibre.constants import numeric_version
from calibre.gui2 import Application
from calibre.gui2.threaded_jobs import ThreadedJob
from calibre.utils.ipc.job import ParallelJob
from calibre.utils.logging import GUILog

assert tuple(numeric_version) == (9, 15, 0)
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from plugins.calibre_umcp_plugin.bridge import CalibreRpcBridge
from plugins.calibre_umcp_plugin.task_history import TaskHistory

app = Application([])
root = Path(tempfile.mkdtemp(prefix='native-history-'))
path = root / 'tasks.sqlite'
manager = SimpleNamespace(jobs=[])
gui = SimpleNamespace(job_manager=manager, current_db=SimpleNamespace(library_path='/private/library'))
bridge = CalibreRpcBridge(gui, task_history_path=str(path), email_config_adapter=lambda: SimpleNamespace(relay_password=''))
try:
    native = ThreadedJob('email', 'Mail issue to private@example.com', lambda **kw: None,
                         (), {}, None)
    native.log('SMTP accepted message', 'private@example.com')
    native.start_time, native.duration = time.time(), 0.1
    native.update()
    manager.jobs.append(native)
    bridge.capture_native_tasks()
    row = bridge.task_history.list()['items'][0]
    assert row['status'] == 'completed'
    assert 'private@example.com' not in json.dumps(row)
    assert 'SMTP accepted message' in bridge.task_history.log(row['task_id'])['text']
    assert row['delivery_confirmed'] is False
    native._cleanup()  # Calibre consolidates its live GUILog to JSON.
    bridge.capture_native_tasks()
    assert 'SMTP accepted message' in bridge.task_history.log(row['task_id'])['text']

    log_path = root / 'parallel.log'
    log_path.write_text('Downloaded issue for private@example.com\n')
    parallel = ParallelJob('arbitrary', 'Scheduled news', None)
    parallel.log_path = str(log_path)
    parallel.start_time, parallel.duration = time.time(), 1.
    parallel.update()
    manager.jobs.append(parallel)
    bridge.capture_native_tasks()
    rows = bridge.task_history.list()['items']
    assert len(rows) == 2
    parallel_row = next(r for r in rows if r['description'] == 'Scheduled news')
    assert 'Downloaded issue' in bridge.task_history.log(parallel_row['task_id'])['text']
    assert 'private@example.com' not in path.read_bytes().decode('latin1')
    bridge.close()
    reopened = TaskHistory(path, bridge._redact_native_text)
    assert reopened.list()['total'] == 2
    reopened.close()
    print('PASS native ThreadedJob/GUILog, consolidated JSON, ParallelJob log, redaction and restart')

    from calibre.gui2.jobs import JobManager
    live_manager = JobManager()
    gui.job_manager = live_manager
    live_bridge = CalibreRpcBridge(gui, task_history_path=str(root / 'live.sqlite'))
    events = []
    def capture(*args):
        events.append(True)
        live_bridge.capture_native_tasks()
    live_manager.job_added.connect(capture)
    live_manager.job_done.connect(capture)
    def work(**kwargs):
        kwargs['log']('Synthetic automatic email task accepted')
    task = ThreadedJob('email', 'Synthetic automatic email', work, (), {}, lambda job: None)
    try:
        live_manager.run_threaded_job(task)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            app.processEvents()
            live_manager.update()
            rows = live_bridge.task_history.list()['items']
            if rows and rows[0]['status'] == 'completed':
                break
            time.sleep(0.01)
        assert rows and rows[0]['status'] == 'completed'
        assert len(events) >= 2
        assert 'accepted' in live_bridge.task_history.log(rows[0]['task_id'])['text']
        print('PASS real JobManager signals and asynchronous native ThreadedJob completion')
    finally:
        live_bridge.close()
        live_manager.timer.stop()
        live_manager.server.close()
        live_manager.threaded_server.close()
        gui.job_manager = manager

    if os.environ.get('NATIVE_HISTORY_PROFILE'):
        store = TaskHistory(root / 'profile.sqlite', bridge._redact_native_text)
        jobs = []
        for i in range(2000):
            job = ThreadedJob('email', 'Mail issue', lambda **kw: None, (), {}, None)
            job.log('Message accepted; synthetic workload ' + 'x' * 1024)
            job.start_time, job.duration = time.time(), 1.
            jobs.append(job)
        store.observe(jobs)
        profile = cProfile.Profile()
        begin = time.monotonic()
        profile.enable()
        for _ in range(3):
            store.observe(jobs)
        profile.disable()
        elapsed = time.monotonic() - begin
        profile.dump_stats(os.environ['NATIVE_HISTORY_PROFILE'])
        print(json.dumps({'records': 2000, 'unchanged_observations': 3, 'elapsed_seconds': elapsed}))
        store.close()
finally:
    bridge.close()
    import shutil
    shutil.rmtree(root)
