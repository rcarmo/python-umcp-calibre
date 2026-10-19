"""Redacted native GUI task history, independent of bridge job/audit records.

The caller supplies a config-directory database path and its public-text redactor.
Call observe on the GUI thread with a JobManager snapshot; never update native jobs.
No Calibre imports are needed. None disables disk persistence (not live history).
"""

import io
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import threading
import time
import uuid
import weakref
from pathlib import Path


LOG_BYTES = 32 * 1024
READ_BYTES = 1024 * 1024
TEXT_BYTES = 4096
PAYLOAD_BYTES = 32768
STATUSES = frozenset(('queued', 'running', 'completed', 'failed', 'cancelled', 'interrupted'))
TASK_ID = re.compile(r'native-task:[0-9a-f]{32}:[1-9][0-9]{0,19}\Z')
CAVEAT = ('Native task success does not confirm email delivery or SMTP acceptance. '
          'No book association is inferred from task descriptions.')
FIELDS = frozenset(('task_id', 'created_at', 'start_time', 'duration', 'description',
                    'name', 'native_id', 'status', 'percent', 'bridge_job_id',
                    'delivery_confirmed', 'caveat', 'log_available', 'truncated'))
SCHEMA = [('task_id', 'TEXT', 1, 1), ('created_at', 'REAL', 1, 0),
          ('metadata', 'TEXT', 1, 0), ('log', 'TEXT', 1, 0)]


def _integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise ValueError('Invalid ' + label)
    return value


def _number(value, maximum=1e15):
    if type(value) not in (int, float) or not 0 <= value <= maximum or not math.isfinite(value):
        return None
    return float(value)


def _clip(text, size):
    return text.encode('utf-8', 'replace')[:size].decode('utf-8', 'ignore')


def _attr(job, name, default=None):
    try:
        return getattr(job, name, default)
    except Exception:
        return default


