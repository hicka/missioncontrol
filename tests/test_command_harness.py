"""Behavioral coverage for commands, mission lifecycle and skill execution."""
import asyncio
import json
import time

import pytest

import db
import harness
import orchestrator
import runtime_store as store
import skills


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'runtime.db')
    monkeypatch.setattr(db, '_conn', None)
    monkeypatch.setattr(skills, 'SKILLS_DIR', tmp_path / 'skills')
    monkeypatch.setattr(skills, 'USES_PATH', tmp_path / 'skills' / '.uses.json')
    yield
    if db._conn:
        db._conn.close()


def contract(**overrides):
    import yaml
    meta = {'name': 'Harness check', 'description': 'exercise the harness',
            'allowed_tools': ['memory_search'], 'max_tool_calls': 2,
            'timeout_seconds': 10, 'version': '2.0',
            'inputs': {'type': 'object', 'required': ['query'], 'additionalProperties': False,
                       'properties': {'query': {'type': 'string', 'minLength': 2}}}, **overrides}
    skills.SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    slug = skills.save_skill('owner', 'Harness check', 'd', 'Read memory, then verify.')
    skills.write_skill(slug, '---\n' + yaml.safe_dump(meta) + '---\nRead memory, then verify.')
    return slug








def test_restart_marks_work_interrupted_without_replaying_it(isolated):
    mission = store.mission_create('chief', 'group:team', 'A deployment')
    store.step_create(mission['id'], 'chief')
    call = store.execution_start('tool', 'chief', 'terminal', mission=mission['id'])
    store.recover()
    assert store.mission_get(mission['id'])['status'] == 'waiting'
    assert store.executions()[0]['id'] == call['id']
    assert store.executions()[0]['status'] == 'interrupted'


def test_invalid_tool_arguments_return_errors_and_do_not_execute(isolated, monkeypatch):
    o = orchestrator.Orchestrator()
    aid = next(iter(o.agents))
    called = []
    async def terminal(*args, **kwargs):
        called.append(1)
        return 'exit 0'
    monkeypatch.setattr('tools.run_terminal', terminal)
    async def run():
        for args in (None, [], '{not json', {'command': 42}, {'command': 'echo ok', 'surprise': True}):
            out = await o._exec_tool(aid, '~', 'terminal', args)
            assert out.status == 'failed' and out.startswith('error:')
        assert not called
        assert all(e['status'] == 'failed' for e in store.executions())
    asyncio.run(run())


def test_skill_validates_inputs_and_agent_access_before_start(isolated):
    o = orchestrator.Orchestrator()
    aid = next(iter(o.agents))
    slug = contract(agents=[aid])
    async def run():
        with pytest.raises(ValueError, match='required property'):
            await o.harness.activate(aid, slug, {})
        with pytest.raises(ValueError, match='not available'):
            await o.harness.activate(list(o.agents)[1], slug, {'query': 'CI'})
        assert not store.executions()
        with pytest.raises(ValueError, match='required property'):
            await o.user_message('group:team', '/skill harness-check {}')
        assert not store.missions()
    asyncio.run(run())


def test_skill_enforces_tools_budget_and_requires_completion_evidence(isolated):
    o = orchestrator.Orchestrator()
    aid = next(iter(o.agents))
    slug = contract(max_tool_calls=1)
    async def call(name, args):
        return await o._exec_tool(aid, '~', name, args)
    async def run():
        o.harness.start_turn(aid)
        text = await o.harness.activate(aid, slug, {'query': 'CI'})
        assert 'ACTIVE SKILL' in text and 'revision' in text
        denied = await call('terminal', {'command': 'echo forbidden'})
        assert denied.status == 'failed' and 'does not allow' in denied
        assert (await call('memory_search', {'query': 'CI'})).status == 'completed'
        assert 'budget exhausted' in await call('memory_search', {'query': 'CI'})
        assert 'evidence' in await call('skill', {'action': 'complete', 'result': 'done'})
        assert 'completed' in await call('skill', {'action': 'complete', 'result': 'No saved CI facts', 'evidence': 'memory_search returned no matches'})
        assert aid not in o.harness.active
        run_record = next(e for e in store.executions() if e['kind'] == 'skill')
        assert run_record['status'] == 'completed' and 'Evidence:' in run_record['result']
        calls = [e for e in store.executions() if e['kind'] == 'tool']
        assert all(e['skill_run'] == run_record['id'] for e in calls)
    asyncio.run(run())


def test_skill_cannot_widen_agent_permissions_or_replace_active_run(isolated):
    o = orchestrator.Orchestrator()
    aid = next(iter(o.agents))
    slug = contract(allowed_tools=['terminal'])
    o.agents[aid]['tools'] = ['skill', 'memory_search']
    async def run():
        await o.harness.activate(aid, slug, {'query': 'CI'})
        with pytest.raises(ValueError, match='finish the active skill'):
            await o.harness.activate(aid, slug, {'query': 'CI'})
        denied = await o._exec_tool(aid, '~', 'terminal', {'command': 'echo forbidden'})
        assert 'not enabled' in denied
        await o.harness.end_turn(aid)
        assert next(e for e in store.executions() if e['kind'] == 'skill')['status'] == 'unverified'
    asyncio.run(run())


