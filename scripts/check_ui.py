"""Read-only browser checks for a running Mission Control preview.

Usage: python scripts/check_ui.py --url http://127.0.0.1:8899
Walks every view in dark, light and phone layouts, opens a conversation and
the search palette, and fails on any page error. It never submits anything:
a POST from this script is itself a failure. Screenshots land in build/review.
Requires Playwright and its Chromium browser (see README).
"""
import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--url', required=True, help='Running local preview URL')
args = parser.parse_args()
base = args.url.rstrip('/')
out = Path(__file__).resolve().parents[1] / 'build' / 'review'
out.mkdir(parents=True, exist_ok=True)
VIEWS = {'home': 'Home', 'inbox': 'Inbox', 'missions': 'Tasks', 'activity': 'Activity', 'schedule': 'Schedule',
         'knowledge': 'Knowledge', 'evolution': 'Evolution', 'modules': 'Modules'}

with sync_playwright() as p:
    browser = p.chromium.launch()
    for scheme in ('dark', 'light'):
        page = browser.new_page(viewport={'width': 1440, 'height': 1000}, color_scheme=scheme)
        errors, posts = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: posts.append(request.url) if request.method != 'GET' else None)
        page.goto(base + '/', wait_until='networkidle')
        page.locator('.home-hero h2').wait_for()
        assert not page.locator('#welcome-backdrop').is_visible(), 'welcome screen showed on a configured app'
        agents = page.evaluate('Object.keys(S.agents).length')
        page.locator('.team-chip:not(.add)').first.wait_for()
        assert page.locator('.team-chip:not(.add)').count() == agents
        # A profile is a presence, never a face.
        assert page.locator('.presence').count() >= agents * 2
        assert page.locator('.bot, .eye, .mouth').count() == 0, 'a face is still drawn somewhere'
        assert page.locator('#convo-list .convo').count() == agents + 1, 'every agent and the team chat'
        # The views used less often sit under More, folded until asked for.
        assert not page.locator('#more-nav').is_visible()
        page.locator('#more-toggle').click()
        for view, title in VIEWS.items():
            page.locator(f'#sidebar [data-deck="{view}"]').click()
            page.wait_for_timeout(500)
            assert page.locator('#deck-title').inner_text() == title
            assert page.locator(f'#sidebar [data-deck="{view}"]').get_attribute('aria-current') == 'page'
            page.screenshot(path=str(out / f'{view}-{scheme}.png'))
        # Charts are drawn at the width they are shown at, so labels are not stretched.
        chart = page.locator('#evo-charts svg.chart').first
        if chart.count():
            drawn = float(chart.get_attribute('width'))
            shown = chart.bounding_box()['width']
            assert abs(drawn - shown) < 4, f'chart drawn at {drawn}px but shown at {shown}px'
        # A conversation, its profile, and the way back.
        page.locator('#convo-list .convo').nth(1).click()
        page.locator('#chat').wait_for(state='visible')
        page.wait_for_timeout(800)
        assert page.locator('#header-name').inner_text().strip()
        assert page.locator('#agent-panel').is_visible(), 'profile panel opens beside a direct chat'
        assert page.locator('#agent-panel .ap-head h3').inner_text().strip() == page.locator('#header-name').inner_text().strip()
        page.screenshot(path=str(out / f'chat-{scheme}.png'))
        page.locator('#panel-btn').click()
        assert not page.locator('#agent-panel').is_visible()
        page.locator('#panel-btn').click()
        # Search palette: opens, filters, closes without acting.
        page.keyboard.press('Meta+K' if 'darwin' in __import__('sys').platform else 'Control+K')
        page.locator('#command-palette').wait_for(state='visible')
        page.locator('#palette-search').fill('evolution')
        assert page.locator('.palette-option').count() >= 1
        page.keyboard.press('Escape')
        assert not page.locator('#command-palette').is_visible()
        page.locator('#sidebar [data-deck="home"]').click()
        assert not errors, errors
        assert not posts, f'a read-only check sent requests: {posts}'
        page.close()
    # Phone width: navigation is a drawer.
    page = browser.new_page(viewport={'width': 390, 'height': 844}, color_scheme='dark')
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(base + '/', wait_until='networkidle')
    page.locator('.home-hero h2').wait_for()
    assert not page.locator('#sidebar').evaluate('el => el.getBoundingClientRect().right > 0')
    page.locator('#control [data-menu]').click()
    page.wait_for_timeout(350)
    assert page.locator('#sidebar').evaluate('el => el.getBoundingClientRect().left >= 0')
    page.screenshot(path=str(out / 'home-phone.png'))
    width = page.evaluate('document.documentElement.scrollWidth')
    assert width <= 390, f'horizontal scroll at phone width ({width}px)'
    assert not errors, errors
    browser.close()
print(f'UI checks passed; screenshots in {out}')
