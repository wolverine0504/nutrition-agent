"""Run the curry walkthrough in two Chrome sessions against a disposable local workspace."""
import argparse
import json
import re
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from playwright.sync_api import sync_playwright, expect

parser = argparse.ArgumentParser()
parser.add_argument('url')
parser.add_argument('runtime_env', type=Path)
args = parser.parse_args()
url = args.url.rstrip('/')
assert urlparse(url).hostname in {'localhost', '127.0.0.1'}, 'Only send test mutations to a local demo.'
env = dict(line.split('=', 1) for line in args.runtime_env.read_text().splitlines() if '=' in line)
assert env['WEB_ENABLE_MODEL_CALLS'] == 'false'
interaction = env['WEB_INTERACTION_CODE']
presenter = env['WEB_PRESENTER_CODE']

def state():
    with urlopen(url + '/api/state', timeout=8) as r:
        result = json.load(r)
    assert result['model_enabled'] is False
    return result

initial = state()
assert initial['synthetic'] is True
assert initial['goals']['meal']['kcal'] == '1000', 'Use the dedicated curry chicken review workspace.'

def request_reset():
    req = Request(url + '/api/reset', method='POST', headers={'X-Presenter-Code': presenter})
    with urlopen(req, timeout=8) as result:
        assert result.status == 200

def press(page, label, path='/api/messages'):
    button = page.get_by_role('button', name=label, exact=True)
    for _ in range(8):
        with page.expect_response(lambda res: res.url.endswith(path) and res.request.method == 'POST') as result:
            button.click()
        res = result.value
        if res.status != 409:
            return res
        page.wait_for_timeout(140)
    raise AssertionError('Workspace remained busy')

def send(page, message):
    page.get_by_label('Your message').fill(message)
    return press(page, 'Send message')

def authorize(page):
    page.get_by_label('Interaction code').fill(interaction)

def reset_ui(page, code):
    controls = page.get_by_text('Presenter controls', exact=True)
    if controls.get_attribute('aria-expanded') != 'true' and not page.get_by_label('Presenter code').is_visible():
        controls.click()
    page.get_by_label('Presenter code').fill(code)
    listener = lambda dialog: dialog.accept()
    page.on('dialog', listener)
    try:
        return press(page, 'Reset demo', '/api/reset')
    finally:
        page.remove_listener('dialog', listener)

with sync_playwright() as pw:
    request_reset()
    browser = pw.chromium.launch(channel='chrome')
    a, b = [browser.new_context().new_page() for _ in range(2)]
    for p in (a, b):
        p.goto(url)
        expect(p.get_by_text('Offline · model calls disabled')).to_be_visible()
        expect(p.get_by_role('heading', name='Per-meal goals')).to_be_visible()
        expect(p.get_by_role('heading', name='Daily goals')).to_be_visible()
        expect(p.locator('#meal-goals')).to_contain_text('1,000 kcal')
        expect(p.locator('#batches')).to_contain_text('No batches prepared yet')
        expect(p.locator('#daily')).to_contain_text('No meals logged today')
    assert press(a, 'Confirm inventory').status == 403
    authorize(a)
    assert press(a, 'Confirm inventory').status == 200
    recipe = re.compile(r'^curry chicken$', re.I)
    expect(b.get_by_role('button', name=recipe)).to_be_visible(timeout=11000)
    assert press(a, recipe).status == 200
    expect(a.get_by_role('button', name='Adjust to my goals')).to_be_visible()

    assert reset_ui(a, interaction).status == 403
    assert reset_ui(a, presenter).status == 200
    authorize(a)
    assert press(a, 'Confirm inventory').status == 200
    assert send(a, 'curry').status == 200
    expect(a.get_by_role('button', name='Adjust to my goals')).to_be_visible()
    assert press(a, 'Adjust to my goals').status == 200
    expect(b.get_by_text('160 g short', exact=True)).to_be_visible(timeout=11000)
    expect(a.get_by_role('button', name='Accept recipe')).to_be_disabled()
    expect(a.locator('#proposal')).to_contain_text('Nutrition goals met')
    proposal_id = state()['proposal']['id']

    assert press(a, 'Add 200 g chicken').status == 200
    expect(a.locator('#preview')).to_be_visible()
    assert press(a, 'Reject', '/api/decisions').status == 200
    expect(a.get_by_role('button', name='Accept recipe')).to_be_disabled()
    expect(a.get_by_text('160 g short', exact=True)).to_be_visible()

    assert send(a, 'I bought 200 g of raw chicken breast.').status == 200
    expect(a.locator('#preview')).to_be_visible()
    assert press(a, 'Approve changes', '/api/decisions').status == 200
    expect(b.get_by_role('button', name='Accept recipe')).to_be_enabled(timeout=11000)
    assert state()['proposal']['id'] == proposal_id
    expect(a.locator('#inventory')).to_contain_text(re.compile(r'1[,]?200 g'))

    assert press(a, 'Accept recipe').status == 200
    expect(a.get_by_text('Cooking instructions', exact=True)).to_be_visible()
    assert press(a, 'Used plan as written').status == 200
    expect(a.locator('#preview')).to_be_visible()
    assert press(a, 'Approve changes', '/api/decisions').status == 200
    expect(a.locator('#proposal')).to_be_hidden()
    expect(a.locator('#batches')).to_contain_text(re.compile(r'4(?:/4| of 4)? boxes'))
    expect(a.locator('#inventory')).to_contain_text('40 g')
    expect(b.locator('#batches')).to_contain_text(re.compile(r'4(?:/4| of 4)? boxes'), timeout=11000)

    response = send(a, 'I ate one box from the 4-box curry chicken batch I just saved for lunch.')
    assert response.status == 200 and response.json()['ok']
    expect(b.locator('#batches')).to_contain_text(re.compile(r'3(?:/4| of 4)? boxes'), timeout=11000)
    expect(b.locator('#daily')).to_contain_text(re.compile(r'95[67](?:\.\d+)? kcal', re.I), timeout=11000)
    assert state()['meal_choices'] is None
    assert state()['proposal'] is None
    assert not re.search(r'/accept-recipe [\w-]+', a.locator('#messages').inner_text())

    b.reload()
    expect(b.locator('#batches')).to_contain_text(re.compile(r'3(?:/4| of 4)? boxes'))
    expect(b.locator('#daily')).to_contain_text(re.compile(r'95[67](?:\.\d+)? kcal', re.I))
    assert reset_ui(a, presenter).status == 200
    expect(b.locator('#batches')).to_contain_text('No batches prepared yet', timeout=11000)
    expect(b.locator('#daily')).to_contain_text('No meals logged today', timeout=11000)
    browser.close()
    print('PASS: two local Chrome sessions; empty startup; both goals; code permissions; recipe click and natural selection; 160 g shortage; purchase rejection and 200 g purchase approval; unchanged proposal; four boxes saved and 40 g chicken left; three boxes after natural meal logging; daily totals and reload persistence.')
