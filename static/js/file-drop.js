/* file-drop.js — drag-and-drop file landing zone with folder picker */

var _fdFile = null;
var _fdQueue = [];
var _fdProcessing = false;
var _fdSelectedPath = '';
var _fdDragCounter = 0;
var _fdOnPickerDone = null;

// --- Drag overlay ---

document.addEventListener('dragenter', function(e) {
  if (!e.dataTransfer || !e.dataTransfer.types.includes('Files')) return;
  e.preventDefault();
  _fdDragCounter++;
  document.getElementById('file-drop-zone').classList.add('visible');
});

document.addEventListener('dragover', function(e) {
  if (!e.dataTransfer || !e.dataTransfer.types.includes('Files')) return;
  e.preventDefault();
  e.dataTransfer.dropEffect = 'copy';
});

document.addEventListener('dragleave', function(e) {
  _fdDragCounter--;
  if (_fdDragCounter <= 0) {
    _fdDragCounter = 0;
    document.getElementById('file-drop-zone').classList.remove('visible');
  }
});

document.addEventListener('drop', function(e) {
  _fdDragCounter = 0;
  document.getElementById('file-drop-zone').classList.remove('visible');

  if (!e.dataTransfer || !e.dataTransfer.files || e.dataTransfer.files.length === 0) return;
  e.preventDefault();

  // Queue all dropped files
  for (var i = 0; i < e.dataTransfer.files.length; i++) {
    _fdQueue.push(e.dataTransfer.files[i]);
  }
  if (!_fdProcessing) _fdProcessNext();
});

function _fdProcessNext() {
  if (_fdQueue.length === 0) { _fdProcessing = false; return; }
  _fdProcessing = true;
  _fdFile = _fdQueue.shift();
  _fdShowPicker(_fdFile);
}

// --- Folder picker modal ---

async function _fdShowPicker(file) {
  document.getElementById('fd-filename').textContent = file ? file.name : 'Choose a file location';

  // Default to Downloads folder, fall back to project path
  try {
    var res = await fetch('/api/default-save-dir');
    var data = await res.json();
    _fdSelectedPath = data.path || '';
  } catch (e) {
    _fdSelectedPath = '';
  }

  if (_fdSelectedPath) {
    _fdRenderBreadcrumb(_fdSelectedPath);
    _fdLoadTree(_fdSelectedPath);
  }

  document.getElementById('fd-picker-overlay').classList.add('show');
}

function _fdRenderBreadcrumb(fullPath) {
  var el = document.getElementById('fd-breadcrumb');
  // Normalize separators
  var parts = fullPath.replace(/\\/g, '/').split('/').filter(Boolean);
  var html = '';
  for (var i = 0; i < parts.length; i++) {
    var partial = parts.slice(0, i + 1).join('/');
    // On Windows paths like C:/Users/..., reconstruct properly
    if (i === 0 && parts[0].length === 2 && parts[0][1] === ':') {
      partial = parts[0] + '/' + parts.slice(1, i + 1).join('/');
      if (i === 0) partial = parts[0] + '/';
    }
    var isLast = (i === parts.length - 1);
    if (isLast) {
      html += '<span class="fd-crumb-current">' + escHtml(parts[i]) + '</span>';
    } else {
      html += '<span class="fd-crumb" onclick="_fdNavigate(\'' + escHtml(partial.replace(/'/g, "\\'")) + '\')">' + escHtml(parts[i]) + '</span>';
      html += '<span class="fd-crumb-sep">/</span>';
    }
  }
  el.innerHTML = html;
}

async function _fdLoadTree(dirPath) {
  var treeEl = document.getElementById('fd-tree');
  treeEl.innerHTML = '<div style="color:var(--text-faint);font-size:12px;padding:8px;">Loading...</div>';

  try {
    var res = await fetch('/api/browse-dir?path=' + encodeURIComponent(dirPath));
    var data = await res.json();
    if (data.error) {
      treeEl.innerHTML = '<div style="color:var(--text-faint);font-size:12px;padding:8px;">' + escHtml(data.error) + '</div>';
      return;
    }

    _fdSelectedPath = data.path;
    _fdRenderBreadcrumb(data.path);

    if (data.dirs.length === 0) {
      treeEl.innerHTML = '<div style="color:var(--text-faint);font-size:12px;padding:8px;">No subfolders</div>';
      return;
    }

    var html = '';
    for (var i = 0; i < data.dirs.length; i++) {
      var d = data.dirs[i];
      var childPath = data.path.replace(/\\/g, '/') + '/' + d;
      html += '<div class="fd-tree-item" onclick="_fdNavigate(\'' + escHtml(childPath.replace(/'/g, "\\'")) + '\')">';
      html += '<svg class="fd-tree-icon" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>';
      html += '<span>' + escHtml(d) + '</span>';
      html += '<svg class="fd-tree-arrow" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="9 18 15 12 9 6"/></svg>';
      html += '</div>';
    }
    treeEl.innerHTML = html;
  } catch (e) {
    treeEl.innerHTML = '<div style="color:var(--text-faint);font-size:12px;padding:8px;">Error loading directory</div>';
  }
}

function _fdNavigate(path) {
  _fdSelectedPath = path;
  _fdLoadTree(path);
}

// --- Save / Cancel ---

async function fdSave() {
  // Template-triggered picker (no file) — store path and notify caller
  if (!_fdFile && _fdSelectedPath) {
    window._fdTemplatePath = _fdSelectedPath;
    if (typeof _fdOnPickerDone === 'function') {
      _fdOnPickerDone(_fdSelectedPath);
      _fdOnPickerDone = null;
    }
    _fdClosePicker();
    return;
  }
  if (!_fdFile || !_fdSelectedPath) return;

  var btn = document.getElementById('fd-save-btn');
  btn.disabled = true;
  btn.textContent = 'Saving...';

  var formData = new FormData();
  formData.append('file', _fdFile);
  formData.append('target_dir', _fdSelectedPath);

  try {
    var res = await fetch('/api/file-drop', {method: 'POST', body: formData});
    var data = await res.json();

    if (data.ok) {
      // Notify running session if one exists
      if (liveSessionId && runningIds.has(liveSessionId)) {
        socket.emit('send_message', {
          session_id: liveSessionId,
          text: '[file dropped] ' + data.filename + ' is now at ' + data.path
        });
        showToast('File saved and session notified');
      } else {
        showToast('File saved to ' + data.path);
      }
    } else {
      showToast(data.error || 'Upload failed', true);
    }
  } catch (e) {
    showToast('Upload failed', true);
  }

  btn.disabled = false;
  btn.textContent = 'Save here';
  _fdClosePicker();
  _fdProcessNext();
}

function fdCancel() {
  _fdFile = null;
  _fdOnPickerDone = null;
  _fdClosePicker();
  _fdProcessNext();
}

function _fdClosePicker() {
  document.getElementById('fd-picker-overlay').classList.remove('show');
}

// Close on overlay background click
document.getElementById('fd-picker-overlay').addEventListener('click', function(e) {
  if (e.target === this) fdCancel();
});
