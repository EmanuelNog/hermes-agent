"""A TUI session's /status, /context and /cron on the shared owner: /status is main's TUI sidecar
text, /context is the classic CLI's own body (the two used to disagree), and /cron reads run main's
handler on the owner — its writes run there only when the classic CLI sends them on its own command
socket (a viewer's connection keeps main's refusal).

Through the owner a TUI session got the messaging gateway's markdown instead (FINDINGS rows 17 / 19 /
21); main's TUI drew /context as conversation counts where the classic CLI draws the glyph grid, the
category table and the context-file manifest. Other surfaces keep the gateway's handlers.
"""
import contextlib
import io
from types import SimpleNamespace

import pytest
import pytest_asyncio
from persistence_assembly import store_location
from tests.fixtures.storage_seed import seed_messages


@pytest_asyncio.fixture
async def owner(tmp_path, monkeypatch):
    from gateway import run
    from gateway.config import GatewayConfig
    from gateway.session import SessionStore
    from gateway.session_authority import initialize_session_authority
    from gateway.session_controls import AuthorityConnection

    home = tmp_path / 'home'
    home.mkdir()
    (home / 'config.yaml').write_text(
        'model: {default: fake-model, provider: custom, base_url: "http://127.0.0.1:9/v1"}\n')
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(run, '_hermes_home', home)
    store = SessionStore(tmp_path / 'sessions', GatewayConfig())
    runner = SimpleNamespace(session_store=store, _session_db=store.persistence, adapters={}, _draining=False,
                             _cached_agent_for=lambda route: None)
    runner._adapter_for_source = lambda source: runner.adapters.get(source.platform)
    runner._adapters_for_profile = lambda profile: runner.adapters
    authority = await initialize_session_authority(runner, profile_id=str(home), instance_id='first')
    connection = AuthorityConnection(authority, object(), {'user_id': 'human'})

    async def call(method, **params):
        reply = await connection.dispatch({'id': 1, 'method': method, 'params': params})
        assert 'result' in reply, reply
        return reply['result']
    yield SimpleNamespace(call=call, home=home, authority=authority, workdir=tmp_path, connection=connection)


async def _exec(owner, sid, command):
    return (await owner.call('slash.exec', session_id=sid, command=command))['output']


@pytest.mark.asyncio
async def test_tui_status_is_main_tui_status(owner):
    sid = (await owner.call('session.create', request_id='t', source='tui', cwd=str(owner.workdir)))['session_id']
    out = await _exec(owner, sid, 'status')
    lines = out.splitlines()
    assert lines[0] == 'Hermes TUI Status' and lines[1] == '', out
    assert lines[2].startswith('Session ID: ') and lines[3].startswith('Path: ')
    labels = [line.split(':')[0] for line in lines[4:]]
    assert labels == ['Model', 'Created', 'Last Activity', 'Tokens', 'Agent Running'], out
    assert 'Model: fake-model (custom)' in out and 'Agent Running: No' in out
    assert '**' not in out and 'Gateway Status' not in out


@pytest.mark.asyncio
async def test_tui_status_names_the_custom_entry_a_bare_custom_agent_runs_on(owner):
    relay = 'http://127.0.0.1:8080/v1'
    (owner.home / 'config.yaml').write_text(
        f'model: {{default: deepseek-v4.1-flash, provider: custom, base_url: "{relay}"}}\n'
        f'providers:\n  glm: {{api: "{relay}", default_model: glm-5.3}}\n'
        f'  deepseek: {{api: "{relay}", default_model: deepseek-v4.1-flash}}\n')
    sid = (await owner.call('session.create', request_id='t', source='tui', cwd=str(owner.workdir)))['session_id']
    agent = SimpleNamespace(model='deepseek-v4.1-flash', provider='custom', requested_provider='custom:deepseek',
                            base_url=relay)
    owner.authority.runner._cached_agent_for = lambda route: agent
    assert 'Model: deepseek-v4.1-flash (custom:deepseek)' in await _exec(owner, sid, 'status')


