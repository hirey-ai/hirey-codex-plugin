#!/usr/bin/env python3
"""Foreground Codex inbox checks with local, recoverable reminder decisions."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
import tempfile
import time
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from hi_hook_client import HookClient

try:
    import fcntl
except ImportError:
    fcntl = None
try:
    import msvcrt
except ImportError:
    msvcrt = None

SCHEMA_STATE = 'hirey.codex.inbox_reminder.state.v5'
SCHEMA_CONFIG = 'hirey.codex.inbox_reminder.config.v1'
SCHEMA_JOURNAL = 'hirey.codex.inbox_reminder.journal.v1'
CONFIG_FILENAME = 'inbox_reminder_config.json'
STATE_FILENAME = 'inbox_reminder_state.v5.json'
JOURNAL_FILENAME = 'inbox_reminder_journal.jsonl'
LOCK_FILENAME = 'inbox_reminder_state.lock'
MAX_INPUT_BYTES = 262144
MAX_STATE_BYTES = 1048576
MAX_CONFIG_BYTES = 16384
MAX_JOURNAL_BYTES = 16 * 1048576
ROTATE_JOURNAL_BYTES = 8 * 1048576
RECENT_JOURNAL_RECORDS = 100
LOCK_WAIT_SECONDS = 2.0
SUPPORTED_EVENTS = ('SessionStart', 'UserPromptSubmit')
BINARY_OPEN_FLAG = getattr(os, 'O_BINARY', 0)


class LocalError(Exception):
    def __init__(self, stage, code):
        self.stage, self.code = stage, code
        super().__init__(code)


def _set_private_file_mode(fd):
    if os.name != 'nt':
        os.fchmod(fd, 0o600)


class _StateLock:
    def __init__(self, path):
        self.path, self.fd, self.locked = path, None, False

    def __enter__(self):
        if self.path.is_symlink():
            return self
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR | BINARY_OPEN_FLAG | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            _set_private_file_mode(self.fd)
            if os.fstat(self.fd).st_size == 0:
                os.write(self.fd, b'\0')
            deadline = time.monotonic() + LOCK_WAIT_SECONDS
            while time.monotonic() < deadline:
                try:
                    if fcntl:
                        fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    elif msvcrt:
                        os.lseek(self.fd, 0, os.SEEK_SET)
                        msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
                    else:
                        return self
                    self.locked = True
                    break
                except OSError:
                    time.sleep(0.05)
        except OSError:
            pass
        return self

    def __exit__(self, *_):
        if self.fd is not None:
            try:
                if self.locked and fcntl:
                    fcntl.flock(self.fd, fcntl.LOCK_UN)
                elif self.locked and msvcrt:
                    os.lseek(self.fd, 0, os.SEEK_SET)
                    msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
            finally:
                os.close(self.fd)


def _now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _epoch(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (AttributeError, TypeError, ValueError):
        return None


def _ref(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]


def _instance_helper():
    root = Path(__file__).resolve().parent.parent
    path = root / 'skills/hi-instance/scripts/hi_instance.py'
    if not path.is_file():
        path = root.parent.parent / 'skills/hi-instance/scripts/hi_instance.py'
    spec = importlib.util.spec_from_file_location('hi_instance_hook', path)
    if spec is None or spec.loader is None:
        raise LocalError('identity', 'instance_helper_unavailable')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _installed_data_dir():
    script = Path(__file__).absolute()
    hooks = script.parent
    version = hooks.parent
    plugin = version.parent
    marketplace = plugin.parent
    cache = marketplace.parent
    plugins = cache.parent
    if not (hooks.name == 'hooks' and cache.name == 'cache' and plugins.name == 'plugins'):
        return None
    components = (plugins, cache, marketplace, plugin, version, hooks, script)
    if any(part.is_symlink() for part in components):
        raise LocalError('local_write', 'unsafe_plugin_path')
    return plugins / 'data' / f'{plugin.name}-{marketplace.name}'


def _data_dir(hook=False):
    raw = os.environ.get('PLUGIN_DATA', '').strip()
    try:
        installed_path = _installed_data_dir()
        if installed_path is not None:
            if hook and raw:
                supplied = Path(raw)
                if (not supplied.is_absolute()
                        or os.path.normcase(os.path.abspath(supplied))
                        != os.path.normcase(os.path.abspath(installed_path))):
                    return None
            # Agent commands cannot rely on the Hook's PLUGIN_DATA environment.
            path = installed_path
        elif raw:
            path = Path(raw)
            if not path.is_absolute():
                return None
        else:
            path = Path(_instance_helper().host_dir('codex')) / 'inbox-reminder'
    except (OSError, ValueError, LocalError):
        return None
    try:
        current = path
        while True:
            if current.is_symlink():
                return None
            if current.parent == current:
                break
            current = current.parent
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Refuse symlinked path components as well as a symlink at the final directory.
        current = path
        while True:
            if current.is_symlink():
                return None
            if current.parent == current:
                break
            current = current.parent
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            return None
        os.chmod(path, 0o700)
        return path
    except OSError:
        return None


def _read_json(path, limit):
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise LocalError('local_write', 'unsafe_local_file')
        with open(path, 'rb') as handle:
            raw = handle.read(limit + 1)
        if len(raw) > limit:
            raise LocalError('local_write', 'oversize_local_file')
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError('not an object')
        return value
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, ValueError) as exc:
        raise LocalError('local_write', 'invalid_local_file') from exc


def _atomic_state(path, state):
    if path.is_symlink():
        raise LocalError('local_write', 'unsafe_state_file')
    raw = (json.dumps(state, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')
    if len(raw) > MAX_STATE_BYTES:
        raise LocalError('local_write', 'state_limit')
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.inbox-state-')
    try:
        with os.fdopen(fd, 'wb') as handle:
            _set_private_file_mode(handle.fileno())
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    except OSError as exc:
        raise LocalError('local_write', 'state_write_failed') from exc
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _initial_state():
    return {'schema': SCHEMA_STATE, 'installations': {}, 'sessions': {}, 'scopes': {}}


def _initial_scope():
    return {'last_seen_checkpoint': None, 'last_success_at': None,
            'pending_batch': None, 'last_cold_hint_id': None, 'last_any_hint_at': None}


def _read_journal(path):
    state = _initial_state()
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_JOURNAL_BYTES:
            raise LocalError('local_write', 'unsafe_journal')
        with open(path, 'rb') as handle:
            raw = handle.read(MAX_JOURNAL_BYTES + 1)
    except FileNotFoundError:
        raw = b''
    except OSError as exc:
        raise LocalError('local_write', 'journal_read_failed') from exc
    if len(raw) > MAX_JOURNAL_BYTES:
        raise LocalError('local_write', 'journal_limit')
    end = raw.rfind(b'\n') + 1
    fragment = end != len(raw)
    records = []
    last_record_id = None
    for line in raw[:end].splitlines():
        try:
            record = json.loads(line)
            if not isinstance(record, dict) or record.get('schema') != SCHEMA_JOURNAL:
                raise ValueError('journal schema')
        except (UnicodeError, ValueError) as exc:
            raise LocalError('local_write', 'invalid_journal') from exc
        last_record_id = record.get('record_id')
        event, data = record.get('event'), record.get('data')
        if not isinstance(data, dict):
            raise LocalError('local_write', 'invalid_journal')
        if event == 'state_snapshot':
            snapshot_state, recent = data.get('state'), data.get('recent_records')
            if (not isinstance(snapshot_state, dict) or snapshot_state.get('schema') != SCHEMA_STATE
                    or not isinstance(recent, list) or len(recent) > RECENT_JOURNAL_RECORDS
                    or any(not isinstance(item, dict) or item.get('schema') != SCHEMA_JOURNAL
                           or item.get('event') == 'state_snapshot' for item in recent)):
                raise LocalError('local_write', 'invalid_journal_snapshot')
            state = deepcopy(snapshot_state)
            records = recent[:]
            continue
        records.append(record)
        if event == 'session_registered':
            state['sessions'][record['session_ref']] = {'registered_at': data['registered_at'], 'silent': False}
        elif event == 'session_silence_changed':
            state['sessions'][record['session_ref']] = data['session_state_after']
        elif event == 'check_started':
            state['installations'][data['installation_ref']] = {'last_request_at': data['requested_at']}
        if 'scope_state_after' in data:
            state['scopes'][record['scope_ref']] = data['scope_state_after']
    return state, records, raw, end, fragment, last_record_id


def _replay(data_dir):
    path = data_dir / JOURNAL_FILENAME
    state_path = data_dir / STATE_FILENAME
    state, records, _, end, fragment, last_record_id = _read_journal(path)
    if state_path.is_symlink():
        raise LocalError('local_write', 'unsafe_state_file')
    try:
        saved = _read_json(state_path, MAX_STATE_BYTES)
    except LocalError as exc:
        if exc.code == 'unsafe_local_file':
            raise
        saved = None
    if saved is not None and saved.get('schema') != SCHEMA_STATE:
        saved = None
    recovered = fragment or (saved is not None and saved != state) or (saved is None and last_record_id is not None)
    if fragment:
        with open(path, 'r+b') as handle:
            handle.truncate(end)
            handle.flush()
            os.fsync(handle.fileno())
    if recovered:
        _append(path, 'state_recovered', None, None, {'reason': 'journal_fragment' if fragment else 'state_mismatch',
                'last_replayed_record_id': last_record_id})
        _atomic_state(state_path, state)
    return state, records


def _write_new_file(path, raw):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | BINARY_OPEN_FLAG | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        _set_private_file_mode(fd)
        view = memoryview(raw)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError('short file write')
            view = view[count:]
        os.fsync(fd)
    except OSError:
        os.unlink(path)
        raise
    finally:
        os.close(fd)


def _sync_directory(path):
    if os.name == 'nt':
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _rotate_journal(path):
    state, records, raw, end, fragment, last_record_id = _read_journal(path)
    if fragment or end != len(raw):
        raise LocalError('local_write', 'journal_fragment')
    archive = path.with_name(f'inbox_reminder_journal.{uuid.uuid4().hex}.jsonl')
    snapshot = {'schema': SCHEMA_JOURNAL, 'record_id': uuid.uuid4().hex,
                'event': 'state_snapshot', 'at': _now(), 'scope_ref': None, 'session_ref': None,
                'data': {'state': state, 'recent_records': records[-RECENT_JOURNAL_RECORDS:],
                         'last_archived_record_id': last_record_id}}
    snapshot_raw = (json.dumps(snapshot, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')
    if len(snapshot_raw) > MAX_JOURNAL_BYTES:
        raise LocalError('local_write', 'journal_snapshot_limit')
    temporary = path.with_name(f'.inbox-journal-{uuid.uuid4().hex}')
    try:
        _write_new_file(archive, raw)
        _sync_directory(path.parent)
        _write_new_file(temporary, snapshot_raw)
        os.replace(temporary, path)
        _sync_directory(path.parent)
    except OSError as exc:
        raise LocalError('local_write', 'journal_rotation_failed') from exc
    finally:
        if temporary.exists():
            temporary.unlink()


def _archived_hint_records(data_dir, hint_id):
    found = {}
    for path in sorted(data_dir.glob('inbox_reminder_journal.*.jsonl')):
        if path.is_symlink():
            raise LocalError('local_write', 'unsafe_journal_archive')
        _, records, _, _, fragment, _ = _read_journal(path)
        if fragment:
            raise LocalError('local_write', 'invalid_journal_archive')
        for record in records:
            if ((record['event'] == 'hint_offered' and record['record_id'] == hint_id)
                    or (record['event'] in ('reminder_attempted', 'reminder_skipped')
                        and record['data'].get('hint_id') == hint_id)):
                found[record['record_id']] = record
    return list(found.values())


def _append(path, event, scope_ref, session_ref, data, record_id=None):
    record = {'schema': SCHEMA_JOURNAL, 'record_id': record_id or uuid.uuid4().hex, 'event': event,
              'at': _now(), 'scope_ref': scope_ref, 'session_ref': session_ref, 'data': data}
    raw = (json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')
    if path.is_symlink():
        raise LocalError('local_write', 'unsafe_journal')
    try:
        if path.exists() and path.stat().st_size + len(raw) >= ROTATE_JOURNAL_BYTES:
            _rotate_journal(path)
        fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY | BINARY_OPEN_FLAG | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size + len(raw) > MAX_JOURNAL_BYTES:
                raise LocalError('local_write', 'journal_limit')
            _set_private_file_mode(fd)
            if os.write(fd, raw) != len(raw):
                raise LocalError('local_write', 'short_journal_write')
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        raise LocalError('local_write', 'journal_write_failed') from exc
    return record


def _write_event(data_dir, state, event, scope, session, data):
    record = _append(data_dir / JOURNAL_FILENAME, event, scope, session, data)
    _atomic_state(data_dir / STATE_FILENAME, state)
    return record


def _preflight(data_dir):
    for name in (JOURNAL_FILENAME, STATE_FILENAME):
        path = data_dir / name
        if path.is_symlink():
            raise LocalError('local_write', 'unsafe_local_file')
        if path.exists() and (not path.is_file() or not os.access(path, os.W_OK)):
            raise LocalError('local_write', 'unwritable_local_file')
    fd, name = tempfile.mkstemp(dir=data_dir, prefix='.inbox-check-')
    os.close(fd)
    os.unlink(name)
    fd = os.open(data_dir / JOURNAL_FILENAME, os.O_CREAT | os.O_APPEND | os.O_WRONLY | BINARY_OPEN_FLAG | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    os.close(fd)


def _config(data_dir):
    result = {'enabled': True, 'min_seconds': 300, 'hot_repeat_seconds': 7200,
              'cold_repeat_seconds': 86400, 'max_hot_hints': 3}
    raw = _read_json(data_dir / CONFIG_FILENAME, MAX_CONFIG_BYTES)
    if raw and raw.get('schema') not in (None, SCHEMA_CONFIG):
        raise LocalError('config', 'config_schema')
    if raw:
        if isinstance(raw.get('enabled'), bool):
            result['enabled'] = raw['enabled']
        result['min_seconds'] = _old_int(raw.get('min_seconds'), 300)
        for name, low, high in (('hot_repeat_seconds', 60, 604800),
                                ('cold_repeat_seconds', 60, 604800), ('max_hot_hints', 1, 10)):
            if name in raw:
                value = raw[name]
                if type(value) is not int or not low <= value <= high:
                    raise LocalError('config', 'invalid_' + name)
                result[name] = value
    enabled = os.environ.get('HIREY_CODEX_INBOX_REMINDER', '').strip().lower()
    if enabled in ('1', 'on', 'true', 'yes', 'enabled'):
        result['enabled'] = True
    elif enabled in ('0', 'off', 'false', 'no', 'disabled'):
        result['enabled'] = False
    result['min_seconds'] = _old_int(os.environ.get('HIREY_CODEX_INBOX_REMINDER_SECONDS'), result['min_seconds'])
    return result


def _old_int(value, default):
    try:
        if isinstance(value, bool) or value is None:
            return default
        return max(1, min(86400, int(value)))
    except (TypeError, ValueError):
        return default


def _installation():
    helper = _instance_helper()
    try:
        item = helper.read_installation('codex')
    except helper.InstanceError as exc:
        raise LocalError('identity', exc.code) from exc
    reference = item.get('installation_ref') if isinstance(item, dict) else None
    if not isinstance(reference, str) or not reference:
        raise LocalError('identity', 'installation_missing')
    return reference


def _scope(status):
    if not isinstance(status, dict) or status.get('identity_bound') is not True or status.get('instance_status') != 'bound':
        raise LocalError('identity', 'instance_binding_required')
    instance = status.get('current_instance')
    profile = status.get('profile_key')
    if (not isinstance(instance, dict) or instance.get('status') != 'active'
            or not isinstance(profile, str) or not re.fullmatch(r'[0-9a-f]{64}', profile)
            or not isinstance(instance.get('instance_id'), str)
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', instance['instance_id'])):
        raise LocalError('identity', 'invalid_status')
    return _ref(json.dumps([profile, instance['instance_id']], separators=(',', ':')))


def _latest(result):
    if result.get('scope') != 'authorized_workspaces' or not isinstance(result.get('checkpoint'), str) or not result['checkpoint'] or len(result['checkpoint']) > 8192:
        raise LocalError('contract', 'invalid_checkpoint_receipt')
    if _epoch(result.get('checked_at')) is None or _epoch(result.get('window_started_at')) is None:
        raise LocalError('contract', 'invalid_check_time')
    for name in ('messages', 'contact_requests'):
        counts = result.get(name)
        if not isinstance(counts, dict):
            raise LocalError('contract', 'missing_counts')
        for key in ('new_count', 'historical_count', 'total_count'):
            if type(counts.get(key)) is not int or counts[key] < 0:
                raise LocalError('contract', 'invalid_counts')
        if counts['new_count'] + counts['historical_count'] != counts['total_count']:
            raise LocalError('contract', 'inconsistent_counts')
    messages = result['messages']
    if (type(messages.get('new_sender_count')) is not int or messages['new_sender_count'] < 0
            or type(messages.get('new_system_count')) is not int or messages['new_system_count'] < 0
            or not isinstance(messages.get('senders'), list) or len(messages['senders']) > 3
            or messages['new_sender_count'] + messages['new_system_count'] > messages['new_count']):
        raise LocalError('contract', 'invalid_senders')
    for sender in messages['senders']:
        if (not isinstance(sender, dict) or not isinstance(sender.get('person_id'), str)
                or sender.get('display_name') is not None and not isinstance(sender.get('display_name'), str)
                or type(sender.get('message_count')) is not int or sender['message_count'] < 1):
            raise LocalError('contract', 'invalid_sender')
    snapshots = result.get('snapshots')
    if not isinstance(snapshots, list) or len(snapshots) > 10:
        raise LocalError('contract', 'invalid_snapshots')
    for item in snapshots:
        if (not isinstance(item, dict) or item.get('kind') not in ('message', 'contact_request')
                or not isinstance(item.get('item_ref'), str) or not item['item_ref']
                or _epoch(item.get('occurred_at')) is None):
            raise LocalError('contract', 'invalid_snapshot')
        person = item.get('sender') if item['kind'] == 'message' else item.get('requester')
        if person is not None and (not isinstance(person, dict)
                or not isinstance(person.get('person_id'), str)
                or person.get('display_name') is not None and not isinstance(person.get('display_name'), str)):
            raise LocalError('contract', 'invalid_snapshot_person')
        if item['kind'] == 'contact_request' and item.get('request_kind') not in ('contact_intent', 'introduction_ready'):
            raise LocalError('contract', 'invalid_request_kind')
    return result


def _summary(result, kind):
    m, c = result['messages'], result['contact_requests']
    parts = []
    if kind == 'hot':
        if m['new_count']:
            parts.append(f"新增未读消息 {m['new_count']} 条")
        if c['new_count']:
            parts.append(f"新增待处理联系申请 {c['new_count']} 条")
        names = [re.sub(r'[\r\n\t\[\]<>`]', '', s['display_name'])[:24] for s in m['senders'] if s['display_name']]
        if names:
            parts.append('来自' + '、'.join(names))
        elif m['new_sender_count']:
            parts.append(f"来自 {m['new_sender_count']} 位联系人")
        if m['historical_count'] or c['historical_count']:
            parts.append(f"另有历史未读 {m['historical_count']} 条、待处理申请 {c['historical_count']} 条")
    else:
        parts.append(f"历史未读消息 {m['total_count']} 条、待处理联系申请 {c['total_count']} 条")
    return 'HiRey：' + '；'.join(parts) + '。'


def _emit(event, record, kind, summary, journal):
    script = str(Path(__file__).resolve())
    context = (f"[HiRey 收件箱状态]\n{('新增批次' if kind == 'hot' else '历史积压')}：{summary}\n"
               f"hint_id：{record['record_id']}；日志：{journal}\n"
               f"结合本轮请求决定是否简短提醒。最终答复前登记：python3 {json.dumps(script)} "
               f"record --hint-id {record['record_id']} --outcome attempted --summary '<拟输出摘要>'；"
               f"若跳过，改用 --outcome skipped --summary ''。记录只表示尝试，不表示用户已看到。")
    return {'hookSpecificOutput': {'hookEventName': event, 'additionalContext': context}}


def _fail(data_dir, state, session, stage, code):
    try:
        _write_event(data_dir, state, 'check_failed', None, session,
                     {'stage': stage, 'error_code': str(code)[:120]})
    except (OSError, LocalError):
        pass


def run(event, client_factory=HookClient, installation=_installation):
    if not isinstance(event, dict) or os.environ.get('HIREY_CODEX_INBOX_HOOK_ACTIVE') == '1':
        return {}
    name, sid = event.get('hook_event_name'), event.get('session_id')
    if (name not in SUPPORTED_EVENTS or name == 'SessionStart' and event.get('source') not in ('startup', 'resume')
            or not isinstance(sid, str) or not 0 < len(sid) <= 256):
        return {}
    data_dir = _data_dir(hook=True)
    if data_dir is None:
        return {}
    session = _ref(sid)
    with _StateLock(data_dir / LOCK_FILENAME) as lock:
        if not lock.locked:
            return {}
        try:
            state, records = _replay(data_dir)
            if session not in state['sessions']:
                registered = _now()
                state['sessions'][session] = {'registered_at': registered, 'silent': False}
                _write_event(data_dir, state, 'session_registered', None, session, {'registered_at': registered})
            if state['sessions'][session]['silent']:
                return {}
            config = _config(data_dir)
        except LocalError as exc:
            _fail(data_dir, locals().get('state', _initial_state()), session, exc.stage, exc.code)
            return {}
        if name == 'UserPromptSubmit' and (any(s.get('pending_batch') for s in state['scopes'].values())
                or any(r['event'] == 'reminder_attempted' and (_epoch(r['at']) or 0) > time.time() - config['cold_repeat_seconds'] for r in records[-100:])):
            try:
                _write_event(data_dir, state, 'user_activity', None, session,
                             {'hook_event': name, 'pending_batch_id': next((s['pending_batch']['batch_id'] for s in state['scopes'].values() if s.get('pending_batch')), None)})
            except LocalError:
                return {}
        if not config['enabled']:
            return {}
        try:
            installation_id = installation()
            install = _ref(installation_id)
        except (OSError, LocalError, ValueError) as exc:
            _fail(data_dir, state, session, 'identity', getattr(exc, 'code', 'installation_missing'))
            return {}
        last = state['installations'].get(install, {}).get('last_request_at')
        if last is not None and (_epoch(last) is None or time.time() - _epoch(last) < config['min_seconds']):
            return {}
        try:
            _preflight(data_dir)
        except (OSError, LocalError) as exc:
            _fail(data_dir, state, session, 'local_write', getattr(exc, 'code', 'preflight_failed'))
            return {}
        requested = _now()
        state['installations'][install] = {'last_request_at': requested}
        try:
            _write_event(data_dir, state, 'check_started', None, session,
                         {'installation_ref': install, 'requested_at': requested})
        except LocalError:
            return {}
        scope = None
        try:
            with client_factory() as client:
                scope = _scope(client.status(installation_id))
                before = deepcopy(state['scopes'].get(scope, _initial_scope()))
                batch = before['pending_batch']
                checkpoint = batch['from_checkpoint'] if batch else before['last_seen_checkpoint']
                result = _latest(client.latest(checkpoint))
        except LocalError as exc:
            _fail(data_dir, state, session, exc.stage, exc.code)
            return {}
        except (OSError, ValueError, TimeoutError, KeyError) as exc:
            _fail(data_dir, state, session, 'request', type(exc).__name__)
            return {}
        after = deepcopy(before)
        after['last_seen_checkpoint'] = result['checkpoint']
        after['last_success_at'] = result['checked_at']
        hot = result['messages']['new_count'] + result['contact_requests']['new_count']
        if batch and not hot:
            after['pending_batch'] = None
            close_reason = 'no_hot_items'
        elif batch:
            batch = deepcopy(batch)
            batch['latest_checkpoint'] = result['checkpoint']
            after['pending_batch'] = batch
            close_reason = None
        elif hot:
            after['pending_batch'] = {'batch_id': uuid.uuid4().hex, 'from_checkpoint': before['last_seen_checkpoint'],
                                      'latest_checkpoint': result['checkpoint'], 'created_at': result['checked_at'],
                                      'last_hint_id': None, 'last_hint_at': None, 'hint_count': 0, 'attempt_count': 0}
            close_reason = None
        else:
            close_reason = None
        current_batch = after['pending_batch']
        if (current_batch and current_batch['hint_count'] >= config['max_hot_hints']
                and current_batch['last_hint_at'] is not None
                and _epoch(current_batch['last_hint_at']) is not None
                and time.time() - _epoch(current_batch['last_hint_at']) >= config['hot_repeat_seconds']):
            after['pending_batch'] = None
            close_reason = 'hot_hint_limit'
        state['scopes'][scope] = after
        data = {'input_checkpoint': checkpoint, 'checkpoint': result['checkpoint'], 'checked_at': result['checked_at'],
                'messages': {k: result['messages'][k] for k in ('new_count', 'historical_count', 'total_count', 'new_sender_count', 'new_system_count')},
                'contact_requests': {k: result['contact_requests'][k] for k in ('new_count', 'historical_count', 'total_count')},
                'senders': result['messages']['senders'],
                'snapshots': [{'kind': s['kind'], 'item_ref': s['item_ref'], 'occurred_at': s['occurred_at']} for s in result['snapshots']],
                'scope_state_after': deepcopy(after)}
        if close_reason:
            data['close_reason'] = close_reason
        try:
            _write_event(data_dir, state, 'check_succeeded', scope, session, data)
        except LocalError:
            return {}
        kind = None
        batch = after['pending_batch']
        elapsed = lambda at, seconds: at is None or (_epoch(at) is not None and time.time() - _epoch(at) >= seconds)
        if batch and hot:
            if batch['hint_count'] < config['max_hot_hints'] and elapsed(batch['last_hint_at'], config['hot_repeat_seconds']):
                kind = 'hot'
        elif result['messages']['total_count'] or result['contact_requests']['total_count']:
            if elapsed(after['last_any_hint_at'], config['cold_repeat_seconds']):
                kind = 'cold'
        if kind is None:
            return {}
        summary = _summary(result, kind)
        hint_id = uuid.uuid4().hex
        hint_at = _now()
        if kind == 'hot':
            batch = after['pending_batch']
            batch['last_hint_id'] = hint_id
            batch['last_hint_at'] = hint_at
            batch['hint_count'] += 1
            batch_id = batch['batch_id']
        else:
            after['last_cold_hint_id'] = hint_id
            batch_id = None
        after['last_any_hint_at'] = hint_at
        state['scopes'][scope] = after
        hint_data = {'hint_kind': kind, 'batch_id': batch_id,
                     'message_new_count': result['messages']['new_count'],
                     'message_historical_count': result['messages']['historical_count'],
                     'contact_request_new_count': result['contact_requests']['new_count'],
                     'contact_request_historical_count': result['contact_requests']['historical_count'],
                     'summary': summary, 'scope_state_after': deepcopy(after)}
        try:
            record = _append(data_dir / JOURNAL_FILENAME, 'hint_offered', scope, session, hint_data, hint_id)
            _atomic_state(data_dir / STATE_FILENAME, state)
        except LocalError:
            return {}
        return _emit(name, record, kind, summary, data_dir / JOURNAL_FILENAME)


def _command_error(code):
    return {'ok': False, 'error_code': code}


def _command_session():
    sid = os.environ.get('CODEX_SESSION_ID')
    if not sid or len(sid) > 256:
        raise LocalError('identity', 'codex_session_id_required')
    return _ref(sid)


def command_record(hint_id, outcome, summary):
    if outcome not in ('attempted', 'skipped') or not isinstance(hint_id, str) or not re.fullmatch(r'[0-9a-f]{32}', hint_id):
        raise LocalError('contract', 'invalid_record_command')
    if outcome == 'skipped' and summary or len(summary) > 500:
        raise LocalError('contract', 'invalid_summary')
    session = _command_session()
    data_dir = _data_dir()
    if data_dir is None:
        raise LocalError('local_write', 'data_dir_unavailable')
    with _StateLock(data_dir / LOCK_FILENAME) as lock:
        if not lock.locked:
            raise LocalError('local_write', 'lock_unavailable')
        state, records = _replay(data_dir)
        hints = [r for r in records if r['event'] == 'hint_offered' and r['record_id'] == hint_id]
        archived = _archived_hint_records(data_dir, hint_id) if not hints else []
        hints.extend(r for r in archived if r['event'] == 'hint_offered')
        if len(hints) != 1 or hints[0]['session_ref'] != session:
            raise LocalError('identity', 'hint_session_mismatch')
        hint = hints[0]
        event = 'reminder_attempted' if outcome == 'attempted' else 'reminder_skipped'
        prior = [r for r in records if r['event'] in ('reminder_attempted', 'reminder_skipped') and r['data'].get('hint_id') == hint_id]
        if not prior and not archived:
            archived = _archived_hint_records(data_dir, hint_id)
        prior.extend(r for r in archived if r['event'] in ('reminder_attempted', 'reminder_skipped'))
        if prior:
            if prior[0]['event'] != event or prior[0]['data'].get('summary', '') != summary:
                raise LocalError('contract', 'hint_already_recorded_differently')
            return {'ok': True, 'record_id': prior[0]['record_id'], 'already_recorded': True}
        scope = hint['scope_ref']
        after = deepcopy(state['scopes'][scope])
        batch_id = hint['data']['batch_id']
        if batch_id is None:
            if after['last_cold_hint_id'] != hint_id:
                raise LocalError('contract', 'hint_not_current')
        elif not after['pending_batch'] or after['pending_batch']['batch_id'] != batch_id or after['pending_batch']['last_hint_id'] != hint_id:
            raise LocalError('contract', 'hint_not_current')
        if outcome == 'attempted' and batch_id is not None:
            after['pending_batch']['attempt_count'] += 1
            after['pending_batch'] = None
        state['scopes'][scope] = after
        data = {'hint_id': hint_id, 'batch_id': batch_id, 'scope_state_after': deepcopy(after)}
        if outcome == 'attempted':
            data['summary'] = summary
        record = _write_event(data_dir, state, event, scope, session, data)
        return {'ok': True, 'record_id': record['record_id'], 'already_recorded': False}


def command_silence(value):
    if value not in ('on', 'off'):
        raise LocalError('contract', 'invalid_silence_value')
    session = _command_session()
    data_dir = _data_dir()
    if data_dir is None:
        raise LocalError('local_write', 'data_dir_unavailable')
    with _StateLock(data_dir / LOCK_FILENAME) as lock:
        if not lock.locked:
            raise LocalError('local_write', 'lock_unavailable')
        state, _ = _replay(data_dir)
        if session not in state['sessions']:
            raise LocalError('identity', 'session_not_registered')
        after = deepcopy(state['sessions'][session])
        after['silent'] = value == 'on'
        state['sessions'][session] = after
        _write_event(data_dir, state, 'session_silence_changed', None, session,
                     {'session_ref': session, 'silent': after['silent'], 'session_state_after': after})
        return {'ok': True, 'session_ref': session, 'silent': after['silent']}


def main():
    try:
        if len(sys.argv) > 1:
            import argparse
            parser = argparse.ArgumentParser()
            sub = parser.add_subparsers(dest='command', required=True)
            rec = sub.add_parser('record')
            rec.add_argument('--hint-id', required=True)
            rec.add_argument('--outcome', required=True)
            rec.add_argument('--summary', required=True)
            silence = sub.add_parser('session-silence')
            silence.add_argument('--value', required=True)
            args = parser.parse_args()
            result = command_record(args.hint_id, args.outcome, args.summary) if args.command == 'record' else command_silence(args.value)
        else:
            raw = sys.stdin.read(MAX_INPUT_BYTES + 1)
            result = run(json.loads(raw)) if len(raw) <= MAX_INPUT_BYTES and raw.strip() else {}
    except LocalError as exc:
        result = _command_error(exc.code) if len(sys.argv) > 1 else {}
    except Exception:
        result = _command_error('unexpected_error') if len(sys.argv) > 1 else {}
    sys.stdout.write(json.dumps(result, ensure_ascii=True) + '\n')
    return 0 if not (len(sys.argv) > 1 and result.get('ok') is False) else 1


if __name__ == '__main__':
    sys.exit(main())
