"""A compaction driven by ``session.mutate(compress)`` records its summary spend (chk-n5va.87).

``session_model_usage`` gets a ``task='compression'`` row from the aux accounting chokepoint
(``agent/aux_accounting.record_aux_usage``), which records only when a session handle is
published — and only an agent turn published one. A session mutation builds its own compressor
on a worker thread, so the summarizer's tokens were billed to nobody and any cost review
trusting ``task='compression'`` under-reported summarisation.

Here ``prepare_compress`` runs for real (real SessionDB, real ContextCompressor, real summary
call against a loopback HTTP model); only the gateway's policy/config layer is stubbed.
"""
from contextlib import closing
from http.server import ThreadingHTTPServer
import asyncio
import json
import sqlite3
import threading
from types import SimpleNamespace
from unittest.mock import patch

from gateway.session_mutation_compress import prepare_compress
from hermes_state import SessionDB


def test_out_of_turn_compression_records_compression_usage(tmp_path):
    from http.server import BaseHTTPRequestHandler

    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(body)
            summary = any('You are a summarization agent creating a context checkpoint.' in str(m.get('content', ''))
                          for m in body.get('messages', []))
            text = 'SUMMARY_RETAINED_FACTS' if summary else 'filler'
            payload = json.dumps({'id': 'fixture', 'choices': [{'index': 0,
                'message': {'role': 'assistant', 'content': text}, 'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 42, 'completion_tokens': 8, 'total_tokens': 50}}).encode()
            kind = 'application/json'
            if body.get('stream'):
                payload = ('data: ' + json.dumps({'id': 'fixture', 'choices': [{'index': 0,
                    'delta': {'role': 'assistant', 'content': text}, 'finish_reason': 'stop'}]})
                    + '\n\ndata: ' + json.dumps({'id': 'fixture', 'choices': [{'index': 0,
                    'delta': {}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 42,
                    'completion_tokens': 8, 'total_tokens': 50}}) + '\n\ndata: [DONE]\n\n').encode()
                kind = 'text/event-stream'
            self.send_response(200)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    peer = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=peer.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{peer.server_port}/v1'

    home = tmp_path / 'home'
    home.mkdir()
    db = SessionDB(home / 'state.db')
    db.create_session('s1', source='cli')
    for i in range(8):
        db.append_message('s1', 'user', f'question {i} ' + 'detail ' * 200)
        db.append_message('s1', 'assistant', f'answer {i} ' + 'detail ' * 200)

    policy = SimpleNamespace(safe_mode=False, config=lambda authority: {
        'compression': {'protect_first_n': 1, 'protect_last_n': 2, 'min_tail_user_messages': 1},
    })
    authority = SimpleNamespace(
        db=db,
        runner=SimpleNamespace(
            _resolve_session_agent_runtime=lambda source, session_key: ('main-model', {
                'provider': 'custom', 'base_url': url, 'api_key': 'test-key', 'api_mode': '',
            }),
            _cached_agent_for=lambda route: None,
        ),
    )
    live = SimpleNamespace(source='cli', route='agent:main:cli')
    prepared = {'snapshot': {'target': 's1', 'receipt': {'policy': {}}}, 'messages': []}

    with (
        patch('gateway.session_policy.restore_policy', return_value=policy),
        patch('gateway.session_policy.policy_scope'),
        patch('gateway.session_mutation_compress._plugin_engine', return_value=None),
        patch('agent.context_compressor.get_model_context_length', return_value=100000),
        patch('hermes_cli.config.get_compatible_custom_providers', return_value=[]),
        patch('agent.auxiliary_client._resolve_task_provider_model',
              return_value=('custom', 'summary-model', url, 'test-key', None)),
        patch('agent.auxiliary_client._get_auxiliary_task_config', return_value={}),
    ):
        result = asyncio.run(prepare_compress(authority, live, {}, prepared))
    peer.shutdown()
    peer.server_close()
    thread.join(timeout=5)

    assert 'SUMMARY_RETAINED_FACTS' in json.dumps(result['messages'])
    assert any('You are a summarization agent' in str(m.get('content', ''))
               for body in requests for m in body.get('messages', [])), 'no summary call reached the model'
    with closing(sqlite3.connect(home / 'state.db')) as conn:
        rows = conn.execute(
            "SELECT session_id, task, api_call_count, input_tokens, output_tokens"
            " FROM session_model_usage WHERE task='compression'").fetchall()
    assert rows, 'the summary spent tokens but no task=compression usage row was recorded'
    assert rows[0][0] == 's1'
    assert rows[0][2] >= 1 and rows[0][3] == 42