_PAYLOAD = {
    'categories': [{'id': 'system_prompt', 'label': 'System prompt', 'tokens': 3000, 'color': '#fff'},
                   {'id': 'conversation', 'label': 'Conversation', 'tokens': 4000, 'color': '#eee'}],
    'context_max': 128000, 'context_used': 7000, 'context_percent': 5, 'estimated_total': 7000,
    'context_estimated': False, 'context_source': 'provider_usage', 'model': 'fake-model'}


def _classic_cli_body(agent, model):
    """What the classic CLI's /context prints for *agent*, as lines (its own 2-space margin removed)."""
    from hermes_cli.cli_info_mixin import CLIInfoMixin

    class _ClassicCLI(CLIInfoMixin):  # the mixin's own method, over the two attributes it reads
        def __init__(self):
            self.agent, self.conversation_history, self.model = agent, [], model
            self._session_db, self._gateway_session_open = None, False

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        _ClassicCLI()._show_context_breakdown('context')
    lines = [line.removeprefix('  ') for line in buffer.getvalue().splitlines()]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return lines


@pytest.mark.asyncio
async def test_tui_context_without_an_agent_answers_the_classic_cli_line(owner):
    from agent.i18n import t
    sid = (await owner.call('session.create', request_id='t', source='tui', cwd=str(owner.workdir)))['session_id']
    assert await _exec(owner, sid, 'context') == t('cli.shared.no_active_agent')


@pytest.mark.asyncio
async def test_tui_context_with_a_live_agent_draws_the_classic_cli_grid_and_table(owner, monkeypatch):
    from agent import context_breakdown
    from agent.i18n import t
    sid = (await owner.call('session.create', request_id='t', source='tui', cwd=str(owner.workdir)))['session_id']
    agent = SimpleNamespace(model='fake-model', provider='custom')
    owner.authority.runner._cached_agent_for = lambda route: agent
    monkeypatch.setattr(context_breakdown, 'compute_session_context_breakdown', lambda a, m: dict(_PAYLOAD))
    out = await _exec(owner, sid, 'context')
    lines = out.splitlines()
    assert lines[0] == t('cli.context.header', model='fake-model'), out
    assert len(lines[2].split()) == 20 and len(lines[6].split()) == 20, out  # 5x20 glyph grid
    assert 'Estimated usage by category' in out and 'System prompt' in out, out
    # The counts header main's TUI printed is gone — the CLI never had it.
    assert 'Conversation is empty' not in out and 'Provider: auto' not in out, out


@pytest.mark.asyncio
async def test_tui_context_is_byte_for_byte_the_classic_cli_body(owner, monkeypatch):
    """The regression: /context on a TUI session presented different info than `hermes` (conversation
    counts instead of the CLI's grid/table/context files). Both surfaces must now render the same body
    from the same breakdown payload."""
    from agent import context_breakdown, context_file_sources
    sid = (await owner.call('session.create', request_id='t', source='tui', cwd=str(owner.workdir)))['session_id']
    agent = SimpleNamespace(model='fake-model', provider='custom')
    owner.authority.runner._cached_agent_for = lambda route: agent
    monkeypatch.setattr(context_breakdown, 'compute_session_context_breakdown', lambda a, m: dict(_PAYLOAD))
    monkeypatch.setattr(context_file_sources, 'context_file_sources_for_agent', lambda a: [
        {'label': 'AGENTS.md', 'status': 'loaded', 'est_tokens': 812}])
    tui = (await _exec(owner, sid, 'context')).splitlines()
    assert tui == _classic_cli_body(agent, 'fake-model'), tui
    assert 'Context files' in tui and 'AGENTS.md' in '\n'.join(tui), tui