def test_skill_deadline_cancels_execution_and_keeps_audit(isolated, monkeypatch):
    o = orchestrator.Orchestrator()
    aid = next(iter(o.agents))
    slug = contract()
    cancelled = []
    async def slow():
        try:
            await asyncio.sleep(5)
        finally:
            cancelled.append(True)
    async def run():
        await o.harness.activate(aid, slug, {'query': 'CI'})
        o.harness.active[aid]['deadline'] = time.monotonic() + .02
        out = await o.harness.execute(aid, 'memory_search', {'query': 'CI'}, slow)
        assert out.status == 'failed' and 'timed out' in out
        assert cancelled
        assert next(e for e in store.executions() if e['kind'] == 'tool')['status'] == 'failed'
    asyncio.run(run())


def test_learning_preserves_contract_and_malformed_yaml_is_rejected(isolated):
    slug = contract(max_tool_calls=3)
    skills.save_skill('agent', 'Harness check', 'new description', 'New procedure', mode='replace')
    assert skills.manifest(slug)['max_tool_calls'] == 3
    with pytest.raises(ValueError):
        skills.write_skill(slug, '---\nallowed_tools: [broken\n---\nbody')
    assert skills.manifest(slug)['max_tool_calls'] == 3


def test_concurrent_skill_use_counts_are_not_lost(isolated):
    from concurrent.futures import ThreadPoolExecutor
    slug = contract()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(skills.record_use, [slug] * 30))
    assert skills.list_skills()[0]['uses'] == 30


def test_external_schema_references_and_symlink_skills_are_rejected(isolated, tmp_path):
    with pytest.raises(ValueError, match='local references'):
        contract(inputs={'type': 'object', '$ref': 'https://untrusted.invalid/schema'})
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'SKILL.md').write_text('private')
    link = skills.SKILLS_DIR / 'linked'
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('symlinks not available')
    with pytest.raises(ValueError, match='inside'):
        skills.read_skill('linked')
    assert skills.list_skills()[0]['slug'] != 'linked'


def test_tool_cancellation_is_recorded_and_propagated(isolated):
    o = orchestrator.Orchestrator()
    aid = next(iter(o.agents))
    started = asyncio.Event()
    async def slow():
        started.set()
        await asyncio.sleep(10)
    async def run():
        task = asyncio.create_task(o.harness.execute(aid, 'memory_search', {'query': 'CI'}, slow))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert store.executions()[0]['status'] == 'cancelled'
    asyncio.run(run())


def test_audit_redacts_configured_secrets(isolated, monkeypatch):
    monkeypatch.setattr('settings.env_all', lambda: {'TOKEN': 'private-test-value'})
    assert 'private-test-value' not in harness.redact('curl -H private-test-value')
    assert 'password123' not in harness.redact({'password': 'password123'})


def test_local_commands_do_not_wake_models_and_unknown_commands_fail(isolated):
    import kernel_store
    o = orchestrator.Orchestrator()
    async def run():
        result = await o.user_message('group:team', '/remember CI uses Windows and macOS')
        assert result['ok']
        recalled = await o.user_message('group:team', '/recall CI')
        assert 'Windows' in recalled['reply']
        assert not kernel_store.inbox_pending('router') and not kernel_store.sessions(None, 'worker')
        with pytest.raises(ValueError, match='unknown command'):
            await o.user_message('group:team', '/destructive-mystery')
        with pytest.raises(ValueError, match='unknown conversation'):
            await o.user_message('dm:missing', 'hello')
    asyncio.run(run())


def test_mission_routes_to_target_and_stop_ends_its_work(isolated, monkeypatch):
    import kernel_store
    o = orchestrator.Orchestrator()
    monkeypatch.setattr(o.workers, 'wake', lambda *a, **k: None)
    monkeypatch.setattr(o, '_current_state_block', __import__('unittest.mock', fromlist=['AsyncMock']).AsyncMock(return_value=''))
    aid = list(o.agents)[1]
    async def run():
        result = await o.user_message('group:team', f'/mission @{aid} Investigate build failures')
        mission = result['mission']
        assert mission['agent'] == aid
        row = kernel_store.session_for_mission(mission['id'])
        assert row['profile'] == aid and kernel_store.inbox_should_wake(row['id'])['count'] == 1
        assert await o.stop_agent(aid)
        assert kernel_store.session_get(row['id'])['status'] == 'ended'
        assert store.mission_get(mission['id'])['status'] == 'cancelled'
        await o.user_message('group:team', f'/resume {mission["id"]}')
        assert kernel_store.session_get(row['id'])['status'] != 'ended'
        assert store.mission_get(mission['id'])['status'] == 'running'
        with pytest.raises(ValueError, match='only waiting'):
            await o.user_message('group:team', f'/resume {mission["id"]}')
    asyncio.run(run())
