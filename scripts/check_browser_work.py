"""Exercise the real agent browser against an offline website fixture.

No model calls, external sites, or owner profile data. Pass --executable to
test the Chromium bundled inside the native application.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile


async def check():
    import browser
    import browser_auth
    import credentials
    import channels
    import auth_channels
    import settings
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    class MemoryVault:
        def __init__(self): self.values = {}
        def set_password(self, service, account, value): self.values[service, account] = value
        def get_password(self, service, account): return self.values.get((service, account))
        def delete_password(self, service, account): self.values.pop((service, account), None)
    vault = MemoryVault()
    credentials.backend = lambda: vault
    channel = SimpleNamespace(connected=True)
    channels.manager = SimpleNamespace(get=lambda name: channel if name == 'imessage' else None,
                                       send=AsyncMock(return_value=True))
    settings.update('channels', {'imessage': {'enabled': True, 'allowed_senders': ['+15550001111']}})
    settings.update('browser', {'headless': True})
    async def fixture(route):
        path = route.request.url.rsplit('/', 1)[-1]
        if path == 'popup':
            html = '<title>New site tab</title><h1>Popup destination verified</h1>'
        elif path == 'login':
            html = '<title>Fixture sign-in</title><p>Fixture signed in</p>'
        elif path == 'signin':
            html = '''<title>Credential fixture</title><h1>Sign in</h1>
              <form onsubmit="event.preventDefault()">
              <input id="username" autocomplete="username" placeholder="Email">
              <input id="password" type="password" autocomplete="current-password">
              <button id="login-submit" onclick="document.querySelector('#challenge').hidden=false">Sign in</button>
              </form><div id="challenge" hidden><p>Verification code</p>
              <input class="otp" autocomplete="one-time-code" maxlength="1"><input class="otp" autocomplete="one-time-code" maxlength="1">
              <input class="otp" autocomplete="one-time-code" maxlength="1"><input class="otp" autocomplete="one-time-code" maxlength="1">
              <input class="otp" autocomplete="one-time-code" maxlength="1"><input class="otp" autocomplete="one-time-code" maxlength="1">
              <button id="verify" onclick="if([...document.querySelectorAll('.otp')].map(x=>x.value).join('')==='135790') document.querySelector('h1').textContent='Signed in successfully'">Verify</button></div>'''
        elif path == 'profiles':
            html = '''<title>Profile switch regression</title>
              <style>#profile {position:fixed;right:20px;top:20px;cursor:pointer;padding:12px;background:#def}
              #menu {position:fixed;right:20px;top:75px;background:#eee;padding:20px}
              #pointer {position:fixed;right:20px;top:300px;cursor:pointer;padding:12px}
              #hidden-link {visibility:hidden}</style>
              <a id="details" href="/details">Alex Example</a>
              <div id="profile" data-kt-menu-trigger="click">Alex Example</div>
              <div id="menu" hidden><input placeholder="Search by name or number">
              <a id="self" href="#">Self</a>
              <a id="company" href="#">Example Company</a></div>
              <div id="pointer">Custom pointer control</div>
              <a id="hidden-link" href="/wrong">Hidden control</a>
              <h1 id="active">Active profile: Self</h1>
              <input type="password" value="private-browser-value">
              <script>
              profile.addEventListener('click',()=>{menu.hidden=!menu.hidden});
              company.addEventListener('click',e=>{e.preventDefault();active.textContent='Active profile: Example Company';menu.hidden=true});
              pointer.addEventListener('click',()=>{active.textContent='Pointer clicked'});
              </script>'''
        else:
            html = '''<title>Website task fixture</title><h1>Draft editor</h1>
              <input id="secret" type="password" value="secret-password">
              <input id="otp" autocomplete="one-time-code" value="246810">
              <input id="card" autocomplete="cc-number" value="4111111111111111">
              <select id="expiry" autocomplete="cc-exp-month"><option value="01">January</option></select>
              <input id="draft" placeholder="Draft title">
              <button onclick="document.querySelector('h1').textContent='Draft saved: '+document.querySelector('#draft').value">Save draft</button>
              <a href="/popup" target="_blank">Open related record</a>'''
        headers = {'content-type': 'text/html'}
        if path == 'login':
            headers['set-cookie'] = 'mc_fixture_session=verified; Max-Age=3600; Path=/'
        await route.fulfill(status=200, body=html, headers=headers)
    try:
        page = await browser._ensure_page('webdev')
        await browser._context.route('**/*', fixture)
        result = await browser.act('webdev', 'goto', {'url': 'https://missioncontrol.test/editor'})
        assert 'Draft editor' in result, result
        for secret in ('secret-password', '246810', '4111111111111111'):
            assert secret not in result
        for selector in ('#secret', '#otp', '#card', '#expiry'):
            ref = int(await page.locator(selector).get_attribute('data-oref'))
            action = 'select' if selector == '#expiry' else 'type'
            result = await browser.act('webdev', action, {'ref': ref, 'text': 'private-value'})
            assert result.startswith('Blocked:'), result
        ref = int(await page.locator('#draft').get_attribute('data-oref'))
        await browser.act('webdev', 'type', {'ref': ref, 'text': 'Verified website task'})
        ref = int(await page.locator('button').get_attribute('data-oref'))
        result = await browser.act('webdev', 'click', {'ref': ref})
        assert 'Draft saved: Verified website task' in result, result
        ref = int(await page.locator('a').get_attribute('data-oref'))
        result = await browser.act('webdev', 'click', {'ref': ref})
        assert 'Popup destination verified' in result, result
        assert browser._pages['webdev'] is not page
        tabs = (await browser.snapshot())['sessions']
        assert len(tabs) == 2 and all(t['agent'] == 'webdev' for t in tabs), tabs
        owner = await browser._ensure_page('owner')
        await owner.goto('https://missioncontrol.test/login')
        qa = await browser._ensure_page('qa')
        assert qa is owner  # continue the owner's open tab, not a blank duplicate
        await qa.goto('https://missioncontrol.test/editor')
        assert await qa.evaluate("document.cookie") == 'mc_fixture_session=verified'
        qa_id = next(t['id'] for t in (await browser.snapshot())['sessions'] if t['agent'] == 'qa')
        await browser.session_action(qa_id, 'close')
        assert qa.is_closed() and not browser._pages.get('qa')
        await browser._reset()
        assert not (await browser.snapshot())['running']
        assert not (await browser.snapshot())['sessions']
        await browser._ensure_page('webdev')
        assert any(c['name'] == 'mc_fixture_session' and c['value'] == 'verified'
                   for c in await browser._context.cookies('https://missioncontrol.test'))
        await browser._context.route('**/*', fixture)
        credentials.save('https://missioncontrol.test', 'fixture-user', 'fixture-password', 'Fixture account')
        result = await browser.act('webdev', 'goto', {'url': 'https://missioncontrol.test/signin'})
        page = browser._pages['webdev']
        async def ref(selector): return int(await page.locator(selector).get_attribute('data-oref'))
        result = await browser.act('webdev', 'login', {'username_ref': await ref('#username'), 'password_ref': await ref('#password')})
        assert 'fields filled' in result, result
        assert await page.locator('#username').input_value() == 'fixture-user'
        assert await page.locator('#password').input_value() == 'fixture-password'
        snapshot = await browser.act('webdev', 'read', {})
        assert 'fixture-password' not in snapshot and 'fixture-user' not in snapshot
        await browser.act('webdev', 'click', {'ref': await ref('#login-submit')})
        refs = [int(await el.get_attribute('data-oref')) for el in await page.locator('.otp').all()]
        task = asyncio.create_task(browser.act('webdev', 'request_input', {'kind': 'code', 'refs': refs}))
        for _ in range(100):
            if browser_auth.pending() and browser_auth.pending()[0].get('delivery'):
                break
            await asyncio.sleep(.05)
        assert browser_auth.pending()[0]['delivery'] == 'imessage'
        assert await auth_channels.consume('imessage', 'iMessage;-;+15550001111', '+15550001111', '135790')
        result = await task
        assert 'entered directly' in result, result
        assert '135790' not in result and '135790' not in str(channels.manager.send.call_args_list)
        result = await browser.act('webdev', 'click', {'ref': await ref('#verify')})
        assert 'Signed in successfully' in result, result
        # The same credential must not fill a form that submits to another origin.
        await page.locator('form').evaluate("el => el.action='https://different.test/session'")
        result = await browser.act('webdev', 'login', {'password_ref': await ref('#password')})
        assert result.startswith('Blocked:') and 'different website' in result, result
        # A missing login asks the owner and persists only when they choose it.
        await browser.act('webdev', 'goto', {'url': 'https://new-account.test/signin'})
        task = asyncio.create_task(browser.act('webdev', 'login', {'username_ref': await ref('#username'), 'password_ref': await ref('#password')}))
        for _ in range(100):
            if browser_auth.pending(): break
            await asyncio.sleep(.05)
        pending = browser_auth.pending()[0]
        assert pending['kind'] == 'login'
        await browser_auth.answer(pending['id'], {'username': 'second-user', 'password': 'second-password', 'remember': True})
        assert 'fields filled' in await task
        assert len(credentials.list_entries('https://new-account.test')) == 1
        snapshot = await browser.act('webdev', 'read', {})
        assert 'second-password' not in snapshot and 'second-user' not in snapshot
        # Reproduce a real site: identical names, a non-button top-right menu trigger,
        # and an owner-opened menu that must survive the next agent turn.
        await browser.act('webdev', 'goto', {'url': 'https://missioncontrol.test/profiles'})
        result = await browser.act('webdev', 'read', {})
        profile_ref = await ref('#profile')
        line = next(line for line in result.splitlines() if line.startswith(f'[{profile_ref}]'))
        assert 'top-right' in line and '[menu/toggle]' in line, result
        assert await ref('#details') != profile_ref
        assert 'Hidden control' not in result.split('PAGE TEXT:')[0]
        assert await ref('#pointer')
        await page.locator('#profile').click()  # owner opens the menu
        result = await browser.current_observation('webdev')
        assert 'Example Company' in result and await page.locator('#menu').is_visible()
        assert await ref('#profile') == profile_ref  # menu insertion never reassigns IDs
        company_ref = await ref('#company')
        result = await browser.act('webdev', 'click', {'ref': company_ref})
        assert 'Active profile: Example Company' in result and page.url.endswith('/profiles')
        # Removed elements cannot leave an old ref pointing to a new control.
        await page.locator('#company').evaluate('el=>el.remove()')
        result = await browser.act('webdev', 'click', {'ref': company_ref})
        assert 'stale or ambiguous' in result
        result = await browser.act('webdev', 'click', {'x': 10, 'y': 10})
        assert 'fresh browser screenshot' in result
        capture = await browser.act('webdev', 'screenshot', {})
        import base64
        assert capture.content[1]['type'] == 'image'
        assert base64.b64decode(capture.content[1]['source']['data']).startswith(b'\x89PNG\r\n\x1a\n')
        assert 'private-browser-value' not in str(capture)
        box = await page.locator('#pointer').bounding_box()
        result = await browser.act('webdev', 'click', {'x': box['x'] + box['width']/2, 'y': box['y'] + box['height']/2})
        assert 'Pointer clicked' in result
        # Tab selection observes the existing page without navigating.
        tabs = await browser.snapshot()
        tab_id = next(row['id'] for row in tabs['sessions'] if row['agent'] == 'webdev')
        assert tab_id in await browser.act('webdev', 'tabs', {})
        result = await browser.act('webdev', 'use_tab', {'tab_id': tab_id})
        assert 'Pointer clicked' in result
        assert (await browser.act('cto', 'use_tab', {'tab_id': tab_id})).startswith('Blocked:')
        return {'ok': True, 'checks': ['real browser navigation', 'private field masking',
            'credential and payment input blocked', 'form editing and verified save',
            'popup adoption', 'complete tab inventory', 'shared owner sign-in',
            'owner tab close', 'shutdown clears inventory', 'sign-in persists after restart',
            'vault credentials filled without model exposure', 'messaging 2FA to split inputs',
            'verified sign-in after code', 'cross-origin form blocked', 'private prompt saves new login',
            'custom top-right profile trigger discovered', 'duplicate names distinguished by position',
            'owner-opened menu preserved across turns', 'stable refs after menu insertion',
            'profile switch verified on page', 'stale refs rejected', 'actual browser screenshot image',
            'coordinate fallback requires screenshot', 'existing tab selection and ownership']}
    finally:
        await browser._reset()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--executable')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='mc-browser-check-') as home:
        os.environ['MISSIONCONTROL_HOME'] = home
        os.environ.pop('MISSIONCONTROL_OWNER_PID', None)
        if args.executable:
            os.environ['MISSIONCONTROL_BROWSER_EXECUTABLE'] = args.executable
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
        print(json.dumps(asyncio.run(check())))
