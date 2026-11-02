"""Credentials never become chat/model data; challenges are origin/sender bound."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import auth_channels
import browser_auth
import channels
import credentials
import main
import settings
import secret_mask


class MemoryVault:
    def __init__(self):
        self.values = {}

    def set_password(self, service, account, value):
        self.values[service, account] = value

    def get_password(self, service, account):
        return self.values.get((service, account))

    def delete_password(self, service, account):
        self.values.pop((service, account), None)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    vault = MemoryVault()
    monkeypatch.setattr(credentials, 'INDEX', tmp_path / 'secrets' / 'websites.json')
    monkeypatch.setattr(credentials, 'ROOT', tmp_path)
    monkeypatch.setattr(credentials, 'backend', lambda: vault)
    monkeypatch.setattr(settings, 'SETTINGS_PATH', tmp_path / 'settings.yaml')
    monkeypatch.setattr(browser_auth, '_requests', {})
    monkeypatch.setattr(browser_auth, 'emit', AsyncMock())
    monkeypatch.setattr(auth_channels, '_direct', {})
    monkeypatch.setattr(auth_channels, 'source_for', lambda aid: ('gui', None))
    ch = SimpleNamespace(connected=True)
    manager = SimpleNamespace(get=lambda name: ch if name == 'imessage' else None, send=AsyncMock(return_value=True))
    monkeypatch.setattr(channels, 'manager', manager)
    settings.update('channels', {'imessage': {'enabled': True, 'allowed_senders': ['+15550001111', 'owner@example.test']}})
    return vault, manager


def page(url='https://accounts.example.test/login'):
    return SimpleNamespace(url=url, is_closed=lambda: False)


async def wait_pending(count=1):
    for _ in range(100):
        if len(browser_auth.pending()) == count:
            await asyncio.sleep(0)  # Let the nudge bind its destination.
            return browser_auth.pending()
        await asyncio.sleep(.001)
    raise AssertionError('Expected a private prompt')


def test_credentials_only_os_vault_holds_secrets_and_origin_cannot_be_rebound(isolated):
    row = credentials.save('https://ACCOUNTS.example.test:443/login', 'private-user', 'secret-password', 'Work account')
    assert row['origin'] == 'https://accounts.example.test'
    raw = credentials.INDEX.read_text()
    assert 'private-user' not in raw and 'secret-password' not in raw
    assert credentials.get(row['id'], row['origin'])['password'] == 'secret-password'
    with pytest.raises(credentials.VaultError):
        credentials.get(row['id'], 'https://evil.example.test')
    # Editing the index cannot make the encrypted record valid on another site.
    index = json.loads(raw); index[0]['origin'] = 'https://evil.example.test'
    credentials.INDEX.write_text(json.dumps(index))
    with pytest.raises(credentials.VaultError, match='different website'):
        credentials.get(row['id'], 'https://evil.example.test')


@pytest.mark.parametrize('url', ['http://example.test', 'file:///tmp/page', 'https://user:pw@example.test', 'https://example.test:bad'])
def test_credential_origin_rejects_unsafe_addresses(url):
    with pytest.raises(credentials.VaultError):
        credentials.origin(url)


def test_forgetting_a_login_removes_only_its_vault_record(isolated):
    vault, _ = isolated
    first = credentials.save('https://one.example.test', 'user', 'first-password')
    second = credentials.save('https://two.example.test', 'user', 'second-password')
    credentials.delete(first['id'])
    assert len(vault.values) == 1
    assert credentials.list_entries()[0]['id'] == second['id']
    assert credentials.get(second['id'], second['origin'])['password'] == 'second-password'


def test_vault_failure_never_echoes_a_secret(isolated, monkeypatch):
    vault, _ = isolated
    def fail(*args):
        raise RuntimeError('secret-password must never appear')
    monkeypatch.setattr(vault, 'set_password', fail)
    with pytest.raises(credentials.VaultError) as error:
        credentials.save('https://example.test', 'user', 'secret-password')
    assert 'secret-password' not in str(error.value)


def test_private_api_never_returns_secrets_even_in_validation_errors(isolated):
    client = TestClient(main.app, client=('127.0.0.1', 5000))
    values = {'url': 'https://example.test', 'username': 'private-user', 'password': 'secret-password'}
    response = client.post('/api/runtime/browser/credentials', json=values)
    assert response.status_code == 200
    assert all(secret not in response.text for secret in ('secret-password', 'private-user'))
    response = client.post('/api/runtime/browser/credentials', json={**values, 'url': 42})
    assert response.status_code == 422
    assert 'secret-password' not in response.text
    remote = TestClient(main.app, client=('192.0.2.5', 3000))
    assert remote.post('/api/runtime/browser/credentials', json=values).status_code == 403
    assert client.post('/api/runtime/browser/credentials', json=values,
                       headers={'Origin': 'https://evil.example.test'}).status_code == 403


@pytest.mark.parametrize('code', ['123456', 'A1B2C3'])
def test_code_reply_bypasses_conversation_and_only_reaches_matching_sender(isolated, monkeypatch, code):
    _, manager = isolated
    user_message = AsyncMock()
    monkeypatch.setattr(main.orch, 'user_message', user_message)
    async def run():
        task = asyncio.create_task(browser_auth._ask('webdev', page(), 'code'))
        pending = (await wait_pending())[0]
        await main._channel_message('imessage', 'iMessage;-;+15550002222', '+15550002222', code)
        assert not task.done()
        await main._channel_message('imessage', 'iMessage;+;group', '+15550001111', code)
        assert not task.done()
        await main._channel_message('imessage', 'iMessage;-;+15550001111', '+15550001111', code)
        answer = await task
        assert answer['value'] == code
        assert not browser_auth.pending()
        user_message.assert_not_awaited()
        assert code not in str(manager.send.call_args_list)
        assert code not in str(browser_auth.emit.call_args_list)
        with pytest.raises(browser_auth.AuthError):
            await browser_auth.answer(pending['id'], {'value': '999999'})
    asyncio.run(run())


def test_concurrent_logins_require_ids_even_after_one_completes(isolated):
    async def run():
        first = asyncio.create_task(browser_auth._ask('a', page(), 'code'))
        first_row = (await wait_pending())[0]
        second = asyncio.create_task(browser_auth._ask('b', page('https://other.example.test'), 'code'))
        await wait_pending(2)
        route = ('imessage', 'iMessage;-;+15550001111', '+15550001111')
        assert await auth_channels.consume(*route, '123456')
        assert not first.done() and not second.done()
        assert await auth_channels.consume(*route, f"2fa {first_row['id'][:8]} 123456")
        assert (await first)['value'] == '123456'
        assert await auth_channels.consume(*route, '123456')
        assert not second.done()  # An old/duplicate bare reply cannot target the other login.
        second_row = browser_auth.pending()[0]
        assert await auth_channels.consume(*route, f"2fa {second_row['id'][:8]} 654321")
        assert (await second)['value'] == '654321'
    asyncio.run(run())


def test_expired_codes_are_consumed_without_chat_fallback(isolated, monkeypatch):
    monkeypatch.setattr(browser_auth, 'WAIT_SECONDS', .02)
    user_message = AsyncMock()
    monkeypatch.setattr(main.orch, 'user_message', user_message)
    async def run():
        with pytest.raises(browser_auth.AuthError, match='expired'):
            await browser_auth._ask('a', page(), 'code')
        assert not browser_auth.pending()
        await main._channel_message('imessage', 'iMessage;-;+15550001111', '+15550001111', '654321')
        user_message.assert_not_awaited()
        await main._channel_message('imessage', 'iMessage;-;+15550001111', '+15550001111', 'Check my dashboard')
        user_message.assert_awaited_once()
    asyncio.run(run())


def test_closed_page_and_cancelled_tools_remove_pending_prompts(isolated):
    async def run():
        target = page()
        task = asyncio.create_task(browser_auth._ask('a', target, 'code'))
        await wait_pending()
        browser_auth.cancel_page(target)
        with pytest.raises(browser_auth.AuthError):
            await task
        assert not browser_auth.pending()
        task = asyncio.create_task(browser_auth._ask('a', target, 'login'))
        await wait_pending()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not browser_auth.pending()
    asyncio.run(run())


def test_login_form_checks_prevent_cross_site_or_password_change_filling():
    valid = {'connected': True, 'visible': True, 'disabled': False, 'tag': 'INPUT', 'type': 'password',
             'url': 'https://example.test/login', 'action': 'https://example.test/session',
             'identity': 'password', 'autocomplete': 'current-password'}
    async def run():
        handle = SimpleNamespace(evaluate=AsyncMock(return_value=valid))
        await browser_auth._check(handle, 'https://example.test', 'password')
        for override in ({'action': 'https://other.example.test/login'}, {'connected': False},
                         {'autocomplete': 'new-password'}, {'identity': 'confirm_password'}, {'autocomplete': 'cc-number'}):
            handle.evaluate.return_value = {**valid, **override}
            with pytest.raises((browser_auth.AuthError, credentials.VaultError)):
                await browser_auth._check(handle, 'https://example.test', 'password')
    asyncio.run(run())


def test_website_echoes_are_redacted_without_keeping_plaintext(monkeypatch):
    from collections import deque
    monkeypatch.setattr(secret_mask, '_recent', deque(maxlen=96))
    secret_mask.protect('opaque-password')
    secret_mask.protect('654321')
    output = secret_mask.redact('Website echoed opaque-password and code 654321.')
    assert 'opaque-password' not in output and '654321' not in output
    assert '[private input]' in output
    assert 'opaque-password' not in str(secret_mask._recent)
    assert '654321' not in str(secret_mask._recent)
