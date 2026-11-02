"""The desktop and channel command surface share a tested backend contract."""
import pytest
from fastapi.testclient import TestClient

import db
import main
import orchestrator
import runtime_store
import skills


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'api.db')
    monkeypatch.setattr(db, '_conn', None)
    monkeypatch.setattr(skills, 'SKILLS_DIR', tmp_path / 'skills')
    monkeypatch.setattr(skills, 'USES_PATH', tmp_path / 'skills' / '.uses.json')
    monkeypatch.setattr(main, 'orch', orchestrator.Orchestrator())
    monkeypatch.setattr(main, '_api_token', lambda: 'control-test-token')
    yield TestClient(main.app, headers={'Authorization': 'Bearer control-test-token'})
    if db._conn:
        db._conn.close()


def test_unknown_command_and_thread_return_actionable_errors(client):
    response = client.post('/api/command', json={'text': '/not-a-command'})
    assert response.status_code == 400
    assert '/help' in response.json()['error']
    response = client.post('/api/send', json={'thread': 'dm:nope', 'text': 'hello'})
    assert response.status_code == 400
    assert not response.json()['ok']


def test_plain_command_creates_a_mission_and_snapshot_is_consistent(client):
    response = client.post('/api/command', json={'text': 'Inspect the build logs'})
    mission = response.json()['mission']
    assert mission['objective'] == 'Inspect the build logs'
    snapshot = client.get('/api/control').json()
    assert snapshot['missions'][0]['id'] == mission['id']
    assert client.get('/api/kernel').json()['router']['pending'] == 1    # waiting on the Router's decision
    assert set(snapshot) >= {'executions', 'watches', 'skills', 'activity', 'counts', 'platform'}


def test_memory_and_help_commands_return_results_without_queueing(client):
    assert client.post('/api/command', json={'text': '/remember QA runs on Windows'}).json()['ok']
    result = client.post('/api/command', json={'text': '/recall QA'}).json()
    assert 'Windows' in result['reply']
    assert client.get('/api/kernel').json()['router']['pending'] == 0
    assert 'commands' in client.get('/api/commands').json()
    assert client.get('/api/control').json()['counts']['memories'] == 1


def test_invalid_skill_editor_content_cannot_replace_working_skill(client):
    slug = skills.save_skill('owner', 'Example', 'test', 'A useful procedure')
    response = client.put(f'/api/skills/{slug}', json={'content': '---\nmax_tool_calls: -4\n---\nbad'})
    assert not response.json()['ok']
    assert 'A useful procedure' in skills.read_skill(slug)


def test_snapshot_keeps_long_running_tools_after_history_rollover(client):
    long_call = runtime_store.execution_start('tool', 'chief', 'terminal')
    for _ in range(81):
        call = runtime_store.execution_start('tool', 'chief', 'memory_search')
        runtime_store.execution_finish(call['id'], 'completed', 'found')
    snapshot = client.get('/api/control').json()
    assert any(e['id'] == long_call['id'] and e['status'] == 'running' for e in snapshot['executions'])


def test_the_kernel_can_be_inspected_and_policies_edited(client):
    client.post('/api/command', json={'text': 'Inspect the build logs'})
    snapshot = client.get('/api/kernel').json()
    assert snapshot['events'][0]['from_owner'] and snapshot['events'][0]['disposition'] == 'queued'
    assert snapshot['sources']['github'] == 'candidate'
    added = client.post('/api/kernel/policies', json={'action': 'add', 'text': 'Reproduce reported bugs quietly.'}).json()
    assert added['ok']
    assert any('Reproduce reported' in p['text'] for p in client.get('/api/kernel').json()['policies'])
    assert not client.post('/api/kernel/policies', json={'action': 'enable', 'id': 'p_nope'}).json()['ok']
    recalled = client.get('/api/kernel/recall/' + snapshot['events'][0]['id']).json()
    assert 'Inspect the build logs' in recalled['text']
    assert client.get('/api/kernel/sessions/w_nope').status_code == 404


def test_health_reports_the_router(client):
    body = client.get('/api/health').json()
    assert body['ok'] and body['workers'] == 0 and 'router_alive' in body
