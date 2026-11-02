"""UI regression for private sign-in forms; all writes are browser-local mocks.

Point --url at a running Mission Control UI. No credentials are saved and no
messages are sent. Synthetic input values never leave the test browser.
"""
import argparse
import json
from pathlib import Path
import time

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    args = parser.parse_args()
    state = {'enabled': True, 'status': 'running', 'headless': False, 'running': False,
             'sessions': [], 'agents': [{'id': 'webdev', 'name': 'Website agent', 'allowed': True}],
             'vault': {'available': True, 'name': 'macOS Keychain'}, 'credentials': [],
             'requests': [], 'auth_channel': 'auto'}
    actions, errors = [], []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={'width': 1280, 'height': 1000})
        page.on('pageerror', lambda error: errors.append(str(error)))
        def api(route):
            request = route.request
            path = request.url.split('/api/runtime/', 1)[-1]
            result = state
            if request.method != 'GET':
                actions.append((path, request.method))
                data = request.post_data_json if request.post_data else {}
                if path == 'browser/credentials':
                    assert data['username'] == 'fixture-user' and data['password'] == 'fixture-password'
                    state['credentials'] = [{'id': 'test-entry', 'origin': data['url'], 'label': data['label']}]
                elif path.startswith('browser/requests/'):
                    assert data['value'] == '123456'
                    state['requests'] = []
                elif path == 'browser/settings':
                    state.update(data)
                elif path == 'browser/credentials/test-entry' and request.method == 'DELETE':
                    state['credentials'] = []
                else:
                    raise AssertionError('Unexpected mocked action: ' + path)
                result = {'ok': True}
            route.fulfill(status=200, content_type='application/json', body=json.dumps(result))
        page.route('**/api/runtime/browser**', api)
        # All non-browser mutations are blocked, even if UI startup regresses.
        unexpected = []
        def guard(route):
            if route.request.url.endswith('/api/fleet/tailscale'):
                # Existing Settings automatically runs address discovery via a
                # POST endpoint. Keep that read-only discovery mocked too.
                route.fulfill(status=200, content_type='application/json', body='{"ok":false,"error":"Discovery skipped in UI regression"}')
            elif route.request.method not in ('GET', 'HEAD') and '/api/runtime/browser' not in route.request.url:
                unexpected.append(route.request.url)
                route.abort()
            else:
                route.fallback()
        page.route('**/api/**', guard)
        page.goto(args.url, wait_until='networkidle')
        page.locator('#settings-btn').click()
        page.locator('[data-tab="browser"]').click()
        page.locator('#browser-credential-url').fill('https://accounts.example.test')
        page.locator('#browser-credential-label').fill('Work account')
        page.locator('#browser-credential-username').fill('fixture-user')
        page.locator('#browser-credential-password').fill('fixture-password')
        page.locator('#browser-credential-save').click()
        page.get_by_text('Login saved securely for this website.', exact=True).wait_for()
        assert page.locator('#browser-credential-password').input_value() == ''
        assert page.locator('#browser-credentials').get_by_text('Work account', exact=True).count() == 1
        page.locator('#browser-auth-channel').select_option('imessage')
        page.get_by_text('2FA delivery updated.', exact=True).wait_for()
        state['requests'] = [{'id': 'fixture-request', 'origin': 'https://accounts.example.test',
            'agent': 'webdev', 'kind': 'code', 'message': 'Enter the verification code.',
            'expires': time.time() + 300, 'delivery': 'imessage', 'delivery_status': 'sent'}]
        page.evaluate('BrowserPanel.refresh()')
        value = page.locator('[data-auth-form] [name="value"]')
        value.fill('123456')
        page.evaluate('BrowserPanel.refresh()')
        assert value.input_value() == '123456', 'Polling must preserve private input in progress'
        assert value.get_attribute('type') == 'password'
        page.locator('[data-auth-form] button[type="submit"]').click()
        page.locator('[data-auth-form]').wait_for(state='detached')
        page.locator('[data-credential-delete]').click()
        assert page.locator('[data-credential-delete]').inner_text().casefold() == 'confirm forget'
        page.locator('[data-credential-delete]').click()
        page.get_by_text('Saved login removed. Existing website sessions remain signed in.', exact=True).wait_for()
        for width in (1440, 1040, 680, 390):
            page.set_viewport_size({'width': width, 'height': 1000})
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), f'Overflow at {width}'
        page.set_viewport_size({'width': 1280, 'height': 1000})
        output = Path(__file__).resolve().parents[1] / 'build/review'
        output.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output / 'website-login-ui.png'))
        assert not errors, errors
        assert not unexpected, unexpected
        assert len(actions) == 4, actions
        browser.close()
    print(json.dumps({'ok': True, 'checks': ['vault form and secret clearing', '2FA delivery setting',
        'code prompt and response', 'polling preserves private input', 'forget login confirmation',
        'four viewport widths', 'no real writes or messages', 'no JavaScript errors']}))


if __name__ == '__main__':
    main()
