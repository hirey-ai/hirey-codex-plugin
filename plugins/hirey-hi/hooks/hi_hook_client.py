"""Bounded App Server bridge for the foreground inbox check.

Codex owns OAuth. This client never reads its credential store, starts a model
turn, logs in, answers approval/elicitation requests, or changes configuration.
An ephemeral helper thread uses the installed host's configured Hi connection.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

MAX_BYTES = 2 * 1024 * 1024


class HookClient:
    def __init__(self, binary=None, timeout=11.0):
        self.binary = binary
        self.timeout = timeout
        self.proc = None
        self.temp = None
        self.messages = queue.Queue(maxsize=64)
        self.serial = 0
        self.thread_id = None
        self.reader = None

    def __enter__(self):
        try:
            candidates = [self.binary, os.environ.get('CODEX_CLI_PATH'), shutil.which('codex'),
                          '/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex',
                          '/Applications/Codex.app/Contents/Resources/codex']
            binary = next(p for p in candidates if p and Path(p).is_file() and os.access(p, os.X_OK))
            self.deadline = time.monotonic() + self.timeout
            self.temp = tempfile.TemporaryDirectory(prefix='hirey-inbox-hook-')
            env = dict(os.environ, HIREY_CODEX_INBOX_HOOK_ACTIVE='1')
            # Only the isolated helper disables lifecycle callbacks, preventing
            # recursion. The user's config, trust and tool policies are unchanged.
            self.proc = subprocess.Popen([binary, '-c', 'features.hooks=false', 'app-server', '--stdio'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                cwd=self.temp.name, env=env, start_new_session=(os.name == 'posix'), bufsize=0)
            self.reader = threading.Thread(target=self._read, daemon=True)
            self.reader.start()
            self._rpc('initialize', {'clientInfo': {'name': 'hirey_inbox_hook', 'version': '1'},
                                     'capabilities': {'experimentalApi': True}})
            self._send({'method': 'initialized'})
            result = self._rpc('thread/start', {'ephemeral': True, 'cwd': self.temp.name})
            thread = result.get('thread', {})
            if thread.get('ephemeral') is not True or not isinstance(thread.get('id'), str):
                raise ValueError('ephemeral_thread_unavailable')
            self.thread_id = thread['id']
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def _read(self):
        try:
            total = 0
            while True:
                raw = self.proc.stdout.readline(MAX_BYTES + 1)
                total += len(raw)
                if not raw or len(raw) > MAX_BYTES or total > MAX_BYTES:
                    self.messages.put_nowait(None)
                    return
                self.messages.put_nowait(json.loads(raw))
        except (ValueError, OSError, queue.Full):
            try:
                self.messages.put_nowait(None)
            except queue.Full:
                pass

    def _send(self, message):
        raw = (json.dumps(message, separators=(',', ':')) + '\n').encode()
        # Tiny requests are written after each response (plus initialized); a
        # stalled reader cannot fill a pipe before the bounded receive deadline.
        if len(raw) > 2048 or time.monotonic() >= self.deadline:
            raise TimeoutError('hook_deadline')
        self.proc.stdin.write(raw)
        self.proc.stdin.flush()

    def _rpc(self, method, params):
        self.serial += 1
        self._send({'id': self.serial, 'method': method, 'params': params})
        while True:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('hook_deadline')
            try:
                response = self.messages.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError('hook_deadline') from exc
            if not isinstance(response, dict):
                raise ValueError('host_unavailable')
            if 'method' in response and 'id' in response:
                # No automated approval, login or elicitation from a reminder.
                raise ValueError('interactive_request')
            if response.get('id') == self.serial:
                if 'error' in response or not isinstance(response.get('result'), dict):
                    raise ValueError('host_rpc_failed')
                return response['result']

    def _tool(self, tool, arguments):
        result = self._rpc('mcpServer/tool/call', {'threadId': self.thread_id,
            'server': 'hi', 'tool': tool, 'arguments': arguments})
        if result.get('isError', False) is not False:
            raise ValueError('hi_unavailable')
        value = result.get('structuredContent')
        if not isinstance(value, dict):
            content = result.get('content')
            if not isinstance(content, list) or len(content) != 1 or content[0].get('type') != 'text':
                raise ValueError('ambiguous_receipt')
            value = json.loads(content[0]['text'])
        if not isinstance(value, dict) or value.get('ok') is not True:
            raise ValueError('invalid_receipt')
        return value

    def status(self, local_ref):
        return self._tool('hi_agent_status', {'local_instance_ref': local_ref})

    def latest(self, checkpoint):
        payload = {'include_snapshots': True, 'snapshot_limit': 10}
        if checkpoint is not None:
            payload['checkpoint'] = checkpoint
        value = self._tool('workspace_workflows', {'action': 'inbox.latest', 'payload': payload})
        if value.get('contract') != 'hirey.core.workspace.receipt.v1' or value.get('operation') != 'inbox.latest':
            raise ValueError('wrong_operation_receipt')
        result = value.get('result')
        if not isinstance(result, dict) or result.get('contract') != 'hirey.person.inbox.latest.v1':
            raise ValueError('missing_inbox_result')
        return result

    def __exit__(self, *_):
        if self.proc:
            try:
                if os.name == 'posix':
                    os.killpg(self.proc.pid, signal.SIGTERM)
                else:
                    self.proc.terminate()
                self.proc.wait(timeout=0.3)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    if os.name == 'posix':
                        os.killpg(self.proc.pid, signal.SIGKILL)
                    else:
                        self.proc.kill()
                    self.proc.wait(timeout=0.3)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            for stream in (self.proc.stdin, self.proc.stdout):
                if stream:
                    stream.close()
        if self.reader:
            self.reader.join(timeout=0.3)
        if self.temp:
            self.temp.cleanup()