@pytest.mark.asyncio
async def test_tui_cron_is_the_classic_overview_with_the_profile_jobs(owner):
    from cron.jobs import create_job
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    token = set_hermes_home_override(str(owner.home))
    try:
        create_job('Summarize the news', '0 9 * * *', name='daily-digest')
    finally:
        reset_hermes_home_override(token)
    sid = (await owner.call('session.create', request_id='t', source='tui', cwd=str(owner.workdir)))['session_id']
    out = await _exec(owner, sid, 'cron')
    assert '(^_^) Scheduled Tasks' in out and '/cron list' in out, out
    assert '0 9 * * *' in out and 'Summarize the news' in out, out
    listed = await _exec(owner, sid, 'cron list')
    assert 'Scheduled Jobs:' in listed and 'daily-digest' in listed, listed


@pytest.mark.asyncio
async def test_cron_writes_run_on_the_classic_cli_command_socket_whatever_surface_made_the_session(owner):
    """T1 (#134889): the classic CLI's /cron add|pause|remove wrote the profile's cron store from the
    CLI process. The owner runs them now (one writer) when they arrive on a connection that is not attached
    to the session, the CLI's own command socket (GatewayConnection.call_until), even for a session a TUI
    created and the CLI resumed (main's CLI wrote the store there too)."""
    import json
    from gateway.session_controls import AuthorityConnection
    jobs = owner.home / 'cron' / 'jobs.json'
    sid = (await owner.call('session.create', request_id='t', source='tui', cwd=str(owner.workdir)))['session_id']
    command_socket = AuthorityConnection(owner.authority, object(), {'user_id': 'human'})

    async def run(command):
        reply = await command_socket.dispatch({'id': 2, 'method': 'slash.exec',
                                               'params': {'session_id': sid, 'command': command}})
        assert 'result' in reply, (command, reply)
        return reply['result']['output']
    added = await run('cron add "every 2h" "ping CRONPROBE" --name probe')
    assert 'Created job:' in added and 'every 120m' in added, added
    paused = await run('cron pause probe')
    assert 'Paused job: probe' in paused, paused
    assert [j['enabled'] for j in json.loads(jobs.read_text())['jobs'] if j['name'] == 'probe'] == [False]
    assert 'State: paused' in await run('cron list --all')
    assert 'Removed job: probe' in await run('cron remove probe')
    assert json.loads(jobs.read_text())['jobs'] == []
    assert 'Unknown cron command: bogus' in await run('cron bogus')


@pytest.mark.asyncio
async def test_cron_writes_stay_refused_on_a_viewer_connection_and_reads_list_all(owner):
    """A connection attached to the session (Ink, Desktop and ACP send /cron on their main connection, which
    answers one request at a time with a deadline) keeps main's refusal for /cron writes, whichever surface
    created the session, a classic CLI session included. Its reads still list, ``-a`` included (main's TUI
    alias)."""
    from cron.jobs import create_job, pause_job
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    token = set_hermes_home_override(str(owner.home))
    try:
        pause_job(create_job('ping', 'every 2h', name='parked')['id'])
    finally:
        reset_hermes_home_override(token)
    for source in ('cli', 'tui', 'gui', 'acp'):
        sid = (await owner.call('session.create', request_id=source, source=source,
                                cwd=str(owner.workdir)))['session_id']
        assert 'parked' in await _exec(owner, sid, 'cron list -a'), source
        assert 'parked' not in await _exec(owner, sid, 'cron list'), source
        reply = await owner.connection.dispatch({'id': 9, 'method': 'slash.exec',
                                                 'params': {'session_id': sid, 'command': 'cron resume parked'}})
        assert reply.get('error', {}).get('message') == 'unsupported_command', (source, reply)
    assert 'State: paused' in await _exec(owner, sid, 'cron list --all')  # nothing resumed it


