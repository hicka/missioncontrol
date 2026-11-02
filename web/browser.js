/* The owner's view into the same persistent browser used by agent tools. */
(() => {
  const $ = selector => document.querySelector(selector);
  const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[char]));
  let state = null, busy = false, fetching = false, sessionHTML = '', credentialHTML = '', pendingCount = 0;
  const visible = () => !$('#settings-backdrop').classList.contains('hidden') &&
    !$('#pane-browser').classList.contains('hidden');
  function message(text, error = false) {
    $('#browser-feedback').textContent = text;
    $('#browser-feedback').className = `set-status ${error ? 'err' : 'ok'}`;
  }
  async function request(path, method = 'GET', body) {
    const response = await fetch(`/api/runtime/${path}`, {
      method, headers: {'Content-Type': 'application/json'},
      ...(body === undefined ? {} : {body: JSON.stringify(body)})
    });
    const data = await response.json();
    if (!response.ok || data.ok === false) throw new Error(data.error || 'Browser request failed.');
    return data;
  }
  function render(data) {
    state = data;
    $('#browser-enabled').checked = data.enabled;
    $('#browser-visible').checked = !data.headless;
    $('#browser-auth-channel').value = data.auth_channel || 'auto';
    const ready = data.enabled && data.status === 'running';
    $('#browser-state').textContent = !data.enabled ? 'Off' : !ready ?
      'Start the agent engine in Runtime Manager' : data.running ? 'Browser connected' : 'Ready · opens on demand';
    $('#browser-state').className = `set-status ${ready ? 'ok' : ''}`;
    $('#browser-open').disabled = busy || !ready || data.headless;
    $('#browser-enabled').disabled = $('#browser-visible').disabled = busy;
    $('#browser-auth-channel').disabled = busy;
    const vault = data.vault || {};
    $('#browser-vault-status').textContent = vault.available ? vault.name : vault.error || 'Secure storage unavailable';
    $('#browser-credential-save').disabled = busy || !vault.available;
    const names = Object.fromEntries((data.agents || []).map(a => [a.id, a.name]));
    const html = data.sessions.map(tab => `<div class="set-card browser-session">
      <div><strong>${escape(tab.title)}</strong></div>
      <div class="set-row-note">${escape(tab.agent === 'owner' ? 'Your login tab' : names[tab.agent] || tab.agent)} · ${escape(tab.url)}</div>
      <div class="set-row"><button class="btn" data-browser-session="${escape(tab.id)}" data-action="focus" ${data.headless || busy ? 'disabled' : ''}>Show tab</button>
      <button class="btn" data-browser-session="${escape(tab.id)}" data-action="close" ${busy ? 'disabled' : ''}>Close tab</button></div>
      </div>`).join('') || '<div class="set-hint">No open tabs. Open the browser to sign in, or give an agent a website task.</div>';
    // Preserve buttons and keyboard focus when the live snapshot is unchanged.
    if (sessionHTML !== html) { $('#browser-sessions').innerHTML = html; sessionHTML = html; }
    $('#browser-agents').textContent = (data.agents || []).map(a => `${a.name}: ${a.allowed ? 'browser allowed' : 'browser restricted'}`).join(' · ');
    renderCredentials(data.credentials || []);
    renderRequests(data.requests || [], names);
  }
  function renderCredentials(entries) {
    const html = entries.map(entry => `<div class="browser-credential">
      <strong>${escape(entry.label)}</strong><div class="set-row-note">${escape(entry.origin)}</div>
      <div class="set-row"><button class="btn" data-credential-edit="${escape(entry.id)}">Update login</button>
      <button class="btn" data-credential-delete="${escape(entry.id)}">Forget login</button></div></div>`).join('') ||
      '<div class="set-hint">No saved logins yet. Add a website account below, or save it when an agent asks you to sign in.</div>';
    if (html !== credentialHTML) { $('#browser-credentials').innerHTML = html; credentialHTML = html; }
  }
  function renderRequests(requests, names) {
    const container = $('#browser-auth-requests');
    const ids = new Set(requests.map(row => row.id));
    for (const card of container.querySelectorAll('[data-auth-request]')) {
      if (!ids.has(card.dataset.authRequest)) card.remove();
    }
    for (const row of requests) {
      let card = container.querySelector(`[data-auth-request="${CSS.escape(row.id)}"]`);
      if (!card) {
        card = document.createElement('div');
        card.className = 'set-card browser-auth-request';
        card.dataset.authRequest = row.id;
        const login = row.kind === 'login';
        const accounts = row.accounts || [];
        card.innerHTML = `<h3>${escape(row.origin)}</h3><div class="set-row-note">${escape(names[row.agent] || row.agent)} is waiting · <span data-auth-expiry></span></div>
          <div class="set-hint">${escape(row.message)}</div><div class="set-row-note" data-auth-delivery></div>
          <form data-auth-form="${escape(row.id)}" autocomplete="off">
          ${login ? `${accounts.length ? `<div class="set-row"><select name="credential_id" class="set-control" aria-label="Saved account"><option value="">Enter another login</option>${accounts.map(account => `<option value="${escape(account.id)}">${escape(account.label)}</option>`).join('')}</select></div>` : ''}
            <div class="set-row"><input name="username" class="set-control" placeholder="Username or email" aria-label="Login username" autocomplete="off" maxlength="320"><input name="password" type="password" class="set-control" placeholder="Password" aria-label="Login password" autocomplete="new-password" maxlength="4096"></div>
            <label class="na-check"><input type="checkbox" name="remember" checked><span>Save login in the OS vault for this website</span></label>` :
            row.kind === 'manual' ? '<div class="set-row-note">Complete the step in the managed browser, then choose Continue.</div>' :
            `<div class="set-row"><input name="value" type="password" class="set-control" required placeholder="${row.kind === 'code' ? 'Verification code' : 'Private login response'}" aria-label="${row.kind === 'code' ? 'Verification code' : 'Private login response'}" autocomplete="${row.kind === 'code' ? 'one-time-code' : 'off'}" maxlength="4096"></div>`}
          <div class="set-row"><button class="btn primary" type="submit">${row.kind === 'manual' ? 'Continue' : 'Use securely and continue'}</button><button class="btn" type="button" data-auth-cancel="${escape(row.id)}">Cancel request</button></div>
          <div class="set-status" data-auth-feedback role="status"></div></form>`;
        container.appendChild(card);
      }
      const seconds = Math.max(0, Math.ceil(row.expires - Date.now() / 1000));
      card.querySelector('[data-auth-expiry]').textContent = `expires in ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
      card.querySelector('[data-auth-delivery]').textContent = row.delivery && row.delivery !== 'app'
        ? `${row.delivery}: ${row.delivery_status === 'sent' ? 'nudge sent' : row.delivery_status === 'failed' ? 'nudge failed; enter the code here' : 'sending nudge / awaiting delivery'}. You can reply with the code in your private chat.` : '';
    }
    pendingCount = requests.length;
    $('#browser-auth-alert').classList.toggle('hidden', !pendingCount || visible());
    $('#browser-auth-alert-text').textContent = `${pendingCount} website sign-in${pendingCount === 1 ? '' : 's'} need your input.`;
  }
  async function refresh() {
    if (fetching || busy) return;
    fetching = true;
    try { render(await request('browser')); }
    catch (error) { message(error.message, true); }
    finally { fetching = false; }
  }
  async function act(path, method, body, working, success) {
    if (busy) return;
    busy = true;
    if (state) render(state);
    message(working);
    let ok = false;
    try {
      await request(path, method, body);
      ok = true;
      message(success);
    } catch (error) { message(error.message, true); }
    finally { busy = false; await refresh(); }
    return ok;
  }
  $('#browser-enabled').addEventListener('change', event => {
    const enabled = event.target.checked;
    act(`services/browser/${enabled ? 'start' : 'stop'}`, 'POST', undefined,
      enabled ? 'Enabling website access…' : 'Closing the browser…',
      enabled ? 'Website access enabled.' : 'Website access disabled. Saved sign-ins are preserved.');
  });
  $('#browser-visible').addEventListener('change', event => {
    act('browser/settings', 'PUT', {headless: !event.target.checked},
      'Updating browser visibility…', 'Visibility updated. Reopen the browser when ready.');
  });
  $('#browser-auth-channel').addEventListener('change', event => {
    act('browser/settings', 'PUT', {auth_channel: event.target.value}, 'Updating 2FA delivery…', '2FA delivery updated.');
  });
  $('#browser-auth-review').addEventListener('click', () => window.openSettings('browser'));
  $('#browser-credential-form').addEventListener('submit', async event => {
    event.preventDefault();
    const ok = await act('browser/credentials', 'POST', {
      url: $('#browser-credential-url').value.trim(), label: $('#browser-credential-label').value.trim(),
      username: $('#browser-credential-username').value, password: $('#browser-credential-password').value,
      credential_id: $('#browser-credential-id').value || null
    }, 'Saving in the OS credential vault…', 'Login saved securely for this website.');
    if (ok) { $('#browser-credential-form').reset(); $('#browser-credential-id').value = ''; $('#browser-credential-cancel').classList.add('hidden'); }
  });
  $('#browser-credential-cancel').addEventListener('click', () => {
    $('#browser-credential-form').reset(); $('#browser-credential-id').value = ''; $('#browser-credential-cancel').classList.add('hidden');
  });
  $('#browser-credentials').addEventListener('click', event => {
    const edit = event.target.closest('[data-credential-edit]');
    if (edit) {
      const row = state.credentials.find(row => row.id === edit.dataset.credentialEdit);
      if (!row) return;
      $('#browser-credential-form').reset(); $('#browser-credential-id').value = row.id;
      $('#browser-credential-url').value = row.origin; $('#browser-credential-label').value = row.label;
      $('#browser-credential-cancel').classList.remove('hidden'); $('#browser-credential-username').focus();
      return;
    }
    const button = event.target.closest('[data-credential-delete]');
    if (!button) return;
    if (button.dataset.confirm !== 'yes') { button.dataset.confirm = 'yes'; button.textContent = 'Confirm forget'; return; }
    act(`browser/credentials/${encodeURIComponent(button.dataset.credentialDelete)}`, 'DELETE', undefined,
      'Removing saved login…', 'Saved login removed. Existing website sessions remain signed in.');
  });
  async function answer(form, values) {
    const feedback = form.querySelector('[data-auth-feedback]');
    const buttons = [...form.querySelectorAll('button')];
    buttons.forEach(button => button.disabled = true);
    feedback.textContent = 'Sending directly to the waiting browser…';
    try {
      await request(`browser/requests/${encodeURIComponent(form.dataset.authForm)}`, 'POST', values);
      form.reset(); feedback.textContent = 'Response received. The agent is continuing.'; await refresh();
    } catch (error) { feedback.textContent = error.message; }
    finally { buttons.forEach(button => button.disabled = false); }
  }
  $('#browser-auth-requests').addEventListener('submit', event => {
    const form = event.target.closest('[data-auth-form]');
    if (!form) return;
    event.preventDefault();
    const values = Object.fromEntries(new FormData(form));
    values.remember = values.remember === 'on'; values.confirmed = true;
    if (!values.credential_id) delete values.credential_id;
    answer(form, values);
  });
  $('#browser-auth-requests').addEventListener('click', event => {
    const button = event.target.closest('[data-auth-cancel]');
    if (button) answer(button.closest('form'), {cancel: true});
  });
  $('#browser-open-form').addEventListener('submit', event => {
    event.preventDefault();
    act('browser/open', 'POST', {url: $('#browser-url').value.trim()},
      'Opening your browser…', 'Browser opened. Sign in there, then give an agent your task in chat.');
  });
  $('#browser-sessions').addEventListener('click', event => {
    const button = event.target.closest('[data-browser-session]');
    if (!button) return;
    act(`browser/sessions/${encodeURIComponent(button.dataset.browserSession)}/${button.dataset.action}`,
      'POST', undefined, 'Updating tab…', button.dataset.action === 'close' ? 'Tab closed.' : 'Tab brought to front.');
  });
  $('#browser-refresh').addEventListener('click', refresh);
  window.addEventListener('mc:event', event => {
    if (event.detail.type === 'browser_auth_updated' || visible() && ['browser_updated', 'runtime_updated'].includes(event.detail.type)) refresh();
  });
  setInterval(() => { if ((visible() || pendingCount) && !document.hidden) refresh(); }, 3000);
  window.BrowserPanel = {refresh};
  refresh();
})();
