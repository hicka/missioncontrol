"""Browser ownership, local controls and private input handling."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import browser
import db
import harness
import main
import orchestrator
import runtime
import runtime_store
import settings
import skills


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(db, 'DB_PATH', tmp_path / 'browser.db')
    monkeypatch.setattr(db, '_conn', None)
    monkeypatch.setattr(settings, 'SETTINGS_PATH', tmp_path / 'settings.yaml')
    monkeypatch.setattr(browser, 'PROFILE_DIR', tmp_path / 'profile')
    monkeypatch.setattr(browser, '_pages', {})
    monkeypatch.setattr(browser, '_tabs', {})
    monkeypatch.setattr(browser, '_action_locks', {})
    monkeypatch.setattr(browser, '_lock', asyncio.Lock())
    registry = runtime.Registry()
    registry.add('browser', 'Browser', '', stop=browser._reset)
    registry.services['browser'].state = 'running'
    monkeypatch.setattr(runtime, 'registry', registry)
    yield registry
    if db._conn:
        db._conn.close()


@pytest.mark.parametrize('url', ['file:///tmp/test', 'javascript:alert(1)', 'https://user:password@example.com', 'http://', ''])
def test_navigation_rejects_non_web_and_credential_urls(url):
    with pytest.raises(ValueError):
        browser.website_url(url)


def test_browser_url_and_visible_metadata():
    assert browser.website_url(' example.com/path ') == 'https://example.com/path'
    assert browser._display_url('https://example.com/callback?code=secret#token') == 'https://example.com/callback'


def test_disabled_browser_prevents_launch_and_returns_failed_tool_result(isolated):
    settings.update('runtime_services', {'browser': False})
    result = asyncio.run(browser.act('chief', 'read', {}))
    assert harness.result_status(result) == 'failed'
    assert browser._context is None


def test_tool_errors_never_echo_fill_values(isolated, monkeypatch):
    monkeypatch.setattr(browser, '_act', AsyncMock(side_effect=RuntimeError('fill("private-password") failed')))
    result = asyncio.run(browser.act('chief', 'type', {'ref': 1, 'text': 'private-password'}))
    assert harness.result_status(result) == 'failed'
    assert 'private-password' not in result


def test_browser_audit_omits_typed_text_even_when_blocked(isolated):
    async def run():
        emit = AsyncMock()
        agent = {'chief': {'tools': ['browser']}}
        h = harness.Harness(agent, orchestrator.TOOL_DEFS, emit)
        dispatch = AsyncMock(return_value='Blocked: owner input needed')
        result = await h.execute('chief', 'browser', {'action': 'type', 'ref': 1, 'text': 'private-password'}, dispatch)
        assert result.status == 'failed'
        record = runtime_store.executions()[0]
        assert 'private-password' not in str(record)
        assert '[typed text hidden]' in str(record)
        assert 'private-password' not in str(emit.call_args_list)
    asyncio.run(run())


def test_browser_controls_are_local_only_and_use_registered_service(isolated, monkeypatch):
    monkeypatch.setattr(browser, 'open_owner', AsyncMock())
    monkeypatch.setattr(browser, '_reset', AsyncMock())
    client = TestClient(main.app, client=('127.0.0.1', 5000))
    assert client.get('/api/runtime/browser').json()['enabled']
    assert client.post('/api/runtime/browser/open', json={'url': 'https://example.com'}).json()['ok']
    browser.open_owner.assert_awaited_once_with('https://example.com')
    assert client.put('/api/runtime/browser/settings', json={'headless': True}).json()['headless']
    browser._reset.assert_awaited_once()
    assert settings.load()['browser']['headless']
    assert client.post('/api/runtime/browser/sessions/missing/focus').status_code == 400
    assert client.post('/api/runtime/browser/open', json={}, headers={'origin': 'https://untrusted.example'}).status_code == 403
    remote = TestClient(main.app, client=('192.0.2.1', 5000))
    assert remote.get('/api/runtime/browser').status_code == 403
    browser.open_owner.assert_awaited_once()


def test_website_skill_has_a_valid_bounded_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(skills, 'SKILLS_DIR', tmp_path / 'skills')
    skills.ensure_starters()
    manifest = skills.manifest('website-work', 'chief', {'url': 'https://example.com', 'objective': 'Read this website'})
    assert 'browser' in manifest['allowed_tools']
    assert manifest['max_tool_calls'] == 50


def test_screenshot_image_survives_harness_without_entering_audit(isolated):
    async def run():
        blocks = [{'type': 'text', 'text': 'Current browser viewport'}, {'type': 'image', 'source': {
            'type': 'base64', 'media_type': 'image/png', 'data': 'private-image-payload'}}]
        emit = AsyncMock()
        h = harness.Harness({'chief': {'tools': ['browser']}}, orchestrator.TOOL_DEFS, emit)
        dispatch = AsyncMock(return_value=harness.ToolResult('Current browser viewport', content=blocks))
        result = await h.execute('chief', 'browser', {'action': 'screenshot'}, dispatch)
        assert result.status == 'completed' and result.content == blocks
        assert 'private-image-payload' not in str(runtime_store.executions())
        assert 'private-image-payload' not in str(emit.call_args_list)
    asyncio.run(run())


def test_current_observation_never_navigates_or_launches(isolated, monkeypatch):
    from unittest.mock import Mock
    page = Mock(url='https://example.com/current')
    page.is_closed.return_value = False
    browser._pages['chief'] = page
    monkeypatch.setattr(browser, '_snapshot', AsyncMock(return_value='Open profile menu'))
    monkeypatch.setattr(browser, '_ensure_page', AsyncMock(side_effect=AssertionError('must not launch')))
    result = asyncio.run(browser.current_observation('chief'))
    assert 'Open profile menu' in result
    page.goto.assert_not_called()


def test_browser_turn_receives_live_state_and_image_content(isolated, monkeypatch):
    import llm
    import budget
    import kernel_store
    o = orchestrator.Orchestrator()
    aid = next(iter(o.agents))
    monkeypatch.setattr(budget, 'check', lambda **kwargs: ('ok', None))
    monkeypatch.setattr(browser, 'current_observation', AsyncMock(return_value='LIVE: Northwind profile menu'))
    blocks = [{'type': 'text', 'text': 'Screenshot'}, {'type': 'image', 'source': {
        'type': 'base64', 'media_type': 'image/png', 'data': 'image-data'}}]
    monkeypatch.setattr(o, '_dispatch_tool', AsyncMock(return_value=harness.ToolResult('Screenshot', content=blocks)))
    calls = []
    async def complete(system, messages, *args, **kwargs):
        if not calls:
            assert 'LIVE: Northwind profile menu' in str(messages[-1]['content'])
            calls.append(1)
            return {'content': [{'type': 'tool_use', 'id': 'screen', 'name': 'browser', 'input': {'action': 'screenshot'}}]}
        assert messages[-1]['content'][0]['content'] == blocks
        return {'content': [{'type': 'text', 'text': 'Menu inspected'}]}
    monkeypatch.setattr(llm, 'complete', complete)
    row = kernel_store.session_create('worker', aid, title='menu', status='working', meta={'one_shot': True})
    kernel_store.inbox_put(row['id'], 'brief', 'Inspect this open dropdown')
    asyncio.run(o.workers.worker_for(row).run())
    assert kernel_store.reports_of(row['id'])[0]['summary'] == 'Menu inspected'
    # The image was for that request. It never goes in the record.
    assert 'image-data' not in str(kernel_store.log_all(row['id']))


@pytest.mark.parametrize('args', [{'action': 'click', 'ref': 4}, {'action': 'type', 'ref': 2, 'text': 'hi'},
                                  {'action': 'select', 'ref': 7, 'text': 'May'}, {'action': 'click', 'x': 10, 'y': 20}])
def test_clicking_typing_and_selecting_reach_the_page(isolated, monkeypatch, args):
    """Their activity line once crashed the call before the browser saw it: every
    click, type and select failed with "_activity() missing ... 'tool'"."""
    o = orchestrator.Orchestrator()
    aid = next(iter(o.agents))
    acted = []

    async def act(key, action, a):
        acted.append(action)
        return 'done'

    monkeypatch.setattr(browser, 'act', act)
    monkeypatch.setattr(browser, 'page_desc', lambda key: 'the sign-in page')
    lines = []

    async def activity(a, text, tool, **extra):
        lines.append((text, tool))

    monkeypatch.setattr(o, '_activity', activity)
    assert asyncio.run(o._dispatch_tool(aid, '~', 'browser', args)) == 'done'
    assert acted == [args['action']]
    assert lines and lines[0][1] == 'web' and 'the sign-in page' in lines[0][0]
