"""Source-only fixtures for native history; no Calibre GUI/runtime dependency."""

import gc
import importlib.util
import io
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


# Avoid executing the plugin package initializer (which requires Calibre).
SOURCE = Path(__file__).resolve().parents[1] / 'plugins/calibre_umcp_plugin/task_history.py'
SPEC = importlib.util.spec_from_file_location('native_task_history_under_test', SOURCE)
history_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(history_module)
TaskHistory = history_module.TaskHistory


def redact(text):
    text = text.replace('private-token', '<redacted>')
    text = re.sub(r'\b[^\s@]+@[^\s@]+\.[^\s@]+', '<recipient>', text)
    return re.sub(r'(?<![A-Za-z0-9._-])/(?:[^\s;:,]+/?)+', '<path>', text)


class FakeJob:
    def __init__(self, description='Convert book', **changes):
        self.description = description
        self.name = 'convert'
        self.id = 7
        self.start_time = None
        self.duration = None
        self.is_running = False
        self.is_finished = False
        self.failed = False
        self.killed = False
        self.percent = 0
        self.details = 'A native log'
        self.__dict__.update(changes)

    @property
    def result(self):
        raise AssertionError('result must never be accessed')

    @property
    def args(self):
        raise AssertionError('args must never be accessed')

    @property
    def kwargs(self):
        raise AssertionError('kwargs must never be accessed')


class DangerousDetailsJob(FakeJob):
    def __init__(self, **changes):
        super().__init__(**changes)
        del self.__dict__['details']

    @property
    def details(self):
        raise AssertionError('unbounded details property invoked')

    @details.setter
    def details(self, value):
        self.__dict__['details'] = value


