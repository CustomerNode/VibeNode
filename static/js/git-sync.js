/* git-sync.js — git status polling and sync actions */

let _gitStatus = {};

async function pollGitStatus() {
  try {
    const res = await fetch('/api/git-status');
    const s = await res.json();
    _gitStatus = s;
    const hasPush = s.ahead > 0 || s.uncommitted;
    const hasPull = s.behind > 0;
    const btnUpdate  = document.getElementById('btn-git-update');
    const btnPublish = document.getElementById('btn-git-publish');
    const btnSync    = document.getElementById('btn-git-sync');
    if (hasPull && hasPush) {
      // Both directions — show single Sync button
      btnUpdate.style.display = 'none';
      btnPublish.style.display = 'none';
      document.getElementById('git-badge-sync').textContent = '\u2193\u2191';
      btnSync.style.display = 'inline-flex';
    } else {
      btnSync.style.display = 'none';
      if (hasPull) {
        document.getElementById('git-badge-pull').textContent = '\u2193';
        btnUpdate.style.display = 'inline-flex';
      } else {
        btnUpdate.style.display = 'none';
      }
      if (hasPush) {
        document.getElementById('git-badge-push').textContent = '\u2191';
        btnPublish.style.display = 'inline-flex';
      } else {
        btnPublish.style.display = 'none';
      }
    }
  } catch(e) {}
}

function openGitPublish() {
  const s = _gitStatus;
  const hasPush = s.ahead > 0 || s.uncommitted;
  if (!hasPush) {
    showGitSyncModal('Publish App Update', '<p style="color:#aaa">Nothing to publish \u2014 your app is already up to date on remote.</p>',
      [{label:'OK', onclick: closeGitSyncModal}]);
    return;
  }
  let body = '<p>Your local changes are ready to publish.</p>'
    + '<p style="color:#888;font-size:12px">They will be saved and uploaded to remote automatically.</p>';
  if (s.behind > 0) {
    body += '<p style="color:#aaa;font-size:12px;margin-top:8px">'
      + s.behind + ' remote update(s) will be pulled in first, then your changes pushed.</p>';
  }
  showGitSyncModal('Publish App Update', body, [
    {label: 'Publish Now', primary: true, onclick: () => executeGitAction('both', 'btn-git-publish', 'Publish App Update')},
    {label: 'Cancel', onclick: closeGitSyncModal}
  ]);
}

function openGitSyncBoth() {
  const s = _gitStatus;
  let body = '<p><b style="color:#fff">' + s.behind + ' update(s)</b> to pull and '
    + '<b style="color:#fff">' + (s.ahead + (s.uncommitted ? 1 : 0)) + ' change(s)</b> to push.</p>'
    + '<p style="color:#888;font-size:12px">Remote updates will be pulled first, merge conflicts resolved automatically, then your changes pushed.</p>';
  showGitSyncModal('Sync App', body, [
    {label: 'Sync Now', primary: true, onclick: () => executeGitAction('both', 'btn-git-sync', 'Sync App')},
    {label: 'Cancel', onclick: closeGitSyncModal}
  ]);
}

function openGitUpdate() {
  const s = _gitStatus;
  if (s.behind === 0) {
    showGitSyncModal('Update App', '<p style="color:#aaa">Your app is already up to date.</p>',
      [{label:'OK', onclick: closeGitSyncModal}]);
    return;
  }
  showGitSyncModal('Update App', '<p><b style="color:#fff">' + s.behind + ' update(s)</b> are available from remote.</p>'
    + '<p style="color:#888;font-size:12px">Your app will be updated to the latest version. Your local changes are safe.</p>', [
    {label: 'Update Now', primary: true, onclick: () => executeGitAction('pull', 'btn-git-update', 'Update App')},
    {label: 'Cancel', onclick: closeGitSyncModal}
  ]);
}

function showGitSyncModal(title, body, btns) {
  document.getElementById('git-sync-title').textContent = title;
  document.getElementById('git-sync-body').innerHTML = body;
  const acts = document.getElementById('git-sync-actions');
  acts.innerHTML = '';
  btns.forEach(b => {
    const el = document.createElement('button');
    el.className = 'btn' + (b.primary ? ' primary' : '');
    el.textContent = b.label;
    el.onclick = b.onclick;
    acts.appendChild(el);
  });
  document.getElementById('git-sync-overlay').classList.add('show');
}

function closeGitSyncModal() {
  document.getElementById('git-sync-overlay').classList.remove('show');
}

async function executeGitAction(action, btnId, btnLabel) {
  closeGitSyncModal();
  const btn = document.getElementById(btnId);
  btn.disabled = true;
  try {
    const res = await fetch('/api/git-sync', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({action})
    });
    const r = await res.json();
    const body = '<ul style="margin:10px 0 0 16px;color:#bbb;">'
      + r.messages.map(m => '<li>' + escHtml(m) + '</li>').join('') + '</ul>';
    showGitSyncModal(r.ok ? btnLabel + ' \u2713' : 'Problem', body,
      [{label:'OK', primary:true, onclick: closeGitSyncModal}]);
    await pollGitStatus();
  } catch(e) {
    showGitSyncModal('Error', '<p style="color:#f88">Could not complete. Try again.</p>',
      [{label:'OK', onclick: closeGitSyncModal}]);
  } finally {
    btn.disabled = false;
  }
}