class TaskHistory:
    def __init__(self, path, redact, retention_days=30, max_records=2000):
        _integer(retention_days, 1, 36500, 'retention_days')
        _integer(max_records, 1, 2000, 'max_records')
        if not callable(redact):
            raise ValueError('Invalid redactor')
        self.retention_days, self.max_records = retention_days, max_records
        self._redact = redact
        self._path = Path(path) if path is not None else None
        self._session = uuid.uuid4().hex
        self._counter = 0
        self._records = {}
        self._identities = {}
        self._text_cache = {}
        self._db = None
        self._persisted = {}
        self._error = None
        self._lock = threading.RLock()
        if self._path is not None:
            self._open()

    def _safe(self, value, size=TEXT_BYTES):
        if not isinstance(value, str):
            return ''
        if len(value) > READ_BYTES:
            return "<redacted>"
        # Redact before output truncation; never publish raw text on failure.
        try:
            value = self._redact(value)
            if not isinstance(value, str):
                return '<redacted>'
            return _clip(value, size)
        except Exception:
            return '<redacted>'

    def invalidate_redaction_cache(self):
        with self._lock:
            self._text_cache.clear()

    def _observed_text(self, task_id, field, value, size=TEXT_BYTES):
        # Keep digests and already-redacted output only; never cache raw secrets.
        if not isinstance(value, str) or len(value) > READ_BYTES:
            return self._safe(value, size)
        digest = hashlib.sha256(value.encode('utf-8', 'replace')).digest()
        key = (task_id, field)
        previous = self._text_cache.get(key)
        fingerprint = (digest, id(self._redact))
        if previous is not None and previous[0] == fingerprint:
            return previous[1]
        safe = self._safe(value, size)
        if len(safe.encode('utf-8')) <= LOG_BYTES:
            self._text_cache[key] = (fingerprint, safe)
        else:
            self._text_cache.pop(key, None)
        return safe

    def _disable(self):
        self._error = 'Task history persistence unavailable'
        if self._db is not None:
            try:
                self._db.close()
            except Exception:
                pass
        self._db = None

    def _open(self):
        try:
            exists = self._path.exists()
            if exists:
                info = self._path.lstat()
                if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                    raise ValueError('Unsafe database')
            else:
                # Parent must already be the caller-selected Calibre config directory.
                fd = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
            db = sqlite3.connect(self._path.absolute().as_uri() + '?mode=rw', uri=True,
                                 timeout=0.2, check_same_thread=False)
            self._db = db
            db.execute('PRAGMA trusted_schema=OFF')
            db.execute('PRAGMA mmap_size=0')
            db.execute('PRAGMA cache_size=-1024')
            if exists:
                if db.execute('PRAGMA journal_mode').fetchone()[0].lower() != 'delete':
                    raise ValueError('Unsupported journal')
                objects = db.execute("SELECT type, substr(name, 1, 100) FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 3").fetchall()
                if objects != [('table', 'tasks')] or db.execute('PRAGMA user_version').fetchone()[0] != 1:
                    raise ValueError('Unsupported schema')
                columns = [(r[1], r[2], r[3], r[5]) for r in db.execute('PRAGMA table_info(tasks)')]
                if columns != SCHEMA:
                    raise ValueError('Unsupported schema')
                # Reject oversized or non-text fields before materialising JSON/logs.
                invalid = db.execute('''SELECT 1 FROM tasks WHERE
                    typeof(task_id) != 'text' OR length(CAST(task_id AS BLOB)) > 100 OR
                    typeof(created_at) NOT IN ('real', 'integer') OR
                    typeof(metadata) != 'text' OR length(CAST(metadata AS BLOB)) > ? OR
                    typeof(log) != 'text' OR length(CAST(log AS BLOB)) > ? LIMIT 1''',
                                     (PAYLOAD_BYTES, LOG_BYTES)).fetchone()
                if invalid:
                    raise ValueError('Invalid history')
                loaded = {}
                for task_id, created, payload, log in db.execute(
                        'SELECT task_id, created_at, metadata, log FROM tasks ORDER BY created_at DESC, task_id DESC LIMIT ?',
                        (2001,)):
                    if len(loaded) == 2000:
                        raise ValueError("History exceeds schema capacity")
                    record = json.loads(payload)
                    self._validate(record, task_id, created)
                    if record['status'] in ('queued', 'running'):
                        record['status'] = 'interrupted'
                    loaded[task_id] = (self._public(record), self._safe(log, LOG_BYTES))
                self._records = loaded
            else:
                db.execute('PRAGMA journal_mode=DELETE')
                with db:
                    db.execute('''CREATE TABLE tasks (task_id TEXT NOT NULL PRIMARY KEY,
                        created_at REAL NOT NULL, metadata TEXT NOT NULL, log TEXT NOT NULL)''')
                    db.execute('PRAGMA user_version=1')
            db.execute('PRAGMA secure_delete=ON')
            self._prune()
            self._persist()
        except Exception:
            self._disable()

    @staticmethod
    def _validate(record, task_id, created):
        if not isinstance(record, dict) or set(record) != FIELDS:
            raise ValueError('Invalid metadata')
        if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id) or record['task_id'] != task_id:
            raise ValueError('Invalid task id')
        if _number(created) is None or type(record['created_at']) not in (int, float) or record['created_at'] != created:
            raise ValueError('Invalid timestamp')
        for key in ('start_time', 'duration', 'percent'):
            if record[key] is not None and _number(record[key], 100 if key == 'percent' else 1e15) is None:
                raise ValueError('Invalid number')
        if not isinstance(record['status'], str) or record['status'] not in STATUSES:
            raise ValueError('Invalid status')
        for key in ('description', 'name', 'caveat'):
            if not isinstance(record[key], str) or len(record[key].encode('utf-8')) > TEXT_BYTES:
                raise ValueError('Invalid text')
        for key in ('native_id', 'bridge_job_id'):
            if record[key] is not None and (not isinstance(record[key], str) or len(record[key].encode('utf-8')) > TEXT_BYTES):
                raise ValueError('Invalid identifier')
        if record['delivery_confirmed'] is not False:
            raise ValueError('Invalid delivery claim')
        for key in ('log_available', 'truncated'):
            if type(record[key]) is not bool:
                raise ValueError('Invalid log flag')

    def _persist(self):
        if self._db is None:
            return
        try:
            # Commit only changed rows and pruning, in one transaction.
            with self._db:
                for task_id in self._persisted.keys() - self._records.keys():
                    self._db.execute('DELETE FROM tasks WHERE task_id = ?', (task_id,))
                for task_id, (record, log) in self._records.items():
                    if self._persisted.get(task_id) == (record, log):
                        continue
                    public = self._public(record)
                    payload = json.dumps(public, ensure_ascii=False, allow_nan=False)
                    if len(payload.encode('utf-8')) > PAYLOAD_BYTES:
                        raise ValueError('Metadata too large')
                    self._db.execute('INSERT OR REPLACE INTO tasks VALUES (?, ?, ?, ?)',
                                     (record['task_id'], record['created_at'], payload, self._safe(log, LOG_BYTES)))
            self._persisted = dict(self._records)
        except Exception:
            self._disable()

    def _prune(self):
        cutoff = time.time() - self.retention_days * 86400
        newest = sorted(self._records, key=lambda k: (self._records[k][0]['created_at'], k), reverse=True)
        keep = set([k for k in newest if self._records[k][0]['created_at'] >= cutoff][:self.max_records])
        self._records = {k: v for k, v in self._records.items() if k in keep}
        self._identities = {k: v for k, v in self._identities.items() if v[1] in keep and v[0]() is not None}
        self._text_cache = {k: v for k, v in self._text_cache.items() if k[0] in keep}

    def _identity(self, job):
        old = self._identities.get(id(job))
        if old is not None and old[0]() is job and old[1] in self._records:
            return old[1]
        self._counter += 1
        task_id = 'native-task:{}:{}'.format(self._session, self._counter)
        try:
            ref = weakref.ref(job)
        except TypeError:
            ref = lambda: job  # Bounded by retained records, not a permanent registry.
        self._identities[id(job)] = (ref, task_id)
        return task_id

    def _read_log(self, job):
        """Never invoke details/plain_text properties that assemble unlimited logs."""
        try:
            stored = _attr(job, '__dict__', {})
            if isinstance(stored, dict) and isinstance(stored.get('details'), str):
                text = stored['details']
                return text[-READ_BYTES:], True, len(text) > READ_BYTES
            path = _attr(job, 'consolidated_log')
            if isinstance(path, (str, os.PathLike)):
                with open(path, 'rb') as stream:
                    if os.fstat(stream.fileno()).st_size > READ_BYTES:
                        return '', False, True
                    data = stream.read(READ_BYTES + 1)
                if len(data) > READ_BYTES:
                    return '', False, True
                parsed = json.loads(data.decode('utf-8'))
                if isinstance(parsed, list) and len(parsed) == 2 and isinstance(parsed[1], str):
                    return parsed[1], True, False
                return '', False, False
            log = _attr(job, 'log')
            if log is not None:
                # Pinned 9.15 GUILog stores plain-text fragments in outputs[0].
                outputs = _attr(log, 'outputs')
                if isinstance(outputs, list) and outputs:
                    fragments = _attr(outputs[0], 'plain_text')
                    if isinstance(fragments, list):
                        parts, count = [], 0
                        for fragment in reversed(fragments):
                            if not isinstance(fragment, str):
                                break
                            parts.append(fragment[-(READ_BYTES - count):])
                            count += len(parts[-1])
                            if count >= READ_BYTES:
                                return ''.join(reversed(parts)), True, True
                        return ''.join(reversed(parts)), True, False
                # Some GUI log implementations expose a live StringIO directly.
                stream = log if isinstance(log, io.StringIO) else _attr(log, '__dict__', {}).get('plain_text')
                if isinstance(stream, io.StringIO):
                    position = stream.tell()
                    try:
                        end = stream.seek(0, io.SEEK_END)
                        stream.seek(max(0, end - READ_BYTES))
                        return stream.read(READ_BYTES), True, end > READ_BYTES
                    finally:
                        stream.seek(position)
                return '', False, False
            # BaseJob/ParallelJob opens a separate handle for log_file.
            stream = _attr(job, 'log_file')
            if stream is not None:
                try:
                    end = stream.seek(0, io.SEEK_END)
                    stream.seek(max(0, end - READ_BYTES))
                    data = stream.read(READ_BYTES)
                    if isinstance(data, bytes):
                        return data.decode('utf-8', 'replace'), True, end > READ_BYTES
                    if isinstance(data, str):
                        return data, True, end > READ_BYTES
                finally:
                    stream.close()
        except Exception:
            pass
        return '', False, False

    def observe(self, jobs, bridge_jobs=None):
        """Observe a native JobManager snapshot; None means manager unavailable.

        Only an explicit identity match can link a bridge id. No book/result data
        or job arguments are inspected. Native completion is not email delivery.
        """
        with self._lock:
            now = time.time()
            before = dict(self._records)
            links = {} if bridge_jobs is None else bridge_jobs
            if not hasattr(links, 'items'):
                raise ValueError('Invalid bridge mapping')
            for job in (() if jobs is None else jobs):
                if job is None:
                    continue
                task_id = self._identity(job)
                previous = self._records.get(task_id)
                start = _number(_attr(job, 'start_time'))
                duration = _number(_attr(job, 'duration'))
                if _attr(job, 'killed') is True:
                    status = 'cancelled'
                elif _attr(job, 'is_finished') is True or duration is not None:
                    status = 'failed' if _attr(job, 'failed') is True else 'completed'
                elif _attr(job, 'is_running') is True or start is not None:
                    status = 'running'
                else:
                    status = 'queued'
                native_id = _attr(job, 'id')
                if type(native_id) is int:
                    native_id = str(native_id) if native_id.bit_length() <= 128 else None
                bridge_id = previous[0]['bridge_job_id'] if previous else None
                for key, value in links.items():
                    if value is job and isinstance(key, str):
                        bridge_id = self._safe(key)
                        break
                raw, available, truncated = self._read_log(job)
                # Bound redactor input but retain up to 32 KiB AFTER redaction.
                safe = self._observed_text(task_id, 'log', raw, READ_BYTES)
                encoded = safe.encode('utf-8')
                truncated = truncated or len(encoded) > LOG_BYTES
                text = encoded[-LOG_BYTES:].decode('utf-8', 'ignore')
                if not available and previous:
                    text = previous[1]
                    available = previous[0]['log_available']
                    truncated = truncated or previous[0]['truncated']
                record = dict(task_id=task_id, created_at=previous[0]['created_at'] if previous else now,
                              start_time=start, duration=duration, description=self._observed_text(task_id, 'description', _attr(job, 'description')),
                              name=self._observed_text(task_id, 'name', _attr(job, 'name')), native_id=self._observed_text(task_id, 'native_id', native_id) if isinstance(native_id, str) else None,
                              status=status, percent=_number(_attr(job, 'percent'), 100), bridge_job_id=bridge_id,
                              delivery_confirmed=False, caveat=CAVEAT, log_available=available, truncated=truncated)
                self._records[task_id] = (record, text)
                # Bound strong fallback references even for huge snapshots.
                if len(self._records) > self.max_records:
                    self._prune()
            self._prune()
            if self._records != before:
                self._persist()

    def _public(self, record):
        result = dict(record)
        for key in ('description', 'name', 'native_id', 'bridge_job_id'):
            if result[key] is not None:
                result[key] = self._safe(result[key])
        result['delivery_confirmed'] = False
        result['caveat'] = CAVEAT
        return result

    def list(self, limit=20, offset=0, status='', query=''):
        _integer(limit, 1, 200, 'limit')
        _integer(offset, 0, 2147483647, 'offset')
        if not isinstance(status, str) or (status and status not in STATUSES):
            raise ValueError('Invalid status')
        if not isinstance(query, str) or len(query) > 1024:
            raise ValueError('Invalid query')
        with self._lock:
            self._prune()
            records = [self._public(r) for r, _ in self._records.values()]
            records = [r for r in records if (not status or r['status'] == status) and
                       (not query or query.casefold() in (r['description'] + '\n' + r['name']).casefold())]
            records.sort(key=lambda r: (r['created_at'], r['task_id']), reverse=True)
            items = records[offset:offset + limit]
            following = offset + len(items)
            return dict(items=items, total=len(records), offset=offset, limit=limit,
                        next_offset=following if following < len(records) else None)

    def _lookup(self, task_id):
        if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id) or task_id not in self._records:
            raise ValueError('Task not found')
        return self._records[task_id]

    def get(self, task_id):
        with self._lock:
            self._prune()
            return self._public(self._lookup(task_id)[0])

    def log(self, task_id, offset=0, limit=4000):
        """Offsets and limits are Unicode character counts, not encoded bytes."""
        _integer(offset, 0, 2147483647, 'offset')
        _integer(limit, 1, 16000, 'limit')
        with self._lock:
            self._prune()
            record, text = self._lookup(task_id)
            text = self._safe(text, LOG_BYTES)
            page = text[offset:offset + limit]
            following = offset + len(page)
            return dict(task_id=task_id, text=page, offset=offset, limit=limit, total=len(text),
                        next_offset=following if following < len(text) else None,
                        log_available=record['log_available'], truncated=record['truncated'])

    get_log = log

    def status_info(self):
        with self._lock:
            return dict(retention_days=self.retention_days, max_records=self.max_records,
                        max_log_bytes=LOG_BYTES, persistence_configured=self._path is not None,
                        persistence_available=self._db is not None, persistence_error=self._error)

    def close(self):
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None