@pytest.mark.asyncio
async def test_concurrent_cron_runs_never_wait_on_the_pool_their_jobs_need(owner, monkeypatch):
    """A /cron run waits for its job, and the job's admission (tracked_write) and execution
    (session_cron.execute) take threads from the loop's default pool. Waiting on that same pool, two
    runs on a one-thread pool never answered: the job could not start, and the cron layer paused it as
    unverified. The wait holds a thread of the runner's own unbounded pool instead."""
    import asyncio
    import concurrent.futures
    from types import MethodType
    import cron.scheduler
    from cron.jobs import create_job, get_job
    from gateway.run import GatewayRunner
    from gateway.session_controls import AuthorityConnection
    from gateway.session_cron import current_execution
    runner = owner.authority.runner
    runner._get_executor = MethodType(GatewayRunner._get_executor, runner)
    runner._run_in_executor_with_context = MethodType(GatewayRunner._run_in_executor_with_context, runner)
    submit = cron.scheduler.run_job  # outside the owner's execution: submit the fire and wait for it

    def run_job(job, **kwargs):
        return submit(job, **kwargs) if current_execution() is None else (True, '# out', 'ran', None)
    monkeypatch.setattr(cron.scheduler, 'run_job', run_job)
    # The fire is submitted to the owner bound to the home of the authority's store (bind_owner).
    home = store_location(owner.authority.persistence.scope).resolve().parent
    (home / 'config.yaml').write_text((owner.home / 'config.yaml').read_text())
    monkeypatch.setenv('HERMES_HOME', str(home))
    ids = [create_job('ping', 'every 2h', name=name)['id'] for name in ('one', 'two')]
    sid = (await owner.call('session.create', request_id='r', source='cli', cwd=str(owner.workdir)))['session_id']
    asyncio.get_running_loop().set_default_executor(concurrent.futures.ThreadPoolExecutor(1))

    async def run(n, job_id):
        socket = AuthorityConnection(owner.authority, object(), {'user_id': 'human'})  # the CLI's own
        reply = await socket.dispatch({'id': n, 'method': 'slash.exec',
                                       'params': {'session_id': sid, 'command': f'cron run {job_id}'}})
        assert 'result' in reply, reply
        return reply['result']['output']
    try:
        outs = await asyncio.wait_for(asyncio.gather(*(run(n, i) for n, i in enumerate(ids))), 30)
    finally:
        if getattr(runner, '_executor', None) is not None:
            runner._executor.shutdown(wait=False)
    assert [out.splitlines()[-1].strip() for out in outs] == ['Ran now: succeeded.'] * 2, (
        outs, [get_job(i)['last_error'] for i in ids])


@pytest.mark.asyncio
async def test_other_surfaces_keep_the_gateway_handlers(owner):
    from gateway.session_tui_reads import tui_read
    for source in ('cli', 'gui', 'acp'):
        policy = SimpleNamespace(source=source)
        assert tui_read(policy, 'status', '') is None and tui_read(policy, 'context', '') is None
    assert tui_read(None, 'status', '') is None


@pytest.mark.asyncio
async def test_context_breakdown_verb_serves_the_owner_agents_categories(owner, monkeypatch):
    """The classic CLI's /context (and Desktop's popover) read the breakdown of the gateway's own
    agent through ``session.context_breakdown``; before the agent exists it is the empty shape."""
    sid = (await owner.call('session.create', request_id='c', source='cli', cwd=str(owner.workdir)))['session_id']
    empty = await owner.call('session.context_breakdown', session_id=sid)
    assert empty['categories'] == [] and empty['estimated_total'] == 0
    from agent import context_breakdown, context_file_sources
    agent = SimpleNamespace(model='fake-model', provider='custom')
    owner.authority.runner._cached_agent_for = lambda route: agent
    monkeypatch.setattr(context_breakdown, 'compute_session_context_breakdown', lambda a, m: {
        'categories': [{'id': 'system', 'label': 'System prompt', 'tokens': 3000, 'color': '#fff'}],
        'context_max': 128000, 'context_used': 120, 'context_percent': 0, 'estimated_total': 3000,
        'context_estimated': False, 'context_source': 'provider_usage', 'model': 'fake-model'} if a is agent else {})
    monkeypatch.setattr(context_file_sources, 'context_file_sources_for_agent', lambda a: [])
    live = await owner.call('session.context_breakdown', session_id=sid)
    assert live['categories'][0]['tokens'] == 3000 and live['model'] == 'fake-model', live