class TaskHistoryTests(unittest.TestCase):
    def setUp(self):
        # Fixtures live below the project-aligned scratch root when executed.
        resolver = Path(__file__).resolve().parents[1] / 'scripts' / 'project-tmp.sh'
        scratch = Path(subprocess.check_output([str(resolver), 'root'], text=True).strip()) / 'tests'
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='task-history-', dir=scratch)
        self.root = Path(self.temp.name)
        self.path = self.root / 'history.sqlite'
        self.histories = []

    def tearDown(self):
        for history in self.histories:
            history.close()
        self.temp.cleanup()

    def history(self, path=None, **kwargs):
        result = TaskHistory(path, kwargs.pop('redact', redact), **kwargs)
        self.histories.append(result)
        return result

    def one(self, history):
        return history.list()['items'][0]

    def test_disabled_and_missing_manager(self):
        h = self.history()
        h.observe(None)
        self.assertEqual(h.list()['items'], [])
        self.assertEqual(h.status_info()['persistence_configured'], False)
        self.assertIsNone(h.status_info()['persistence_error'])
        h.observe([FakeJob()])
        self.assertEqual(h.list()['total'], 1)

    def test_native_status_transitions_and_no_mutations(self):
        h = self.history()
        job = FakeJob()
        snapshot = dict(vars(job))
        h.observe([job])
        task_id = self.one(h)['task_id']
        self.assertEqual(vars(job), snapshot)
        self.assertEqual(h.get(task_id)['status'], 'queued')
        self.assertRegex(task_id, r'^native-task:[0-9a-f]{32}:1$')
        job.start_time, job.is_running = 100, True
        h.observe([job])
        self.assertEqual(h.get(task_id)['status'], 'running')
        job.duration, job.is_finished, job.is_running = 0.0, True, False
        h.observe([job])
        self.assertEqual(h.get(task_id)['status'], 'completed')
        job.failed = True
        h.observe([job])
        self.assertEqual(h.get(task_id)['status'], 'failed')
        job.killed = True
        h.observe([job])
        self.assertEqual(h.get(task_id)['status'], 'cancelled')
        self.assertEqual(h.list()['total'], 1)

    def test_first_observed_is_not_native_start_time(self):
        h = self.history()
        job = FakeJob(start_time=123, is_running=True)
        with patch.object(history_module.time, 'time', return_value=100000):
            h.observe([job])
            row = self.one(h)
            self.assertEqual(row['created_at'], 100000)
            self.assertEqual(row['start_time'], 123)
        with patch.object(history_module.time, 'time', return_value=100010):
            h.observe([job])
            self.assertEqual(h.get(row['task_id'])['created_at'], 100000)

    def test_identity_is_not_native_id_or_equality(self):
        class EqualJob(FakeJob):
            def __eq__(self, other):
                return True
            __hash__ = None
        h = self.history()
        first, second = EqualJob(), EqualJob()
        h.observe([first, second])
        self.assertEqual(h.list()['total'], 2)
        old_ids = {r['task_id'] for r in h.list()['items']}
        del first, second
        gc.collect()
        h.observe([EqualJob()])
        self.assertEqual(h.list()['total'], 3)
        self.assertTrue(old_ids < {r['task_id'] for r in h.list()['items']})

    def test_strong_fallback_bounded(self):
        class SlotJob:
            __slots__ = ('description',)
            def __init__(self):
                self.description = 'native'
        h = self.history(max_records=2)
        jobs = [SlotJob() for _ in range(5)]
        h.observe(jobs)
        self.assertLessEqual(len(h._identities), 2)
        self.assertEqual(h.list()['total'], 2)

    def test_only_identity_bridge_link_and_email_caveat(self):
        h = self.history()
        job = FakeJob(description='Email book 42 to person@example.com', is_finished=True)
        h.observe([job], {'bridge-wrong': FakeJob(), 'bridge-private-token': job})
        row = self.one(h)
        self.assertEqual(row['bridge_job_id'], 'bridge-<redacted>')
        self.assertFalse(row['delivery_confirmed'])
        self.assertIn('SMTP acceptance', row['caveat'])
        self.assertEqual(row['status'], 'completed')
        self.assertFalse(any('book_id' in key or 'recipient' in key for key in row))
        h.observe([job])
        self.assertEqual(self.one(h)['bridge_job_id'], row['bridge_job_id'])

    def test_metadata_and_log_redaction_isolated(self):
        h = self.history()
        job = FakeJob(description='private-token /secret/books', details='private-token person@example.com /secret/file')
        h.observe([job])
        row = self.one(h)
        self.assertNotIn('details', row)
        self.assertNotIn('text', row)
        self.assertNotIn('private-token', json.dumps(row))
        text = h.get_log(row['task_id'])['text']
        self.assertEqual(text, '<redacted> <recipient> <path>')
        row['description'] = 'changed'
        self.assertNotEqual(h.get(row['task_id'])['description'], 'changed')

    def test_log_byte_bound_and_character_pagination(self):
        h = self.history()
        h.observe([FakeJob(details='é' * 100000)])
        row = self.one(h)
        page = h.log(row['task_id'], limit=16000)
        self.assertTrue(page['truncated'])
        self.assertTrue(page['log_available'])
        self.assertLessEqual(len(h._records[row['task_id']][1].encode('utf-8')), 32768)
        self.assertEqual(page['next_offset'], 16000)
        last = h.log(row['task_id'], offset=page['next_offset'])
        self.assertIsNone(last['next_offset'])
        self.assertEqual(len(page['text'] + last['text']), page['total'])

    def test_redactor_failure_never_falls_back_to_raw(self):
        def broken(value):
            raise RuntimeError('private-token /secret')
        h = self.history(redact=broken)
        h.observe([FakeJob(description='private-token', details='private-token')])
        self.assertNotIn('private-token', json.dumps(h.list()))
        self.assertEqual(h.log(self.one(h)['task_id'])['text'], '<redacted>')

    def test_live_gui_fragments_without_unbounded_properties(self):
        class Output:
            plain_text = ['private-token\n', 'x' * (2 * history_module.READ_BYTES)]
        class GUILog:
            outputs = [Output()]
            @property
            def plain_text(self):
                raise AssertionError('unbounded join invoked')
        job = DangerousDetailsJob(log=GUILog())
        h = self.history()
        h.observe([job])
        row = self.one(h)
        self.assertTrue(row['log_available'])
        self.assertTrue(row['truncated'])
        self.assertEqual(len(h.log(row['task_id'], limit=16000)['text']), 16000)

    def test_live_stringio_cursor_preserved(self):
        stream = io.StringIO('private-token native log')
        stream.seek(3)
        job = DangerousDetailsJob(log=types.SimpleNamespace(plain_text=stream))
        h = self.history()
        h.observe([job])
        self.assertEqual(stream.tell(), 3)
        self.assertFalse(stream.closed)
        self.assertEqual(h.log(self.one(h)['task_id'])['text'], '<redacted> native log')

    def test_consolidated_json_bounded_and_redacted(self):
        path = self.root / 'native-log.json'
        path.write_text(json.dumps(['<html>not public</html>', 'private-token plain log']))
        job = DangerousDetailsJob(consolidated_log=str(path), log=None)
        h = self.history()
        h.observe([job])
        row = self.one(h)
        self.assertEqual(h.log(row['task_id'])['text'], '<redacted> plain log')
        path.write_bytes(b'x' * (history_module.READ_BYTES + 1))
        other = DangerousDetailsJob(consolidated_log=str(path), log=None)
        h.observe([other])
        other_row = next(r for r in h.list()['items'] if r['task_id'] != row['task_id'])
        self.assertFalse(other_row['log_available'])
        self.assertTrue(other_row['truncated'])
        self.assertEqual(h.log(other_row['task_id'])['text'], '')

    def test_parallel_seek_tail_and_close_separate_handle(self):
        path = self.root / 'parallel.log'
        path.write_bytes(b'x' * (history_module.READ_BYTES + 200) + b' private-token')
        opened = []
        class Parallel(DangerousDetailsJob):
            @property
            def log_file(self):
                result = path.open('rb')
                opened.append(result)
                return result
        h = self.history()
        h.observe([Parallel()])
        row = self.one(h)
        self.assertTrue(row['truncated'])
        self.assertTrue(opened[0].closed)
        self.assertTrue(h._records[row['task_id']][1].endswith('<redacted>'))

    def test_unavailable_log_and_native_property_errors(self):
        class Broken(DangerousDetailsJob):
            @property
            def log_file(self):
                raise OSError('/private/path')
        h = self.history()
        h.observe([Broken()])
        row = self.one(h)
        self.assertFalse(row['log_available'])
        self.assertEqual(h.log(row['task_id'])['text'], '')

    def test_reopen_interrupts_only_nonterminal_and_new_session_ids(self):
        h = self.history(self.path)
        jobs = [FakeJob(), FakeJob(is_running=True), FakeJob(is_finished=True),
                FakeJob(is_finished=True, failed=True), FakeJob(killed=True)]
        h.observe(jobs)
        old = {r['task_id']: r['status'] for r in h.list()['items']}
        self.assertTrue(h.status_info()['persistence_available'])
        h.close()
        reopened = self.history(self.path)
        for row in reopened.list()['items']:
            expected = 'interrupted' if old[row['task_id']] in ('queued', 'running') else old[row['task_id']]
            self.assertEqual(row['status'], expected)
        reopened.observe([jobs[0]])
        new_ids = {r['task_id'] for r in reopened.list()['items']} - set(old)
        self.assertEqual(len(new_ids), 1)
        self.assertNotIn(h._session, next(iter(new_ids)))

    def test_persistence_redaction_permissions_no_wal(self):
        h = self.history(self.path)
        h.observe([FakeJob(description='private-token person@example.com /secret/books',
                           details='private-token /secret/log')])
        task_id = self.one(h)['task_id']
        h.close()
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(Path(str(self.path) + '-wal').exists())
        self.assertFalse(Path(str(self.path) + '-shm').exists())
        data = self.path.read_bytes()
        self.assertNotIn(b'private-token', data)
        self.assertNotIn(b'/secret/books', data)
        def stronger(text):
            return redact(text).replace('Convert', '<new-policy>')
        other = self.history(self.path, redact=stronger)
        self.assertNotIn('private-token', json.dumps(other.get(task_id)))
        self.assertNotIn('/secret', other.log(task_id)['text'])

    def test_re_redacts_persisted_text_on_load_and_return(self):
        h = self.history(self.path, redact=lambda text: text)
        h.observe([FakeJob(description='private-token', details='private-token')])
        task_id = self.one(h)['task_id']
        h.close()
        reopened = self.history(self.path)
        self.assertEqual(reopened.get(task_id)['description'], '<redacted>')
        self.assertEqual(reopened.log(task_id)['text'], '<redacted>')
        reopened._redact = lambda text: text.replace('<redacted>', '<new-policy>')
        self.assertEqual(reopened.get(task_id)['description'], '<new-policy>')
        self.assertEqual(reopened.log(task_id)['text'], '<new-policy>')

    def test_retention_and_cap_persisted(self):
        h = self.history(self.path, max_records=2)
        with patch.object(history_module.time, 'time', return_value=10000000):
            h.observe([FakeJob(description='old')])
            oldest = self.one(h)['task_id']
        with patch.object(history_module.time, 'time', return_value=10000000 + 31 * 86400):
            h.observe([FakeJob(description='one'), FakeJob(description='two'), FakeJob(description='three')])
            self.assertEqual(h.list()['total'], 2)
            with self.assertRaises(ValueError):
                h.get(oldest)
            h.close()
            other = self.history(self.path, max_records=2)
            self.assertEqual(other.list()['total'], 2)
            self.assertEqual(other.status_info()['retention_days'], 30)

    def test_filters_pagination_and_no_log_search(self):
        h = self.history()
        first = FakeJob(description='Alpha', details='log-only-search')
        second = FakeJob(description='Beta', is_finished=True)
        with patch.object(history_module.time, 'time', return_value=100000):
            h.observe([first])
        with patch.object(history_module.time, 'time', return_value=100001):
            h.observe([second])
            page = h.list(limit=1)
            self.assertEqual(page['items'][0]['description'], 'Beta')
            self.assertEqual(page['next_offset'], 1)
            self.assertEqual(h.list(limit=1, offset=1)['items'][0]['description'], 'Alpha')
            self.assertEqual(h.list(query='ALPHA')['total'], 1)
            self.assertEqual(h.list(status='completed')['total'], 1)
            self.assertEqual(h.list(query='log-only-search')['total'], 0)

    def test_bad_filters_and_missing_task(self):
        h = self.history()
        for params in ({'limit': 0}, {'limit': 201}, {'limit': True}, {'offset': -1},
                       {'offset': 1.0}, {'status': 'success'}, {'status': []},
                       {'query': None}, {'query': 'x' * 1025}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                h.list(**params)
        h.observe([FakeJob()])
        task_id = self.one(h)['task_id']
        for params in ({'limit': 16001}, {'limit': 0}, {'offset': -1}, {'offset': False}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                h.log(task_id, **params)
        for invalid in ('missing', [], None, 'native-task:' + 'a' * 32 + ':1'):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                h.get(invalid)
            with self.assertRaises(ValueError):
                h.log(invalid)

    def test_corrupt_file_preserved_live_operation_generic_error(self):
        original = b'corrupt private-token /private/database'
        self.path.write_bytes(original)
        self.path.chmod(0o600)
        h = self.history(self.path)
        h.observe([FakeJob()])
        self.assertEqual(h.list()['total'], 1)
        self.assertEqual(self.path.read_bytes(), original)
        info = h.status_info()
        self.assertTrue(info['persistence_configured'])
        self.assertFalse(info['persistence_available'])
        self.assertEqual(info['persistence_error'], 'Task history persistence unavailable')
        self.assertNotIn(str(self.path), json.dumps(info))

    def test_unwritable_parent_and_unsafe_permissions(self):
        h = self.history(self.root / 'missing-parent' / 'history.sqlite')
        h.observe([FakeJob()])
        self.assertEqual(h.list()['total'], 1)
        self.assertIsNotNone(h.status_info()['persistence_error'])
        self.path.write_bytes(b'leave me alone')
        self.path.chmod(0o644)
        other = self.history(self.path)
        self.assertIsNotNone(other.status_info()['persistence_error'])
        self.assertEqual(self.path.read_bytes(), b'leave me alone')
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o644)

    def test_schema_and_oversized_payload_rejected_without_rewrite(self):
        h = self.history(self.path)
        h.observe([FakeJob()])
        task_id = self.one(h)['task_id']
        h.close()
        with sqlite3.connect(self.path) as db:
            self.addCleanup(db.close)
            db.execute('UPDATE tasks SET metadata=? WHERE task_id=?', ('x' * 40000, task_id))
        original = self.path.read_bytes()
        other = self.history(self.path)
        self.assertIsNotNone(other.status_info()['persistence_error'])
        self.assertEqual(self.path.read_bytes(), original)
        different = self.root / 'other.sqlite'
        with sqlite3.connect(different) as db:
            self.addCleanup(db.close)
            db.execute('CREATE TABLE unrelated (secret TEXT)')
        different.chmod(0o600)
        original = different.read_bytes()
        self.assertIsNotNone(self.history(different).status_info()['persistence_error'])
        self.assertEqual(different.read_bytes(), original)

    def test_invalid_persisted_status_and_types(self):
        for field, invalid in (('status', 'success'), ('percent', '100'),
                               ('log_available', 1), ('delivery_confirmed', True)):
            path = self.root / (field + '.sqlite')
            h = self.history(path)
            h.observe([FakeJob()])
            h.close()
            with sqlite3.connect(path) as db:
                self.addCleanup(db.close)
                metadata = json.loads(db.execute('SELECT metadata FROM tasks').fetchone()[0])
                metadata[field] = invalid
                db.execute('UPDATE tasks SET metadata=?', (json.dumps(metadata),))
            before = path.read_bytes()
            with self.subTest(field=field):
                broken = self.history(path)
                self.assertIsNotNone(broken.status_info()['persistence_error'])
                self.assertEqual(path.read_bytes(), before)

    def test_atomic_transaction_failure_keeps_disk_history(self):
        h = self.history(self.path)
        h.observe([FakeJob(description='original')])
        original_id = self.one(h)['task_id']
        with sqlite3.connect(self.path) as blocker:
            self.addCleanup(blocker.close)
            blocker.execute('BEGIN EXCLUSIVE')
            h.observe([FakeJob(description='live despite lock')])
            self.assertEqual(h.list()['total'], 2)
            self.assertIsNotNone(h.status_info()['persistence_error'])
            blocker.rollback()
        with sqlite3.connect(self.path) as db:
            self.addCleanup(db.close)
            self.assertEqual(db.execute('SELECT task_id FROM tasks').fetchall(), [(original_id,)])

    def test_unchanged_observations_do_not_write_and_changes_only_update_one_row(self):
        h = self.history(self.path)
        jobs = [FakeJob(description='one'), FakeJob(description='two')]
        h.observe(jobs)
        queries = []
        h._db.set_trace_callback(queries.append)
        h.observe(jobs)
        self.assertFalse(queries)
        jobs[0].description = 'updated'
        h.observe(jobs)
        writes = [q for q in queries if q.startswith('INSERT OR REPLACE')]
        self.assertEqual(len(writes), 1)
        self.assertFalse(any(q == 'DELETE FROM tasks' for q in queries))

    def test_constructor_bounds(self):
        for options in ({'retention_days': 0}, {'retention_days': True},
                        {'max_records': 0}, {'max_records': 2001}, {'redact': None}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.history(**options)


if __name__ == '__main__':
    unittest.main()
