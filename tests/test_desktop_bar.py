"""The floating bar: its page is served, and it stays off until someone turns it on."""
import pytest
from fastapi.testclient import TestClient

import main
import settings


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'SETTINGS_PATH', tmp_path / 'settings.yaml')
    monkeypatch.setattr(main, '_api_token', lambda: 'bar-test-token')
    return TestClient(main.app, headers={'Authorization': 'Bearer bar-test-token'})


def test_the_bar_page_is_served_with_what_it_loads(client):
    page = client.get('/bar')
    assert page.status_code == 200
    assert page.headers['cache-control'] == 'no-cache'
    for script in ('presence.js', 'work.js', 'bar.js'):
        assert f'/static/{script}' in page.text
        assert client.get(f'/static/{script}').status_code == 200


def test_the_bar_is_off_until_asked_for_and_the_choice_is_kept(client):
    assert client.get('/api/desktop').json() == {'floating_bar': False}
    saved = client.put('/api/desktop', json={'floating_bar': True}).json()
    assert saved == {'ok': True, 'floating_bar': True}
    assert settings.load()['desktop']['floating_bar'] is True
    assert client.get('/api/desktop').json() == {'floating_bar': True}
    # Anything but a plain yes or no is refused, not guessed at.
    assert client.put('/api/desktop', json={'floating_bar': 'sometimes'}).status_code == 422


def test_no_face_is_drawn_anywhere_in_the_web_interface():
    """Profiles are presences. A face must not come back through an old helper."""
    for name in ('app.js', 'deck.js', 'work.js', 'presence.js', 'bar.js', 'modules.js', 'panes.js', 'ui.css', 'index.html'):
        text = (main.WEB_DIR / name).read_text()
        for gone in ('faceSVG', 'class="eye', 'class="mouth', '.bot-body'):
            assert gone not in text, f'{gone} in web/{name}'