@pytest.mark.asyncio
async def test_context_breakdown_is_reused_until_the_transcript_moves(owner, monkeypatch):
    """#140667: Desktop re-polls ``session.context_breakdown`` on every idle mount and reconnect,
    and each poll rebuilt the prompt over the whole stored transcript (seconds on a 9,501-row
    session). The owner reuses the payload while the transcript revision is unchanged and
    recomputes once a row lands."""
    sid = (await owner.call('session.create', request_id='r', source='cli', cwd=str(owner.workdir)))['session_id']
    from agent import context_breakdown, context_file_sources
    agent = SimpleNamespace(model='fake-model', provider='custom')
    owner.authority.runner._cached_agent_for = lambda route: agent
    computed = []

    def compute(a, messages):
        computed.append(len(messages))
        return {'categories': [{'id': 'conversation', 'label': 'Conversation', 'tokens': len(messages),
                                'color': '#fff'}], 'context_max': 128000, 'context_used': 1,
                'context_percent': 0, 'estimated_total': len(messages), 'context_estimated': True,
                'context_source': 'local_estimate', 'model': 'fake-model'}
    monkeypatch.setattr(context_breakdown, 'compute_session_context_breakdown', compute)
    monkeypatch.setattr(context_file_sources, 'context_file_sources_for_agent', lambda a: [])
    first = await owner.call('session.context_breakdown', session_id=sid)
    again = await owner.call('session.context_breakdown', session_id=sid)
    assert computed == [0] and again == first
    from gateway.session_tui_reads import _stored_id
    stored = _stored_id(owner.authority, SimpleNamespace(session_id=sid))
    seed_messages(owner.authority.persistence, stored, {'role': 'user', 'content': 'hello'})
    moved = await owner.call('session.context_breakdown', session_id=sid)
    assert computed == [0, 1] and moved['estimated_total'] == 1
    await owner.call('session.context_breakdown', session_id=sid)
    assert computed == [0, 1]


@pytest.mark.asyncio
async def test_context_breakdown_follows_an_in_place_row_rewrite(owner, monkeypatch):
    """The turn prologue rewrites an already-written user row in place (``set_user_message_content``:
    an @-file expansion, multimodal context), which moves neither the row count nor the highest id.
    A breakdown cached before the rewrite must not keep the old size (probe by @dokterdok)."""
    sid = (await owner.call('session.create', request_id='r', source='cli', cwd=str(owner.workdir)))['session_id']
    from agent import context_breakdown, context_file_sources
    agent = SimpleNamespace(model='fake-model', provider='custom')
    owner.authority.runner._cached_agent_for = lambda route: agent

    def compute(a, messages):
        chars = sum(len(str(m.get('content') or '')) for m in messages)
        return {'categories': [], 'context_max': 128000, 'context_used': chars, 'context_percent': 0,
                'estimated_total': chars, 'context_estimated': True, 'context_source': 'local_estimate',
                'model': 'fake-model'}
    monkeypatch.setattr(context_breakdown, 'compute_session_context_breakdown', compute)
    monkeypatch.setattr(context_file_sources, 'context_file_sources_for_agent', lambda a: [])
    from gateway.session_tui_reads import _stored_id
    stored = _stored_id(owner.authority, SimpleNamespace(session_id=sid))
    db = owner.authority.persistence
    [row_id] = seed_messages(db, stored, {'role': 'user', 'content': 'explain @file:big.py'})
    before = await owner.call('session.context_breakdown', session_id=sid)
    expanded = 'explain @file:big.py\n' + 'x' * 20000
    assert db.messages.set_user_message_content(stored, row_id, expanded) is True
    after = await owner.call('session.context_breakdown', session_id=sid)
    assert after['estimated_total'] == len(expanded) > before['estimated_total']
