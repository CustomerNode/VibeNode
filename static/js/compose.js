/**
 * compose.js — All Compose feature code extracted from app.js (P3-A).
 *
 * Loaded AFTER app.js (uses globals: viewMode, activeId, allSessions,
 * runningIds, showToast, escHtml, selectSession, addNewAgent) and
 * BEFORE socket.js (socket handlers reference compose functions).
 */

// ═══════════════════════════════════════════════════════════════
// COMPOSE ROOT ORCHESTRATOR — Header, Input Target, State
// ═══════════════════════════════════════════════════════════════

// Current compose state
let _composeProject = null;       // active ComposeProject object
let _composeSections = [];        // section list
let _composeConflicts = [];       // pending conflicts
let composeDetailTaskId = null;   // compose_task_id for session start
let _composeSelectedSection = null; // currently selected section id (null = root)
let _activeComposeProjectId = null; // selected composition ID (null = auto-select most recent)
let _composeProjectsList = [];     // all compositions for the active project
let _composeInitToken = 0;         // concurrency guard for initCompose()
let _composeSelected = new Set();  // multi-select: set of composition IDs
let _composeLastClickedId = null;  // for shift-click range selection
let _composeSearchFilter = '';     // sidebar search filter text
let _composePendingDeletes = [];   // [{ids: [...], timer: timeoutId, toastEl: el}]
let _composeFocusedId = null;      // keyboard-focused composition ID
let _composeActionHistory = [];    // [{label: '...', time: Date.now()}] — last 5 actions
let _composeColumnSorts = {};      // {columnKey: 'manual'|'alpha-asc'|'alpha-desc'|'updated-new'|'updated-old'}
let _composeActiveTagFilter = [];  // active tag filters — sections must match ALL
let _composeBoardSelected = new Set(); // multi-select for board cards (P4-D)
let _composeBoardLastClicked = null;   // for shift-click range on board cards

// ── P4-B: Composition Templates ──
const COMPOSE_TEMPLATES = [
  {
    name: 'Business Proposal',
    icon: '\uD83D\uDCBC',
    sections: [
      { name: 'Executive Summary', artifact_type: 'exec-summary', brief: 'High-level overview of the proposal, key value proposition, and expected outcomes.' },
      { name: 'Problem Statement', artifact_type: 'report', brief: 'Detailed description of the problem or opportunity being addressed.' },
      { name: 'Proposed Solution', artifact_type: 'report', brief: 'The solution approach, methodology, and deliverables.' },
      { name: 'Pricing & Timeline', artifact_type: 'spreadsheet', brief: 'Cost breakdown, payment terms, and project timeline with milestones.' },
      { name: 'Team & Qualifications', artifact_type: 'report', brief: 'Team bios, relevant experience, and case studies.' },
    ]
  },
  {
    name: 'Annual Report',
    icon: '\uD83D\uDCCA',
    sections: [
      { name: 'CEO Letter', artifact_type: 'letter', brief: 'Letter from the CEO summarizing the year, key achievements, and outlook.' },
      { name: 'Financial Summary', artifact_type: 'financial-model', brief: 'Revenue, expenses, profit/loss, and key financial metrics for the year.' },
      { name: 'Market Position', artifact_type: 'report', brief: 'Competitive landscape, market share, and strategic positioning.' },
      { name: 'Product & Innovation', artifact_type: 'report', brief: 'New products, R&D highlights, and technology initiatives.' },
      { name: 'Outlook', artifact_type: 'forecast', brief: 'Forward-looking projections, strategic priorities, and growth targets.' },
    ]
  },
  {
    name: 'Product Launch',
    icon: '\uD83D\uDE80',
    sections: [
      { name: 'Product Overview', artifact_type: 'report', brief: 'Product description, features, specifications, and target audience.' },
      { name: 'Market Analysis', artifact_type: 'report', brief: 'Market size, customer segments, competitive analysis, and positioning.' },
      { name: 'Go-to-Market Strategy', artifact_type: 'plan', brief: 'Launch timeline, marketing channels, messaging, and distribution strategy.' },
      { name: 'Pricing & Revenue Model', artifact_type: 'financial-model', brief: 'Pricing strategy, revenue projections, and unit economics.' },
      { name: 'Launch Budget', artifact_type: 'budget', brief: 'Itemized budget for marketing, development, and operations.' },
    ]
  },
  {
    name: 'Research Paper',
    icon: '\uD83D\uDD2C',
    sections: [
      { name: 'Abstract', artifact_type: 'exec-summary', brief: 'Concise summary of research question, methodology, key findings, and conclusions.' },
      { name: 'Literature Review', artifact_type: 'report', brief: 'Survey of existing research, theoretical framework, and identified gaps.' },
      { name: 'Methodology', artifact_type: 'report', brief: 'Research design, data collection methods, sample, and analytical approach.' },
      { name: 'Results', artifact_type: 'report', brief: 'Data analysis findings, statistical results, tables, and figures.' },
      { name: 'Discussion & Conclusion', artifact_type: 'report', brief: 'Interpretation of results, implications, limitations, and future directions.' },
    ]
  },
  {
    name: 'Pitch Deck',
    icon: '\uD83C\uDFAF',
    sections: [
      { name: 'Problem', artifact_type: 'pitch-deck', brief: 'The problem you are solving and why it matters.' },
      { name: 'Solution', artifact_type: 'pitch-deck', brief: 'Your product/service and how it solves the problem.' },
      { name: 'Market Opportunity', artifact_type: 'pitch-deck', brief: 'Total addressable market, growth trends, and target segments.' },
      { name: 'Business Model', artifact_type: 'pitch-deck', brief: 'Revenue model, pricing, and unit economics.' },
      { name: 'Traction', artifact_type: 'pitch-deck', brief: 'Key metrics, milestones achieved, customer testimonials.' },
      { name: 'Team', artifact_type: 'pitch-deck', brief: 'Founding team, key hires, advisors, and relevant experience.' },
      { name: 'The Ask', artifact_type: 'pitch-deck', brief: 'Funding amount, use of proceeds, and timeline to next milestone.' },
    ]
  },
  {
    name: 'Meeting Notes',
    icon: '\uD83D\uDCDD',
    sections: [
      { name: 'Agenda', artifact_type: 'checklist', brief: 'Meeting agenda items and time allocations.' },
      { name: 'Discussion Notes', artifact_type: 'meeting-notes', brief: 'Key discussion points, decisions made, and rationale.' },
      { name: 'Action Items', artifact_type: 'checklist', brief: 'Assigned tasks with owners, deadlines, and priority.' },
    ]
  },
];

function _composeLoadColumnSorts() {
  try {
    const saved = localStorage.getItem('composeColumnSorts');
    _composeColumnSorts = saved ? JSON.parse(saved) : {};
  } catch(e) { _composeColumnSorts = {}; }
}
function _composeSaveColumnSort(colKey, mode) {
  _composeColumnSorts[colKey] = mode;
  try { localStorage.setItem('composeColumnSorts', JSON.stringify(_composeColumnSorts)); } catch(e) {}
}
function _composeSortSections(sections, colKey) {
  const mode = _composeColumnSorts[colKey] || 'manual';
  if (mode === 'manual') return sections;
  const sorted = [...sections];
  switch (mode) {
    case 'alpha-asc':   sorted.sort((a,b) => (a.name||'').localeCompare(b.name||'')); break;
    case 'alpha-desc':  sorted.sort((a,b) => (b.name||'').localeCompare(a.name||'')); break;
    case 'updated-new': sorted.sort((a,b) => (b.updated_at||'').localeCompare(a.updated_at||'')); break;
    case 'updated-old': sorted.sort((a,b) => (a.updated_at||'').localeCompare(b.updated_at||'')); break;
  }
  return sorted;
}
function _composeToggleSortMenu(colKey, event) {
  event.stopPropagation();
  const existing = document.getElementById('compose-sort-menu');
  if (existing) { existing.remove(); return; }
  const modes = [
    {key:'manual', label:'Manual (drag)'},
    {key:'alpha-asc', label:'A \u2192 Z'},
    {key:'alpha-desc', label:'Z \u2192 A'},
    {key:'updated-new', label:'Newest first'},
    {key:'updated-old', label:'Oldest first'},
  ];
  const current = _composeColumnSorts[colKey] || 'manual';
  let menuHtml = '<div id="compose-sort-menu" class="compose-sort-menu">';
  for (const m of modes) {
    const active = m.key === current ? ' compose-sort-active' : '';
    menuHtml += '<div class="compose-sort-item' + active + '" onclick="event.stopPropagation();_composeApplySort(\'' + colKey + '\',\'' + m.key + '\')">' + m.label + '</div>';
  }
  menuHtml += '</div>';
  const btn = event.currentTarget;
  const rect = btn.getBoundingClientRect();
  const wrapper = document.createElement('div');
  wrapper.innerHTML = menuHtml;
  const menu = wrapper.firstElementChild;
  menu.style.position = 'fixed';
  menu.style.top = (rect.bottom + 4) + 'px';
  menu.style.left = rect.left + 'px';
  document.body.appendChild(menu);
  const close = (e) => { if (!menu.contains(e.target)) { menu.remove(); document.removeEventListener('click', close, true); } };
  setTimeout(() => document.addEventListener('click', close, true), 0);
}
function _composeApplySort(colKey, mode) {
  _composeSaveColumnSort(colKey, mode);
  const m = document.getElementById('compose-sort-menu');
  if (m) m.remove();
  _renderComposeSectionCards();
}

// ── P4-A: Tag color hash (matches kanban's tagColorHash) ──
function _composeTagColor(tag) {
  if (typeof tagColorHash === 'function') return tagColorHash(tag);
  let hash = 0;
  for (let i = 0; i < tag.length; i++) hash = tag.charCodeAt(i) + ((hash << 5) - hash);
  const colors = ['#58a6ff', '#3fb950', '#d29922', '#f85149', '#bc8cff', '#39d2c0', '#e3b341', '#f778ba'];
  return colors[Math.abs(hash) % colors.length];
}

function _composeToggleTagFilter(tag) {
  const idx = _composeActiveTagFilter.indexOf(tag);
  if (idx === -1) { _composeActiveTagFilter.push(tag); } else { _composeActiveTagFilter.splice(idx, 1); }
  _renderComposeSectionCards();
  _renderComposeSidebar();
}

function _composeClearTagFilter() {
  _composeActiveTagFilter = [];
  _renderComposeSectionCards();
  _renderComposeSidebar();
}

function _composeGetAllTags() {
  const tags = new Set();
  for (const sec of _composeSections) {
    if (sec.tags) for (const t of sec.tags) tags.add(t);
  }
  return [...tags].sort();
}

/**
 * Initialize the compose board — fetch project data and render header.
 * Called by setViewMode('compose') in workforce.js.
 */
async function initCompose() {
  _composeLoadColumnSorts();
  const _initToken = ++_composeInitToken;
  try {
    const _proj = localStorage.getItem('activeProject') || '';

    // Restore composition selection from localStorage if not already set
    if (!_activeComposeProjectId && _proj) {
      _activeComposeProjectId = localStorage.getItem('activeComposition:' + _proj) || null;
    }

    // Single fetch — board endpoint returns sibling_projects for the sidebar
    // Always pass &project= so the sidebar stays rooted in the active VibeNode
    // project even when viewing a cross-project pinned composition.
    let query = '';
    if (_activeComposeProjectId) {
      query = '?project_id=' + encodeURIComponent(_activeComposeProjectId);
      if (_proj) query += '&project=' + encodeURIComponent(_proj);
    } else if (_proj) {
      query = '?project=' + encodeURIComponent(_proj);
    }
    const resp = await fetch('/api/compose/board' + query);
    if (_initToken !== _composeInitToken) return;
    const data = await resp.json();

    // Populate sidebar list from sibling_projects (included in board response)
    _composeProjectsList = (data && data.sibling_projects) ? data.sibling_projects : [];

    if (!data || !data.project) {
      // If we had a saved project_id that no longer exists (e.g. deleted),
      // clear it and retry with just the parent project filter so we fall
      // back to the most recent valid composition.  (fix: 2026-04-13)
      if (_activeComposeProjectId) {
        _activeComposeProjectId = null;
        if (_proj) localStorage.removeItem('activeComposition:' + _proj);
        const fallbackQ = _proj ? '?project=' + encodeURIComponent(_proj) : '';
        try {
          const fbResp = await fetch('/api/compose/board' + fallbackQ);
          if (_initToken !== _composeInitToken) return;
          const fbData = await fbResp.json();
          if (fbData && fbData.project) {
            _composeProjectsList = fbData.sibling_projects || [];
            _composeProject = fbData.project;
            _activeComposeProjectId = fbData.project.id;
            _composeSections = fbData.sections || [];
            _composeConflicts = (fbData.conflicts || []).filter(c => c.status === 'pending');
            if (_proj) localStorage.setItem('activeComposition:' + _proj, _activeComposeProjectId);
            _renderComposeBoard();
            attachComposeShortcuts();
            return;
          }
        } catch (_) {}
      }
      _activeComposeProjectId = null;
      _composeSelectedSection = null;
      _composeProject = null;
      _renderComposeEmpty();
      _renderComposeSidebar();
      attachComposeShortcuts();
      return;
    }

    _composeProject = data.project;
    _activeComposeProjectId = data.project.id;
    _composeSections = data.sections || [];
    _composeConflicts = (data.conflicts || []).filter(c => c.status === 'pending');

    // Persist selection
    if (_proj) {
      localStorage.setItem('activeComposition:' + _proj, _activeComposeProjectId);
    }

    // Check if we should restore a section drill-down from URL hash
    if (_restoreComposeSectionFromHash()) {
      // drill-down restored — header stays hidden, keep section's composeDetailTaskId
    } else {
      // Show header and input target for board view
      const header = document.getElementById('compose-root-header');
      const target = document.getElementById('compose-input-target');
      if (header) header.style.display = 'flex';
      if (target) target.style.display = 'flex';
      _updateComposeRootHeader();
      _updateComposeInputTarget();
      _renderComposeSectionCards();
      // Set default compose_task_id to root (only when showing board, not drill-down)
      composeDetailTaskId = 'root:' + _composeProject.id;
    }

  } catch (e) {
    console.error('Failed to init compose:', e);
    _renderComposeEmpty();
  }

  _renderComposeSidebar();
  attachComposeShortcuts();
}

function _renderComposeEmpty() {
  const nameEl = document.getElementById('compose-root-name');
  if (nameEl) nameEl.textContent = 'No composition yet';
  const statusEl = document.getElementById('compose-root-status');
  if (statusEl) statusEl.textContent = '';

  // Hide header bar when no project exists
  const header = document.getElementById('compose-root-header');
  if (header) header.style.display = 'none';
  const target = document.getElementById('compose-input-target');
  if (target) target.style.display = 'none';

  const board = document.getElementById('compose-sections-board');
  if (board) {
    board.innerHTML = `
      <div class="compose-empty-board">
        <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="var(--text-muted)" stroke-width="1.5" stroke-linecap="round">
          <path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/>
        </svg>
        <div style="font-size:16px;font-weight:500;color:var(--text);margin:12px 0 6px;">Welcome to Compose</div>
        <div style="font-size:13px;color:var(--text-muted);margin-bottom:16px;">Orchestrate multiple sections with AI-powered composition.</div>
        <button class="kanban-create-first-btn" onclick="composeCreateProject()">+ Create your first composition</button>
      </div>`;
  }

}

function composeCreateProject() {
  const overlay = document.getElementById('pm-overlay');
  if (!overlay) return;

  let templateHtml = '<div class="compose-template-section"><div class="kanban-create-section-label">Start from template</div><div class="compose-template-grid">';
  for (let i = 0; i < COMPOSE_TEMPLATES.length; i++) {
    const t = COMPOSE_TEMPLATES[i];
    const _esc = typeof escHtml === 'function' ? escHtml : (x => x);
    templateHtml += '<div class="compose-template-card" onclick="_composeSelectTemplate(' + i + ')" data-tidx="' + i + '"><span class="compose-template-icon">' + t.icon + '</span><span class="compose-template-name">' + _esc(t.name) + '</span><span class="compose-template-count">' + t.sections.length + ' sections</span></div>';
  }
  templateHtml += '</div></div>';

  overlay.innerHTML = `<div class="pm-card pm-enter" style="max-width:520px;">
    <h2 class="pm-title">New Composition</h2>
    <div class="pm-body" style="padding:0;">
      <div class="kanban-create-section">
        <div class="kanban-create-section-label">Project name</div>
        <div class="kanban-create-quick-row">
          <input type="text" id="compose-new-project-input" class="kanban-create-input" placeholder="e.g. Blog Series, Product Launch\u2026"
            onkeydown="if(event.key==='Enter'){event.preventDefault();_submitComposeProject();}">
          <button class="kanban-create-submit" onclick="_submitComposeProject()">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
          </button>
        </div>
      </div>
      ${templateHtml}
      <input type="hidden" id="compose-selected-template" value="">
    </div>
  </div>`;
  overlay.classList.add('show');
  requestAnimationFrame(() => {
    overlay.querySelector('.pm-card')?.classList.remove('pm-enter');
    document.getElementById('compose-new-project-input')?.focus();
  });
  overlay.onclick = (e) => { if (e.target === overlay) _closePm(); };
}

function _composeSelectTemplate(idx) {
  // Highlight selected template
  document.querySelectorAll('.compose-template-card').forEach(c => c.classList.remove('active'));
  const card = document.querySelector('.compose-template-card[data-tidx="' + idx + '"]');
  if (card) card.classList.add('active');
  const hidden = document.getElementById('compose-selected-template');
  if (hidden) hidden.value = idx;
  // Auto-fill the name if empty
  const input = document.getElementById('compose-new-project-input');
  if (input && !input.value.trim()) {
    input.value = COMPOSE_TEMPLATES[idx].name;
    input.focus();
  }
}

async function _submitComposeProject() {
  const input = document.getElementById('compose-new-project-input');
  const name = input ? input.value.trim() : '';
  if (!name) { if (input) input.focus(); return; }
  const templateInput = document.getElementById('compose-selected-template');
  const templateIdx = templateInput && templateInput.value !== '' ? parseInt(templateInput.value, 10) : -1;
  _closePm();
  try {
    const resp = await fetch('/api/compose/projects', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name, parent_project: localStorage.getItem('activeProject') || ''}),
    });
    if (!resp.ok) throw new Error('Server error (' + resp.status + ')');
    const data = await resp.json();
    if (data && data.ok) {
      showToast('Created composition: ' + name);
      // Auto-switch to the newly created composition
      if (data.project && data.project.id) {
        _activeComposeProjectId = data.project.id;
        // P4-B: Apply template if selected
        if (templateIdx >= 0 && templateIdx < COMPOSE_TEMPLATES.length) {
          const template = COMPOSE_TEMPLATES[templateIdx];
          try {
            await fetch('/api/compose/projects/' + encodeURIComponent(data.project.id) + '/planner/accept', {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({ sections: template.sections }),
            });
            showToast('Applied template: ' + template.name);
          } catch (te) {
            console.error('Failed to apply template:', te);
          }
        }
      }
      initCompose();
    } else {
      showToast(data.error || 'Failed to create composition', 'error');
    }
  } catch (e) {
    console.error('Failed to create compose project:', e);
    showToast('Failed to create composition', 'error');
  }
}

// P4-B: Apply template to existing project (from empty board)
async function _composeShowTemplates() {
  if (!_composeProject) return;
  const _esc = typeof escHtml === 'function' ? escHtml : (x => x);
  const overlay = document.createElement('div');
  overlay.className = 'pm-overlay';
  overlay.style.cssText = 'display:flex;position:fixed;inset:0;background:rgba(0,0,0,0.6);backdrop-filter:blur(4px);z-index:5000;align-items:center;justify-content:center;';
  overlay.onclick = (e) => { if (e.target === overlay) overlay.remove(); };

  let cardsHtml = '<div class="compose-template-grid" style="padding:0;">';
  for (let i = 0; i < COMPOSE_TEMPLATES.length; i++) {
    const t = COMPOSE_TEMPLATES[i];
    cardsHtml += '<div class="compose-template-card" onclick="this.closest(\'.pm-overlay\').remove();_composeApplyTemplate(' + i + ')">';
    cardsHtml += '<span class="compose-template-icon">' + t.icon + '</span>';
    cardsHtml += '<span class="compose-template-name">' + _esc(t.name) + '</span>';
    cardsHtml += '<span class="compose-template-count">' + t.sections.length + ' sections</span>';
    cardsHtml += '</div>';
  }
  cardsHtml += '</div>';

  overlay.innerHTML = '<div class="pm-card" style="max-width:520px;">' +
    '<div class="pm-title">Choose a Template</div>' +
    '<div class="pm-body">' + cardsHtml + '</div>' +
    '<div class="pm-actions"><button class="pm-btn" onclick="this.closest(\'.pm-overlay\').remove()">Cancel</button></div>' +
    '</div>';
  document.body.appendChild(overlay);
}

async function _composeApplyTemplate(idx) {
  if (!_composeProject || idx < 0 || idx >= COMPOSE_TEMPLATES.length) return;
  const template = COMPOSE_TEMPLATES[idx];
  try {
    const resp = await fetch('/api/compose/projects/' + encodeURIComponent(_composeProject.id) + '/planner/accept', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ sections: template.sections }),
    });
    const data = await resp.json();
    if (data.ok) {
      if (typeof showToast === 'function') showToast('Applied template: ' + template.name);
      initCompose();
    } else {
      if (typeof showToast === 'function') showToast(data.error || 'Failed to apply template', 'error');
    }
  } catch (e) {
    if (typeof showToast === 'function') showToast('Failed to apply template', 'error');
  }
}

let _composeInsertPosition = 'top';
let _composeAddParentId = null;

function composeAddSection(parentId) {
  if (!_composeProject) return;
  const overlay = document.getElementById('pm-overlay');
  if (!overlay) return;
  _composeInsertPosition = 'top';
  _composeArtifactType = 'report';
  _composeAddParentId = parentId || null;

  const _modalTitle = parentId ? 'Add Subsection' : 'Add Section';
  overlay.innerHTML = `<div class="pm-card pm-enter" style="max-width:480px;">
    <h2 class="pm-title" style="display:flex;align-items:center;justify-content:space-between;">
      <span>${_modalTitle}</span>
      <div class="kanban-create-position-row" style="margin:0;">
        <span style="font-size:11px;color:var(--text-dim);">Insert</span>
        <button class="kanban-create-pos-btn active" id="cs-pos-top" onclick="_setComposeInsertPos('top')">Top</button>
        <button class="kanban-create-pos-btn" id="cs-pos-bottom" onclick="_setComposeInsertPos('bottom')">Bottom</button>
      </div>
    </h2>
    <div class="pm-body" style="padding:0;">

      <div class="kanban-create-section">
        <div class="kanban-create-section-label">Quick add</div>
        <div class="kanban-create-quick-row">
          <input type="text" id="compose-new-section-input" class="kanban-create-input" placeholder="Section name\u2026"
            onkeydown="if(event.key==='Enter'){event.preventDefault();_submitComposeSection();}">
          <button class="kanban-create-submit" onclick="_submitComposeSection()">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
          </button>
        </div>
      </div>

      <div class="kanban-create-section">
        <div class="kanban-create-section-label">Type</div>
        <select id="compose-type-picker" class="compose-type-select" onchange="_composeArtifactType=this.value;">
          ${Object.entries(COMPOSE_ARTIFACT_TYPES).map(([cat, types]) =>
            '<optgroup label="' + cat + '">' + types.map(t =>
              '<option value="' + t.key + '"' + (t.key === 'report' ? ' selected' : '') + '>' + t.label + '</option>'
            ).join('') + '</optgroup>'
          ).join('')}
        </select>
      </div>

    </div>
  </div>`;
  overlay.classList.add('show');
  requestAnimationFrame(() => {
    overlay.querySelector('.pm-card')?.classList.remove('pm-enter');
    document.getElementById('compose-new-section-input')?.focus();
  });
  overlay.onclick = (e) => { if (e.target === overlay) _closePm(); };
}

let _composeArtifactType = 'text';

function _setComposeInsertPos(pos) {
  _composeInsertPosition = pos;
  const top = document.getElementById('cs-pos-top');
  const bot = document.getElementById('cs-pos-bottom');
  if (top) top.classList.toggle('active', pos === 'top');
  if (bot) bot.classList.toggle('active', pos === 'bottom');
}

function _setComposeArtifactType(btn, type) {
  _composeArtifactType = type;
  const siblings = btn.parentElement.querySelectorAll('.kanban-create-pos-btn');
  siblings.forEach(b => b.classList.toggle('active', b === btn));
}

async function _submitComposeSection() {
  const input = document.getElementById('compose-new-section-input');
  const name = input ? input.value.trim() : '';
  if (!name) { if (input) input.focus(); return; }
  const insertPos = _composeInsertPosition;
  const artifactType = _composeArtifactType;
  _closePm();

  // Optimistic: insert a ghost card into the drafting column
  const col = document.querySelector('.compose-column[data-status="drafting"] .kanban-column-body');
  let ghostCard = null;
  if (col) {
    ghostCard = document.createElement('div');
    ghostCard.className = 'kanban-card compose-card';
    ghostCard.style.opacity = '0.5';
    ghostCard.innerHTML = '<div class="compose-card-header"><span class="compose-card-title">' + (typeof escHtml === 'function' ? escHtml(name) : name) + '</span></div><div style="font-size:10px;color:var(--text-dim);padding:4px 12px 8px;"><span class="spinner" style="width:10px;height:10px;vertical-align:middle;margin-right:4px;"></span>Creating...</div>';
    if (insertPos === 'top') { col.prepend(ghostCard); } else { col.appendChild(ghostCard); }
    const countEl = col.closest('.compose-column')?.querySelector('.kanban-column-count');
    if (countEl) countEl.textContent = col.querySelectorAll('.kanban-card').length;
  }

  try {
    const resp = await fetch('/api/compose/projects/' + _composeProject.id + '/sections', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name, artifact_type: artifactType, insert_position: insertPos, parent_id: _composeAddParentId || undefined}),
    });
    if (!resp.ok) throw new Error('Server error (' + resp.status + ')');
    const data = await resp.json();
    if (data && data.ok) {
      showToast('Added section: ' + name);
      initCompose();
    } else {
      if (ghostCard) ghostCard.remove();
      showToast(data.error || 'Failed to add section', 'error');
    }
  } catch (e) {
    console.error('Failed to add compose section:', e);
    if (ghostCard) ghostCard.remove();
    showToast('Failed to add section', 'error');
  }
}

// --- Compose sidebar ---

function _renderComposeSidebar() {
  const sidebar = document.getElementById('compose-sidebar');
  if (!sidebar) return;

  const _penIcon = '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/><rect x="12" y="19" width="9" height="2" rx="1"/></svg>';
  const _plusIcon = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>';
  const _refreshIcon = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>';

  // ── Compositions list ──
  let html = '<div class="kanban-sidebar-section">';
  html += '<div class="kanban-sidebar-label">Compositions</div>';

  // Search filter
  if (_composeProjectsList.length > 3) {
    html += '<input type="text" id="compose-sidebar-search" class="compose-sidebar-search" placeholder="Filter\u2026" value="' + (typeof escHtml === 'function' ? escHtml(_composeSearchFilter) : _composeSearchFilter) + '" oninput="_composeFilterSidebar(this.value)">';
  }

  // Bulk action bar (shown when items are selected)
  if (_composeSelected.size > 0) {
    html += '<div class="compose-bulk-bar">';
    html += '<span class="compose-bulk-count">' + _composeSelected.size + ' selected</span>';
    html += '<button class="compose-bulk-btn" onclick="_composeBulkPin()" title="Pin selected">Pin</button>';
    html += '<button class="compose-bulk-btn compose-bulk-danger" onclick="_composeBulkDelete()" title="Delete selected">Delete</button>';
    html += '<button class="compose-bulk-btn" onclick="_composeBulkClear()" title="Clear selection">\u2715</button>';
    html += '</div>';
  }

  if (_composeProjectsList.length > 0) {
    html += '<div id="compose-sidebar-list" class="compose-sidebar-list">';
    const _activeProj = localStorage.getItem('activeProject') || '';
    const filterLower = _composeSearchFilter.toLowerCase();
    for (const cp of _composeProjectsList) {
      // Skip pending deletes and search filter
      if (_composeIsPendingDelete(cp.id)) continue;
      if (filterLower && cp.name.toLowerCase().indexOf(filterLower) === -1) continue;

      const isActive = cp.id === _activeComposeProjectId;
      const isPinned = cp.pinned;
      const isSelected = _composeSelected.has(cp.id);
      const isFocused = cp.id === _composeFocusedId;
      const isCrossProject = isPinned && cp.parent_project && cp.parent_project !== _activeProj;
      const cls = 'kanban-sidebar-btn' + (isActive ? ' compose-sidebar-active' : '') + (isPinned ? ' compose-sidebar-pinned' : '');
      const name = typeof escHtml === 'function' ? escHtml(cp.name) : cp.name;
      const pinDot = isPinned ? '<span class="compose-pin-dot" title="Pinned' + (isCrossProject ? ' (from another project)' : '') + '"></span>' : '';
      const checkbox = '<input type="checkbox" class="compose-select-cb" ' + (isSelected ? 'checked' : '') + ' onclick="event.stopPropagation();_composeToggleSelect(event,\'' + cp.id + '\')" title="Select">';
      const canDrag = !isCrossProject && !_composeSearchFilter;
      html += '<div class="compose-sidebar-item' + (isSelected ? ' compose-sidebar-selected' : '') + (isFocused ? ' compose-sidebar-focused' : '') + '" draggable="' + (canDrag ? 'true' : 'false') + '" data-compose-id="' + cp.id + '"' + (isCrossProject ? ' data-cross-project="1"' : '') + '>';
      html += checkbox;
      html += '<button class="' + cls + '" onclick="switchComposition(\'' + cp.id + '\')" oncontextmenu="event.preventDefault();_composeCtxMenu(event,\'' + cp.id + '\')">' + _penIcon + ' ' + name + pinDot + '</button>';
      // Status indicator
      const st = cp.status;
      if (st) {
        let dotCls = 'compose-status-dot';
        let dotTitle = '';
        if (cp.has_conflicts) {
          dotCls += ' compose-status-conflict';
          dotTitle = 'Has conflicts';
        } else if (st.total_sections === 0) {
          dotCls += ' compose-status-empty';
          dotTitle = 'No sections';
        } else if (st.complete === st.total_sections) {
          dotCls += ' compose-status-done';
          dotTitle = 'All complete';
        } else if (st.in_progress > 0) {
          dotCls += ' compose-status-active';
          dotTitle = st.in_progress + ' in progress';
        } else {
          dotCls += ' compose-status-idle';
          dotTitle = (st.total_sections - st.complete - st.in_progress) + ' idle';
        }
        const fraction = st.total_sections > 0 ? st.complete + '/' + st.total_sections : '';
        html += '<span class="compose-status-badge" title="' + dotTitle + '" data-fraction="' + fraction + '" data-compose-id="' + cp.id + '"><span class="' + dotCls + '"></span>' + fraction + '</span>';
      }
      html += '</div>';
    }
    html += '</div>';
  }

  html += '<button class="kanban-sidebar-btn" onclick="composeCreateProject()">' + _plusIcon + ' New Composition</button>';
  html += '</div>';

  // ── Tag filter bar (P4-A) ──
  if (_composeProject) {
    const allTags = _composeGetAllTags();
    if (allTags.length > 0) {
      html += '<div class="kanban-sidebar-section">';
      html += '<div class="kanban-sidebar-label">Tags</div>';
      html += '<div class="compose-tag-filter-bar">';
      for (const tag of allTags) {
        const tc = _composeTagColor(tag);
        const active = _composeActiveTagFilter.includes(tag) ? ' compose-tag-filter-active' : '';
        html += '<span class="compose-tag-pill compose-tag-filter-pill' + active + '" style="background:' + tc + '22;color:' + tc + ';border-color:' + tc + '44;" onclick="_composeToggleTagFilter(\'' + (typeof escHtml === 'function' ? escHtml(tag) : tag) + '\')">' + (typeof escHtml === 'function' ? escHtml(tag) : tag) + '</span>';
      }
      if (_composeActiveTagFilter.length > 0) {
        html += '<span class="compose-tag-pill compose-tag-filter-pill" style="background:none;color:var(--text-faint);border-color:var(--border);" onclick="_composeClearTagFilter()">clear</span>';
      }
      html += '</div>';
      html += '</div>';
    }
  }

  // ── Actions ──
  if (_composeProject) {
    html += '<div class="kanban-sidebar-section">';
    html += '<div class="kanban-sidebar-label">Actions</div>';
    html += '<button class="kanban-sidebar-btn" onclick="composeAddSection()">' + _plusIcon + ' New Section</button>';
    html += '<button class="kanban-sidebar-btn" onclick="initCompose()">' + _refreshIcon + ' Refresh</button>';
    html += '</div>';
  }

  // ── Action history ──
  if (_composeActionHistory.length > 0) {
    html += '<div class="kanban-sidebar-section" id="compose-action-history">';
    html += '<div class="kanban-sidebar-label">Recent</div>';
    for (const entry of _composeActionHistory) {
      const ago = _composeTimeAgo(entry.time);
      const label = typeof escHtml === 'function' ? escHtml(entry.label) : entry.label;
      html += '<div class="compose-history-item"><span class="compose-history-label">' + label + '</span><span class="compose-history-ago">' + ago + '</span></div>';
    }
    html += '</div>';
  }

  // Detect fraction changes before replacing DOM
  const _oldBadges = {};
  sidebar.querySelectorAll('.compose-status-badge[data-compose-id]').forEach(el => {
    _oldBadges[el.dataset.composeId] = el.dataset.fraction;
  });

  sidebar.innerHTML = html;

  // Pulse badges whose fractions changed
  sidebar.querySelectorAll('.compose-status-badge[data-compose-id]').forEach(el => {
    const prev = _oldBadges[el.dataset.composeId];
    if (prev !== undefined && prev !== el.dataset.fraction) {
      el.classList.add('compose-status-changed');
      setTimeout(() => el.classList.remove('compose-status-changed'), 600);
    }
  });

  // Attach drag-and-drop to composition list
  _attachComposeDragDrop();

  // Permission aggregator
  const permPanel = document.getElementById('sidebar-perm-panel');
  if (permPanel && typeof _buildPermissionPanel === 'function') {
    permPanel.innerHTML = _buildPermissionPanel();
    permPanel.style.display = '';
  }
}

// --- Drag-and-drop reorder for sidebar compositions ---

function _attachComposeDragDrop() {
  const list = document.getElementById('compose-sidebar-list');
  if (!list) return;

  let _dragItem = null;

  list.addEventListener('dragstart', (e) => {
    // Disable drag-reorder while search filter is active — the filtered DOM
    // only contains a subset of items, so reorder would send an incomplete list.
    if (_composeSearchFilter) { e.preventDefault(); return; }
    _dragItem = e.target.closest('.compose-sidebar-item');
    if (!_dragItem) return;
    _dragItem.classList.add('compose-dragging');
    e.dataTransfer.effectAllowed = 'move';
  });

  list.addEventListener('dragend', () => {
    if (_dragItem) _dragItem.classList.remove('compose-dragging');
    _dragItem = null;
    // Remove any lingering drag-over indicators
    list.querySelectorAll('.compose-drag-over').forEach(el => el.classList.remove('compose-drag-over'));
  });

  list.addEventListener('dragover', (e) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    const target = e.target.closest('.compose-sidebar-item');
    if (!target || target === _dragItem || target.dataset.crossProject) return;
    // Add visual indicator
    list.querySelectorAll('.compose-drag-over').forEach(el => el.classList.remove('compose-drag-over'));
    target.classList.add('compose-drag-over');
  });

  list.addEventListener('dragleave', (e) => {
    const target = e.target.closest('.compose-sidebar-item');
    if (target) target.classList.remove('compose-drag-over');
  });

  list.addEventListener('drop', (e) => {
    e.preventDefault();
    const target = e.target.closest('.compose-sidebar-item');
    if (!target || !_dragItem || target === _dragItem || target.dataset.crossProject) return;
    target.classList.remove('compose-drag-over');

    // Reorder DOM
    const items = [...list.querySelectorAll('.compose-sidebar-item')];
    const fromIdx = items.indexOf(_dragItem);
    const toIdx = items.indexOf(target);
    if (fromIdx < toIdx) {
      target.after(_dragItem);
    } else {
      target.before(_dragItem);
    }

    // Collect new order and send to backend (exclude cross-project pinned items)
    const newOrder = [...list.querySelectorAll('.compose-sidebar-item')]
      .filter(el => !el.dataset.crossProject)
      .map(el => el.dataset.composeId);
    // Update local list to match new order, preserving cross-project pinned items at the end
    const idMap = {};
    _composeProjectsList.forEach(p => { idMap[p.id] = p; });
    const reordered = newOrder.map(id => idMap[id]).filter(Boolean);
    const crossProjectItems = _composeProjectsList.filter(p => {
      const el = list.querySelector('[data-compose-id="' + p.id + '"]');
      return el && el.dataset.crossProject;
    });
    _composeProjectsList = reordered.concat(crossProjectItems);

    fetch('/api/compose/projects/reorder', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({order: newOrder}),
    }).catch(err => console.error('Failed to save composition order:', err));
  });
}

// --- Switch composition ---

function switchComposition(projectId) {
  if (projectId === _activeComposeProjectId) return;
  _activeComposeProjectId = projectId;
  _composeFocusedId = projectId;
  _composeSelectedSection = null;
  // Persist selection
  const _proj = localStorage.getItem('activeProject') || '';
  if (_proj) localStorage.setItem('activeComposition:' + _proj, projectId);
  // Clear drill-down hash if present
  const url = new URL(window.location);
  if (url.hash.startsWith('#compose/')) {
    url.hash = '#compose';
    history.replaceState({ view: 'compose' }, '', url.pathname + url.search + '#compose');
  }
  initCompose();
}

// --- Compose context menu (right-click on composition) ---

function _composeCtxMenuClose() {
  const el = document.getElementById('compose-ctx-menu');
  if (el) {
    if (el._closeHandler) document.removeEventListener('click', el._closeHandler, true);
    el.remove();
  }
}

function _composeCtxMenu(event, projectId) {
  // Remove any existing context menu (and its listener)
  _composeCtxMenuClose();

  const menu = document.createElement('div');
  menu.id = 'compose-ctx-menu';
  menu.className = 'compose-ctx-menu';
  menu.style.left = event.clientX + 'px';
  menu.style.top = event.clientY + 'px';

  const renameIcon = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>';
  const dupeIcon = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
  const pinIcon = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 2v8l4 4H8l4-4z"/><line x1="12" y1="22" x2="12" y2="14"/></svg>';
  const deleteIcon = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>';

  // Check if composition is pinned
  const cp = _composeProjectsList.find(p => p.id === projectId);
  const isPinned = cp && cp.pinned;
  const pinLabel = isPinned ? 'Unpin' : 'Pin';

  menu.innerHTML =
    '<div class="compose-ctx-item" onclick="_composeRename(\'' + projectId + '\')">' + renameIcon + ' Rename</div>' +
    '<div class="compose-ctx-item" onclick="_composeDuplicate(\'' + projectId + '\')">' + dupeIcon + ' Duplicate</div>' +
    '<div class="compose-ctx-item" onclick="_composeTogglePin(\'' + projectId + '\')">' + pinIcon + ' ' + pinLabel + '</div>' +
    '<div class="compose-ctx-item compose-ctx-danger" onclick="_composeDelete(\'' + projectId + '\')">' + deleteIcon + ' Delete</div>';

  document.body.appendChild(menu);

  // Clamp position so menu doesn't overflow viewport
  requestAnimationFrame(() => {
    const rect = menu.getBoundingClientRect();
    if (rect.right > window.innerWidth) menu.style.left = Math.max(0, window.innerWidth - rect.width - 4) + 'px';
    if (rect.bottom > window.innerHeight) menu.style.top = Math.max(0, window.innerHeight - rect.height - 4) + 'px';
  });

  // Close on click anywhere else
  const _close = (e) => {
    if (!menu.contains(e.target)) {
      menu.remove();
      document.removeEventListener('click', _close, true);
    }
  };
  menu._closeHandler = _close;
  setTimeout(() => document.addEventListener('click', _close, true), 0);
}

async function _composeRename(projectId) {
  _composeCtxMenuClose();

  const cp = _composeProjectsList.find(p => p.id === projectId);
  const oldName = cp ? cp.name : '';
  const newName = prompt('Rename composition:', oldName);
  if (!newName || !newName.trim() || newName.trim() === oldName) return;

  try {
    const resp = await fetch('/api/compose/projects/' + projectId, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: newName.trim()}),
    });
    if (!resp.ok) throw new Error('Rename failed');
    const data = await resp.json();
    if (data && data.ok) {
      showToast('Renamed to "' + newName.trim() + '"');
      _composeLogAction('Renamed to "' + newName.trim() + '"');
      initCompose();
    } else {
      showToast(data.error || 'Rename failed', 'error');
    }
  } catch (e) {
    console.error('Failed to rename composition:', e);
    showToast('Failed to rename composition', 'error');
  }
}

function _composeIsPendingDelete(id) {
  return _composePendingDeletes.some(pd => pd.ids.includes(id));
}

function _composeFlushPendingDeletes() {
  for (const pd of _composePendingDeletes) {
    clearTimeout(pd.timer);
    if (pd.toastEl && pd.toastEl.parentNode) pd.toastEl.remove();
    _composeExecuteDeletes(pd.ids);
  }
  _composePendingDeletes = [];
}

async function _composeExecuteDeletes(ids) {
  for (const pid of ids) {
    try {
      await fetch('/api/compose/projects/' + pid, {method: 'DELETE'});
    } catch (e) {
      console.error('Failed to delete composition ' + pid, e);
    }
  }
}

function _composeRestackToasts() {
  _composePendingDeletes.forEach((pd, i) => {
    if (pd.toastEl) pd.toastEl.style.bottom = (24 + i * 52) + 'px';
  });
}

function _composeScheduleDelete(ids, label) {
  // If active composition is being deleted, remember it for undo and clear selection
  const wasActive = ids.includes(_activeComposeProjectId) ? _activeComposeProjectId : null;
  if (wasActive) {
    _activeComposeProjectId = null;
    _composeSelectedSection = null;
    const _proj = localStorage.getItem('activeProject') || '';
    if (_proj) localStorage.removeItem('activeComposition:' + _proj);
  }

  // Build undo toast — offset vertically when multiple toasts are active
  const toast = document.createElement('div');
  toast.className = 'compose-undo-toast';
  toast.style.bottom = (24 + _composePendingDeletes.length * 52) + 'px';
  toast.innerHTML = '<span>' + (typeof escHtml === 'function' ? escHtml(label) : label) + '</span>' +
    '<button class="compose-undo-btn">Undo</button>';
  document.body.appendChild(toast);
  requestAnimationFrame(() => toast.classList.add('show'));

  const pd = {ids: ids, timer: null, toastEl: toast};

  // Undo button
  toast.querySelector('.compose-undo-btn').onclick = () => {
    clearTimeout(pd.timer);
    toast.remove();
    _composePendingDeletes = _composePendingDeletes.filter(x => x !== pd);
    _composeRestackToasts();
    // Restore previously-active composition if it was the one deleted
    if (wasActive) {
      _activeComposeProjectId = wasActive;
      const _projKey = localStorage.getItem('activeProject') || '';
      if (_projKey) localStorage.setItem('activeComposition:' + _projKey, wasActive);
    }
    initCompose();
  };

  // Timer: execute deletes after 5 seconds
  pd.timer = setTimeout(() => {
    toast.remove();
    _composePendingDeletes = _composePendingDeletes.filter(x => x !== pd);
    _composeRestackToasts();
    _composeExecuteDeletes(ids);
    _composeLogAction(label);
    // Refresh after last pending delete completes
    if (_composePendingDeletes.length === 0) {
      initCompose();
    }
  }, 5000);

  _composePendingDeletes.push(pd);

  // Hide items immediately from sidebar
  _renderComposeSidebar();
  // If active was deleted, load next composition
  if (!_activeComposeProjectId) {
    initCompose();
  }
}

function _composeDelete(projectId) {
  _composeCtxMenuClose();

  const cp = _composeProjectsList.find(p => p.id === projectId);
  const name = cp ? cp.name : 'this composition';

  _composeScheduleDelete([projectId], 'Deleted "' + name + '"');
}

async function _composeDuplicate(projectId) {
  _composeCtxMenuClose();

  const cp = _composeProjectsList.find(p => p.id === projectId);
  const defaultName = cp ? 'Copy of ' + cp.name : 'Copy';
  const name = prompt('Name for the duplicate:', defaultName);
  if (!name || !name.trim()) return;

  try {
    const resp = await fetch('/api/compose/projects/' + projectId + '/clone', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: name.trim()}),
    });
    if (!resp.ok) throw new Error('Clone failed');
    const data = await resp.json();
    if (data && data.ok) {
      showToast('Duplicated as "' + name.trim() + '"');
      _composeLogAction('Duplicated as "' + name.trim() + '"');
      // Switch to the clone
      if (data.project && data.project.id) {
        _activeComposeProjectId = data.project.id;
      }
      initCompose();
    } else {
      showToast(data.error || 'Duplicate failed', 'error');
    }
  } catch (e) {
    console.error('Failed to duplicate composition:', e);
    showToast('Failed to duplicate composition', 'error');
  }
}

async function _composeTogglePin(projectId) {
  _composeCtxMenuClose();

  const cp = _composeProjectsList.find(p => p.id === projectId);
  const newPinned = !(cp && cp.pinned);

  try {
    const resp = await fetch('/api/compose/projects/' + projectId, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({pinned: newPinned}),
    });
    if (!resp.ok) throw new Error('Pin toggle failed');
    const data = await resp.json();
    if (data && data.ok) {
      showToast(newPinned ? 'Pinned' : 'Unpinned');
      _composeLogAction((newPinned ? 'Pinned' : 'Unpinned') + ' "' + (cp ? cp.name : '') + '"');
      initCompose();
    } else {
      showToast(data.error || 'Failed', 'error');
    }
  } catch (e) {
    console.error('Failed to toggle pin:', e);
    showToast('Failed to update pin state', 'error');
  }
}

// --- Compose bulk selection ---

function _composeToggleSelect(event, projectId) {
  if (event.shiftKey && _composeLastClickedId) {
    // Shift-click: select range
    const ids = _composeProjectsList.map(p => p.id);
    const from = ids.indexOf(_composeLastClickedId);
    const to = ids.indexOf(projectId);
    if (from !== -1 && to !== -1) {
      const start = Math.min(from, to);
      const end = Math.max(from, to);
      for (let i = start; i <= end; i++) {
        _composeSelected.add(ids[i]);
      }
    }
  } else {
    // Single click: toggle
    if (_composeSelected.has(projectId)) {
      _composeSelected.delete(projectId);
    } else {
      _composeSelected.add(projectId);
    }
  }
  _composeLastClickedId = projectId;
  _renderComposeSidebar();
}

function _composeBulkClear() {
  _composeSelected = new Set();
  _composeLastClickedId = null;
  _renderComposeSidebar();
}

function _composeBulkDelete() {
  const count = _composeSelected.size;
  if (!count) return;

  const ids = [..._composeSelected];
  _composeSelected = new Set();
  const label = 'Deleted ' + count + ' composition' + (count > 1 ? 's' : '');
  _composeScheduleDelete(ids, label);
}

async function _composeBulkPin() {
  const ids = [..._composeSelected];
  if (!ids.length) return;

  // Determine action: if all selected are pinned, unpin. Otherwise pin.
  const allPinned = ids.every(id => {
    const cp = _composeProjectsList.find(p => p.id === id);
    return cp && cp.pinned;
  });
  const newPinned = !allPinned;

  let updated = 0;
  for (const pid of ids) {
    try {
      const resp = await fetch('/api/compose/projects/' + pid, {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({pinned: newPinned}),
      });
      if (resp.ok) updated++;
    } catch (e) {
      console.error('Failed to update pin for ' + pid, e);
    }
  }
  const _pinLabel = (newPinned ? 'Pinned ' : 'Unpinned ') + updated + ' composition' + (updated > 1 ? 's' : '');
  showToast(_pinLabel);
  _composeLogAction(_pinLabel);
  _composeSelected = new Set();
  initCompose();
}

function _composeFilterSidebar(value) {
  _composeSearchFilter = value || '';
  _renderComposeSidebar();
  // Restore focus to the search input after re-render
  const input = document.getElementById('compose-sidebar-search');
  if (input) {
    input.focus();
    input.selectionStart = input.selectionEnd = input.value.length;
  }
}

// --- Compose action history ---

function _composeLogAction(label) {
  _composeActionHistory.unshift({label: label, time: Date.now()});
  if (_composeActionHistory.length > 5) _composeActionHistory.length = 5;
  _renderComposeActionHistory();
}

function _renderComposeActionHistory() {
  const el = document.getElementById('compose-action-history');
  if (!el) {
    // Section doesn't exist yet (history was empty when sidebar last rendered).
    // Re-render the full sidebar so the section gets created.
    if (_composeActionHistory.length > 0) _renderComposeSidebar();
    return;
  }
  if (_composeActionHistory.length === 0) {
    el.innerHTML = '';
    return;
  }
  let html = '<div class="kanban-sidebar-label">Recent</div>';
  for (const entry of _composeActionHistory) {
    const ago = _composeTimeAgo(entry.time);
    const label = typeof escHtml === 'function' ? escHtml(entry.label) : entry.label;
    html += '<div class="compose-history-item"><span class="compose-history-label">' + label + '</span><span class="compose-history-ago">' + ago + '</span></div>';
  }
  el.innerHTML = html;
}

function _composeTimeAgo(ts) {
  const diff = Math.floor((Date.now() - ts) / 1000);
  if (diff < 0) return 'just now';  // future timestamp (clock skew)
  if (diff < 5) return 'just now';
  if (diff < 60) return diff + 's ago';
  if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
  if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
  return Math.floor(diff / 86400) + 'd ago';
}

// --- Compose keyboard shortcuts ---

let _composeShortcutsAttached = false;

function _composeVisibleIds() {
  const filterLower = _composeSearchFilter.toLowerCase();
  return _composeProjectsList
    .filter(cp => !_composeIsPendingDelete(cp.id))
    .filter(cp => !filterLower || cp.name.toLowerCase().indexOf(filterLower) !== -1)
    .map(cp => cp.id);
}

function attachComposeShortcuts() {
  if (_composeShortcutsAttached) return;
  _composeShortcutsAttached = true;

  document.addEventListener('keydown', (e) => {
    if (typeof viewMode !== 'undefined' && viewMode !== 'compose') return;
    const tag = (e.target.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select' || e.target.isContentEditable) return;
    if (e.ctrlKey || e.metaKey || e.key === 'F5') return;

    // If shortcut overlay is open, only allow Escape and ? (to close it)
    const _helpOverlay = document.querySelector('.kanban-shortcut-overlay');
    if (_helpOverlay && e.key !== 'Escape' && e.key !== '?') return;

    switch (e.key) {
      case 'n': e.preventDefault(); if (_composeProject) composeAddSection(); else composeCreateProject(); break;
      case 'r': e.preventDefault(); initCompose(); if (typeof showToast === 'function') showToast('Refreshed'); break;
      case 'Escape':
        if (_helpOverlay) { e.preventDefault(); _helpOverlay.remove(); }
        else if (_composeSelected.size > 0) { e.preventDefault(); _composeBulkClear(); }
        else if (_composeFocusedId) { e.preventDefault(); _composeFocusedId = null; _renderComposeSidebar(); }
        else if (_composeSelectedSection) { e.preventDefault(); navigateToComposeBoard(); }
        break;
      case 'ArrowUp':
      case 'ArrowDown': {
        e.preventDefault();
        const visIds = _composeVisibleIds();
        if (!visIds.length) break;
        const curIdx = _composeFocusedId ? visIds.indexOf(_composeFocusedId) : -1;
        let nextIdx;
        if (e.key === 'ArrowDown') {
          nextIdx = curIdx < visIds.length - 1 ? curIdx + 1 : curIdx;
        } else {
          nextIdx = curIdx > 0 ? curIdx - 1 : 0;
        }
        _composeFocusedId = visIds[nextIdx];
        if (e.shiftKey) _composeSelected.add(_composeFocusedId);
        _renderComposeSidebar();
        break;
      }
      case 'Enter':
        if (_composeFocusedId) { e.preventDefault(); switchComposition(_composeFocusedId); }
        break;
      case ' ':
        if (_composeFocusedId) {
          e.preventDefault();
          _composeToggleSelect(e, _composeFocusedId);
        }
        break;
      case 'Delete':
        if (_composeFocusedId) {
          e.preventDefault();
          const _delCp = _composeProjectsList.find(p => p.id === _composeFocusedId);
          const _delName = _delCp ? _delCp.name : 'this composition';
          // Move focus to next visible item (or previous if at end)
          const _delVis = _composeVisibleIds();
          const _delIdx = _delVis.indexOf(_composeFocusedId);
          const _delId = _composeFocusedId;
          if (_delVis.length > 1) {
            _composeFocusedId = _delVis[_delIdx < _delVis.length - 1 ? _delIdx + 1 : _delIdx - 1];
          } else {
            _composeFocusedId = null;
          }
          _composeScheduleDelete([_delId], 'Deleted "' + _delName + '"');
        }
        break;
      case '?':
        e.preventDefault();
        _showComposeShortcutHelp();
        break;
    }
  });
}

function _showComposeShortcutHelp() {
  const existing = document.querySelector('.kanban-shortcut-overlay');
  if (existing) { existing.remove(); return; }
  const overlay = document.createElement('div');
  overlay.className = 'kanban-shortcut-overlay';
  overlay.innerHTML = `<div class="kanban-shortcut-card"><h3>Compose Keyboard Shortcuts</h3>
    <div class="kanban-shortcut-grid">
      <kbd>\u2191 \u2193</kbd><span>Move focus</span>
      <kbd>Enter</kbd><span>Open composition</span>
      <kbd>Space</kbd><span>Toggle selection</span>
      <kbd>Shift+\u2191\u2193</kbd><span>Extend selection</span>
      <kbd>Delete</kbd><span>Delete focused</span>
      <kbd>Esc</kbd><span>Clear selection / close</span>
      <kbd>n</kbd><span>New section</span>
      <kbd>r</kbd><span>Refresh</span>
      <kbd>?</kbd><span>Toggle this help</span>
    </div>
    <button class="kanban-shortcut-close" onclick="this.closest('.kanban-shortcut-overlay').remove()">Close</button>
  </div>`;
  overlay.onclick = (e) => { if (e.target === overlay) overlay.remove(); };
  document.body.appendChild(overlay);
}

function _updateComposeRootHeader() {
  if (!_composeProject) return;

  const nameEl = document.getElementById('compose-root-name');
  if (nameEl) {
    nameEl.textContent = _composeProject.name;
    nameEl.onclick = () => {
      // Open root session
      if (_composeProject.root_session_id) {
        selectSession(_composeProject.root_session_id);
      }
    };
  }

  const statusEl = document.getElementById('compose-root-status');
  if (statusEl) {
    const total = _composeSections.length;
    const complete = _composeSections.filter(s => s.status === 'complete').length;
    const drafting = _composeSections.filter(s => s.status === 'drafting').length;
    const reviewing = _composeSections.filter(s => s.status === 'reviewing').length;
    let parts = [total + ' section' + (total !== 1 ? 's' : '')];
    if (complete > 0) parts.push(complete + ' complete');
    if (reviewing > 0) parts.push(reviewing + ' reviewing');
    if (drafting > 0) parts.push(drafting + ' drafting');
    statusEl.textContent = parts.join(', ');
  }

  const conflictsEl = document.getElementById('compose-root-conflicts');
  const countEl = document.getElementById('compose-root-conflict-count');
  if (conflictsEl && countEl) {
    if (_composeConflicts.length > 0) {
      conflictsEl.style.display = 'inline-flex';
      countEl.textContent = _composeConflicts.length;
    } else {
      conflictsEl.style.display = 'none';
    }
  }

  _composeUpdateSharedBadge();
  _composeUpdateLaunchBtn();
}

// ═══════════════════════════════════════════════════════════════
// COMPOSE LAUNCH ALL — start sessions for all unlinked sections
// ═══════════════════════════════════════════════════════════════

let _composeLaunching = false;

function _composeUpdateLaunchBtn() {
  const btn = document.getElementById('compose-launch-all-btn');
  if (!btn) return;
  const unlinked = _composeSections.filter(s => !s.session_id);
  btn.style.display = (unlinked.length > 0 && !_composeLaunching) ? 'inline-flex' : 'none';
}

async function _composeLaunchAll() {
  if (!_composeProject || _composeLaunching) return;
  const unlinked = _composeSections.filter(s => !s.session_id);
  if (unlinked.length === 0) {
    if (typeof showToast === 'function') showToast('All sections already have sessions');
    return;
  }

  _composeLaunching = true;
  _composeUpdateLaunchBtn();
  const total = unlinked.length;
  if (typeof showToast === 'function') showToast('Launching ' + total + ' section agent' + (total !== 1 ? 's' : '') + '...');

  let succeeded = 0;
  let failed = 0;
  const projId = _composeProject.id;

  for (const sec of unlinked) {
    try {
      const resp = await fetch('/api/compose/projects/' + encodeURIComponent(projId) + '/sections/' + sec.id + '/launch', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
      });
      const data = await resp.json();
      if (data.ok) {
        sec.session_id = data.session_id;
        succeeded++;
      } else {
        failed++;
      }
    } catch (e) {
      failed++;
    }
  }

  _composeLaunching = false;
  _composeUpdateLaunchBtn();
  _renderComposeSectionCards();
  _updateComposeRootHeader();

  let msg = succeeded + ' agent' + (succeeded !== 1 ? 's' : '') + ' launched';
  if (failed > 0) msg += ', ' + failed + ' failed';
  if (typeof showToast === 'function') showToast(msg, failed > 0);
}

// ═══════════════════════════════════════════════════════════════
// COMPOSE EXPORT — download composition content
// ═══════════════════════════════════════════════════════════════

function _composeExportMenu(event) {
  event.stopPropagation();
  const existing = document.getElementById('compose-export-menu');
  if (existing) { existing.remove(); return; }
  const btn = event.currentTarget;
  const rect = btn.getBoundingClientRect();

  const menu = document.createElement('div');
  menu.id = 'compose-export-menu';
  menu.className = 'compose-sort-menu';
  menu.style.position = 'fixed';
  menu.style.top = (rect.bottom + 4) + 'px';
  menu.style.left = rect.left + 'px';
  menu.innerHTML =
    '<div class="compose-sort-item" onclick="_composeExport(\'markdown\')"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="vertical-align:middle;margin-right:6px;"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>Markdown bundle</div>' +
    '<div class="compose-sort-item" onclick="_composeExport(\'zip\')"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" style="vertical-align:middle;margin-right:6px;"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>Zip archive</div>';
  document.body.appendChild(menu);

  const close = (e) => { if (!menu.contains(e.target)) { menu.remove(); document.removeEventListener('click', close, true); } };
  setTimeout(() => document.addEventListener('click', close, true), 0);
}

async function _composeExport(format) {
  const m = document.getElementById('compose-export-menu');
  if (m) m.remove();
  if (!_composeProject) return;

  try {
    const resp = await fetch('/api/compose/projects/' + encodeURIComponent(_composeProject.id) + '/export', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({format: format}),
    });

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      if (typeof showToast === 'function') showToast(err.error || 'Export failed', 'error');
      return;
    }

    // Download the response as a file
    const blob = await resp.blob();
    const disposition = resp.headers.get('Content-Disposition') || '';
    const match = disposition.match(/filename="?([^"]+)"?/);
    const filename = match ? match[1] : ('export.' + (format === 'zip' ? 'zip' : 'md'));

    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);

    if (typeof showToast === 'function') showToast('Export downloaded');
  } catch (e) {
    console.error('Export failed:', e);
    if (typeof showToast === 'function') showToast('Export failed', 'error');
  }
}

// ═══════════════════════════════════════════════════════════════
// COMPOSE AI PLANNER — slideout panel for content planning
// ═══════════════════════════════════════════════════════════════

const _COMPOSE_PLANNER_SYSTEM = [
  'You are a content planner for a composition project.',
  'You MUST respond with ONLY valid JSON. No explanation, no prose, no apologies, no refusals.',
  'Even for trivial or vague inputs, you MUST produce a valid section structure.',
  'NEVER refuse. NEVER explain. NEVER ask questions.',
  'NEVER output anything except the JSON object.',
  'Format: {"sections":[{"name":"...","artifact_type":"text","brief":"A 1-3 sentence description of this section\'s content and purpose.","subsections":[]}]}',
  'artifact_type must be one of: report, proposal, memo, whitepaper, letter, blog, sop, contract, script, copy, press, newsletter, faq, meeting-notes, case-study, resume, legal-brief, exec-summary, spreadsheet, financial-model, budget, forecast, survey, comparison, scorecard, inventory, dataset, pitch-deck, board-deck, training, sales-deck, webinar, flowchart, org-chart, architecture, timeline, mind-map, wireframe, infographic, process-map, er-diagram, code, config, html, plan, checklist, questionnaire, rubric, email, social, talking-points. Choose the most specific type that matches the content.',
  'Each section MUST include a "brief" field: 1-3 sentences describing what this section should contain.',
  'Break the content into logical sections for parallel AI agents.',
  'Each section should be independently writeable by an agent.',
  'Use subsections for deeper breakdown. 1-3 nesting levels typical.',
  'Names should be descriptive content section names, not generic labels.',
].join(' ');

let _composePlannerSessionId = null;
let _composePlannerProposal = null;
let _composePlannerAccumText = '';
let _composePlannerEntryListener = null;
let _composePlannerStateListener = null;
let _composePlannerStartTime = 0;
let _composePlannerTimerInterval = null;
let _composePlannerScopeParentId = null;

function _openComposePlanner(parentId) {
  const old = document.getElementById('compose-planner-panel');
  if (old) old.remove();
  _composePlannerProposal = null;
  _composePlannerScopeParentId = parentId || null;

  // Prompt modal
  const overlay = document.createElement('div');
  overlay.className = 'pm-overlay';
  overlay.onclick = (e) => { if (e.target === overlay) overlay.remove(); };
  overlay.innerHTML = `
    <div class="pm-card" style="max-width:480px;">
      <div class="pm-title">Plan with AI</div>
      <div class="pm-body">
        <textarea id="compose-planner-prompt" class="kanban-create-textarea" rows="3"
          placeholder="Describe what you want to compose\u2026 e.g. 'A quarterly business review with financials, product updates, and team highlights'"
          onkeydown="if(_shouldSend&&_shouldSend(event)){event.preventDefault();_submitComposePlanPrompt();}"></textarea>
      </div>
      <div class="pm-actions">
        <button class="pm-btn" onclick="this.closest('.pm-overlay').remove()">Cancel</button>
        <button class="pm-btn pm-btn-primary" onclick="_submitComposePlanPrompt()">Plan</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  setTimeout(() => { const ta = document.getElementById('compose-planner-prompt'); if (ta) ta.focus(); }, 100);
}

async function _submitComposePlanPrompt() {
  const ta = document.getElementById('compose-planner-prompt');
  const prompt = ta ? ta.value.trim() : '';
  if (!prompt) return;
  const overlay = ta.closest('.pm-overlay');
  if (overlay) overlay.remove();

  _openComposePlannerSlideout(prompt);
}

function _buildComposePlannerPanel() {
  const panel = document.createElement('div');
  panel.id = 'compose-planner-panel';
  panel.className = 'kanban-planner-panel';
  panel.innerHTML = `
    <div class="kanban-planner-header">
      <span class="kanban-planner-title"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="16"/><line x1="8" y1="12" x2="16" y2="12"/></svg> Plan Composition</span>
      <div style="display:flex;gap:4px;align-items:center;">
        <button class="kanban-planner-close" onclick="_closeComposePlanner()" title="Close">&times;</button>
      </div>
    </div>
    <div class="planner-body" id="compose-planner-body">
      <div class="planner-status">
        <div class="planner-spinner"></div><span>Building content plan\u2026</span>
      </div>
    </div>
    <div class="planner-footer">
      <div class="planner-refine-row">
        <textarea id="compose-planner-refine" class="kanban-create-textarea" rows="2" placeholder="Ask for changes\u2026"
          onkeydown="if(_shouldSend&&_shouldSend(event)){event.preventDefault();_refineComposePlan();}"></textarea>
      </div>
    </div>`;
  return panel;
}

async function _openComposePlannerSlideout(prompt) {
  const old = document.getElementById('compose-planner-panel');
  if (old) old.remove();

  const newId = crypto.randomUUID();
  _composePlannerSessionId = newId;
  if (typeof _hiddenSessionIds !== 'undefined') _hiddenSessionIds.add(newId);

  const panel = _buildComposePlannerPanel();
  document.body.appendChild(panel);
  requestAnimationFrame(() => panel.classList.add('open'));

  _attachComposePlannerListeners();

  // Build context snippet
  let contextSnippet = '';
  if (_composeProject && _composeSections.length > 0) {
    const lines = _composeSections.map(s => '- [' + s.status + '] ' + s.name + (s.artifact_type ? ' (' + s.artifact_type + ')' : ''));
    contextSnippet = '\n\nEXISTING SECTIONS:\n' + lines.join('\n') + '\n\nConsider these existing sections. Avoid duplicating them. You may plan additional sections or reorganize.';
  }

  let scopeSnippet = '';
  if (_composePlannerScopeParentId) {
    const parent = _composeSections.find(s => s.id === _composePlannerScopeParentId);
    if (parent) {
      scopeSnippet = '\n\nSCOPED PLANNING: You are planning subsections for "' + parent.name + '". Return sections that will be children of this parent.';
    }
  }

  const sysPrompt = _COMPOSE_PLANNER_SYSTEM + contextSnippet + scopeSnippet;

  _composePlannerAccumText = '';
  _composePlannerStartTime = Date.now();
  if (typeof runningIds !== 'undefined') runningIds.add(newId);
  if (typeof sessionKinds !== 'undefined') sessionKinds[newId] = 'working';

  socket.emit('start_session', {
    session_id: newId,
    prompt: prompt,
    cwd: typeof _currentProjectDir === 'function' ? _currentProjectDir() : '',
    system_prompt: sysPrompt,
    max_turns: 0,
    session_type: 'planner',
  });
}

function _attachComposePlannerListeners() {
  _detachComposePlannerListeners();
  _composePlannerAccumText = '';
  _composePlannerStartTime = Date.now();

  _composePlannerTimerInterval = setInterval(() => {
    const body = document.getElementById('compose-planner-body');
    if (!body) return;
    const status = body.querySelector('.planner-status span');
    if (status) {
      const secs = Math.floor((Date.now() - _composePlannerStartTime) / 1000);
      const titleMatches = _composePlannerAccumText.match(/"name"\s*:\s*"[^"]+"/g);
      const count = titleMatches ? titleMatches.length : 0;
      status.textContent = count > 0
        ? 'Building plan\u2026 ' + count + ' section' + (count !== 1 ? 's' : '') + ' so far (' + secs + 's)'
        : 'Building content plan\u2026 ' + secs + 's';
    }
  }, 1000);

  _composePlannerEntryListener = (data) => {
    if (data.session_id !== _composePlannerSessionId) return;
    if (!data.entry) return;
    const text = data.entry.text || '';
    if (!text || data.entry.kind !== 'asst') return;
    _composePlannerAccumText += text;
  };

  _composePlannerStateListener = (data) => {
    if (data.session_id !== _composePlannerSessionId) return;
    if (data.state === 'idle' || data.state === 'stopped') {
      if (_composePlannerTimerInterval) { clearInterval(_composePlannerTimerInterval); _composePlannerTimerInterval = null; }
      if (_composePlannerProposal) return;
      _showComposePlanResult(_composePlannerAccumText);
      _composePlannerAccumText = '';
    }
  };

  socket.on('session_entry', _composePlannerEntryListener);
  socket.on('session_state', _composePlannerStateListener);
}

function _detachComposePlannerListeners() {
  if (_composePlannerEntryListener) { socket.off('session_entry', _composePlannerEntryListener); _composePlannerEntryListener = null; }
  if (_composePlannerStateListener) { socket.off('session_state', _composePlannerStateListener); _composePlannerStateListener = null; }
  if (_composePlannerTimerInterval) { clearInterval(_composePlannerTimerInterval); _composePlannerTimerInterval = null; }
}

function _showComposePlanResult(rawText) {
  const body = document.getElementById('compose-planner-body');
  if (!body) return;

  let parsed = null;
  // Try 1: ```json ... ```
  const m1 = rawText.match(/```(?:json)?\s*([\s\S]*?)```/);
  if (m1) { try { parsed = JSON.parse(m1[1]); } catch (_) {} }
  // Try 2: whole text as JSON
  if (!parsed) { try { parsed = JSON.parse(rawText.trim()); } catch (_) {} }
  // Try 3: brace extraction
  if (!parsed) {
    const s = rawText.indexOf('{');
    if (s >= 0) {
      let depth = 0, end = -1, inStr = false, esc = false;
      for (let i = s; i < rawText.length; i++) {
        const c = rawText[i];
        if (esc) { esc = false; continue; }
        if (c === '\\' && inStr) { esc = true; continue; }
        if (c === '"') { inStr = !inStr; continue; }
        if (inStr) continue;
        if (c === '{') depth++;
        else if (c === '}') { depth--; if (depth === 0) { end = i; break; } }
      }
      if (end > s) { try { parsed = JSON.parse(rawText.slice(s, end + 1)); } catch (_) {} }
    }
  }

  if (parsed && parsed.sections && parsed.sections.length > 0) {
    _composePlannerProposal = parsed;
    const count = _countComposePlanSections(parsed.sections);
    body.innerHTML =
      '<div class="planner-result">' +
        '<div class="planner-result-header"><strong>' + count + ' sections</strong> proposed</div>' +
        _renderComposePlanTree(parsed.sections) +
        '<div class="planner-actions">' +
          '<button class="planner-accept-btn" id="compose-planner-accept" onclick="_acceptComposePlan()">Add ' + count + ' sections to Board</button>' +
        '</div>' +
        '<div class="planner-hint">Want changes? Type below and send.</div>' +
      '</div>';
  } else {
    body.innerHTML =
      '<div class="planner-result">' +
        '<div class="planner-error">Couldn\'t parse a section structure. Try rephrasing below.</div>' +
        (rawText ? '<pre style="max-height:200px;overflow:auto;white-space:pre-wrap;font-size:11px;margin-top:8px;padding:8px;background:var(--bg-subtle);border-radius:6px;">' + (typeof escHtml === 'function' ? escHtml(rawText.slice(0, 2000)) : rawText.slice(0, 2000)) + '</pre>' : '') +
      '</div>';
  }
}

function _renderComposePlanTree(sections, depth) {
  depth = depth || 0;
  let html = '<div class="planner-tree' + (depth === 0 ? ' planner-tree-root' : '') + '">';
  for (const sec of sections) {
    const hasSubs = sec.subsections && sec.subsections.length > 0;
    const typeLabel = sec.artifact_type || 'text';
    html += '<div class="planner-node" data-depth="' + depth + '">';
    html += '<div class="planner-node-row">';
    html += hasSubs ? '<span class="planner-chevron" onclick="this.parentElement.parentElement.classList.toggle(\'collapsed\')"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 18 15 12 9 6"/></svg></span>'
                    : '<span class="planner-bullet">&bull;</span>';
    html += '<span class="planner-node-title">' + (typeof escHtml === 'function' ? escHtml(sec.name) : sec.name) + '</span>';
    html += '<span style="font-size:10px;color:var(--text-muted);margin-left:6px;">' + typeLabel + '</span>';
    if (hasSubs) html += '<span class="planner-sub-count">' + sec.subsections.length + '</span>';
    html += '</div>';
    if (hasSubs) html += _renderComposePlanTree(sec.subsections, depth + 1);
    html += '</div>';
  }
  html += '</div>';
  return html;
}

function _countComposePlanSections(sections) {
  let c = 0;
  for (const s of sections) { c++; if (s.subsections) c += _countComposePlanSections(s.subsections); }
  return c;
}

async function _acceptComposePlan() {
  if (!_composePlannerProposal || !_composePlannerProposal.sections || !_composeProject) return;
  const btn = document.getElementById('compose-planner-accept');
  if (btn) { btn.disabled = true; btn.textContent = 'Creating\u2026'; }

  try {
    const res = await fetch('/api/compose/projects/' + encodeURIComponent(_composeProject.id) + '/planner/accept', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        sections: _composePlannerProposal.sections,
        parent_id: _composePlannerScopeParentId || null,
      }),
    });
    if (!res.ok) throw new Error('Failed to create sections');
    const data = await res.json();
    const count = data.created_count || 0;
    if (typeof showToast === 'function') showToast('Created ' + count + ' section' + (count !== 1 ? 's' : ''));
    _closeComposePlanner();
    initCompose();
  } catch (e) {
    if (typeof showToast === 'function') showToast('Failed: ' + e.message, true);
    if (btn) { btn.disabled = false; btn.textContent = 'Add to Board'; }
  }
}

function _refineComposePlan() {
  const ta = document.getElementById('compose-planner-refine');
  const text = ta ? ta.value.trim() : '';
  if (!text || !_composePlannerSessionId) return;
  ta.value = '';

  _composePlannerProposal = null;
  const body = document.getElementById('compose-planner-body');
  if (body) {
    body.innerHTML = '<div class="planner-status"><div class="planner-spinner"></div><span>Refining plan\u2026</span></div>';
  }

  _composePlannerAccumText = '';
  _composePlannerStartTime = Date.now();

  socket.emit('send_message', {
    session_id: _composePlannerSessionId,
    text: text,
  });
}

function _closeComposePlanner() {
  _composePlannerProposal = null;
  _detachComposePlannerListeners();
  _composePlannerSessionId = null;
  _composePlannerScopeParentId = null;
  const panel = document.getElementById('compose-planner-panel');
  if (panel) {
    panel.classList.remove('open');
    setTimeout(() => panel.remove(), 300);
  }
}

// ═══════════════════════════════════════════════════════════════
// COMPOSE SETTINGS POPOVER — gear icon opens name + shared prompts toggle
// ═══════════════════════════════════════════════════════════════

function _composeToggleSettings(event) {
  event.stopPropagation();
  const existing = document.getElementById('compose-settings-popover');
  if (existing) { existing.remove(); return; }
  if (!_composeProject) return;

  const btn = event.currentTarget;
  const rect = btn.getBoundingClientRect();

  const pop = document.createElement('div');
  pop.id = 'compose-settings-popover';
  pop.className = 'compose-settings-popover';
  pop.style.top = (rect.bottom + 6) + 'px';
  pop.style.right = (window.innerWidth - rect.right) + 'px';

  const isOn = !!_composeProject.shared_prompts_enabled;
  pop.innerHTML = `
    <div class="compose-settings-row">
      <label class="compose-settings-label">Name</label>
      <input id="compose-settings-name" class="compose-settings-input" value="${typeof escHtml === 'function' ? escHtml(_composeProject.name) : _composeProject.name}" />
    </div>
    <div class="compose-settings-row" style="margin-top:10px;">
      <label class="compose-settings-label">Shared Prompts</label>
      <button id="compose-settings-shared-toggle" class="compose-settings-toggle ${isOn ? 'active' : ''}"
              onclick="_composeToggleSharedPrompts()" title="When on, user prompts are shared across all agents">
        <span class="compose-settings-toggle-knob"></span>
      </button>
    </div>
    <div class="compose-settings-hint">When on, user prompts are logged and shared across all section agents.</div>
  `;

  document.body.appendChild(pop);

  // Name input — save on blur or Enter
  const nameInput = document.getElementById('compose-settings-name');
  const saveName = () => {
    const newName = nameInput.value.trim();
    if (newName && newName !== _composeProject.name) {
      _composeProject.name = newName;
      fetch('/api/compose/projects/' + encodeURIComponent(_composeProject.id), {
        method: 'PUT', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name: newName}),
      });
      _updateComposeRootHeader();
      _renderComposeSidebar();
    }
  };
  nameInput.addEventListener('blur', saveName);
  nameInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); saveName(); nameInput.blur(); } });

  // Click outside closes
  setTimeout(() => {
    const closer = (e) => {
      if (!pop.contains(e.target) && e.target !== btn && !btn.contains(e.target)) {
        pop.remove();
        document.removeEventListener('mousedown', closer);
      }
    };
    document.addEventListener('mousedown', closer);
  }, 0);
}

function _composeToggleSharedPrompts() {
  if (!_composeProject) return;
  const newVal = !_composeProject.shared_prompts_enabled;
  _composeProject.shared_prompts_enabled = newVal;

  // Update toggle button state
  const toggle = document.getElementById('compose-settings-shared-toggle');
  if (toggle) toggle.classList.toggle('active', newVal);

  // Persist
  fetch('/api/compose/projects/' + encodeURIComponent(_composeProject.id), {
    method: 'PUT', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({shared_prompts_enabled: newVal}),
  });

  _composeUpdateSharedBadge();
}

function _composeUpdateSharedBadge() {
  const badge = document.getElementById('compose-shared-badge');
  if (badge) {
    badge.style.display = (_composeProject && _composeProject.shared_prompts_enabled) ? 'inline' : 'none';
  }
}

// --- NB-11: Render section cards in compose board ---

const COMPOSE_STATUS_COLUMNS = [
  { key: 'drafting',    label: 'Drafting',    color: '#4ecdc4' },
  { key: 'reviewing',   label: 'Reviewing',   color: '#f0ad4e' },
  { key: 'complete',    label: 'Complete',     color: '#3fb950' },
];

// Category-level SVG icons (8 categories covering 48+ artifact types)
const _COMPOSE_CATEGORY_SVGS = {
  doc:        '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>',
  data:       '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>',
  slides:     '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>',
  diagram:    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/><path d="M10 7h4v4"/><path d="M14 17h-4v-4"/></svg>',
  code:       '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>',
  structured: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg>',
  comm:       '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z"/><polyline points="22,6 12,13 2,6"/></svg>',
  default:    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="9" y1="3" x2="9" y2="21"/></svg>',
};

// Map every artifact type key to its icon category
const _COMPOSE_TYPE_TO_CATEGORY = {
  // Documents
  report:'doc', proposal:'doc', memo:'doc', whitepaper:'doc', letter:'doc', blog:'doc',
  sop:'doc', contract:'doc', script:'doc', copy:'doc', press:'doc', newsletter:'doc',
  faq:'doc', 'meeting-notes':'doc', 'case-study':'doc', resume:'doc', 'legal-brief':'doc', 'exec-summary':'doc',
  // Data
  spreadsheet:'data', 'financial-model':'data', budget:'data', forecast:'data',
  survey:'data', comparison:'data', scorecard:'data', inventory:'data', dataset:'data',
  // Presentations
  'pitch-deck':'slides', 'board-deck':'slides', training:'slides', 'sales-deck':'slides', webinar:'slides',
  // Diagrams
  flowchart:'diagram', 'org-chart':'diagram', architecture:'diagram', timeline:'diagram',
  'mind-map':'diagram', wireframe:'diagram', infographic:'diagram', 'process-map':'diagram', 'er-diagram':'diagram',
  // Code
  code:'code', config:'code', html:'code',
  // Structured
  plan:'structured', checklist:'structured', questionnaire:'structured', rubric:'structured',
  // Communication
  email:'comm', social:'comm', 'talking-points':'comm',
  // Legacy fallbacks
  text:'doc', data:'data',
};

// Grouped artifact types for the type picker UI
const COMPOSE_ARTIFACT_TYPES = {
  'Documents': [
    {key:'report', label:'Report'}, {key:'proposal', label:'Proposal'}, {key:'memo', label:'Memo'},
    {key:'whitepaper', label:'White Paper'}, {key:'letter', label:'Letter'}, {key:'blog', label:'Blog Post'},
    {key:'sop', label:'SOP / Manual'}, {key:'contract', label:'Contract'}, {key:'script', label:'Script'},
    {key:'copy', label:'Marketing Copy'}, {key:'press', label:'Press Release'}, {key:'newsletter', label:'Newsletter'},
    {key:'faq', label:'FAQ'}, {key:'meeting-notes', label:'Meeting Notes'}, {key:'case-study', label:'Case Study'},
    {key:'resume', label:'Resume / CV'}, {key:'legal-brief', label:'Legal Brief'}, {key:'exec-summary', label:'Executive Summary'},
  ],
  'Data': [
    {key:'spreadsheet', label:'Spreadsheet'}, {key:'financial-model', label:'Financial Model'},
    {key:'budget', label:'Budget'}, {key:'forecast', label:'Forecast'}, {key:'survey', label:'Survey Results'},
    {key:'comparison', label:'Comparison Matrix'}, {key:'scorecard', label:'Scorecard'},
    {key:'inventory', label:'Inventory / Catalog'}, {key:'dataset', label:'Dataset'},
  ],
  'Presentations': [
    {key:'pitch-deck', label:'Pitch Deck'}, {key:'board-deck', label:'Board Deck'},
    {key:'training', label:'Training Material'}, {key:'sales-deck', label:'Sales Deck'}, {key:'webinar', label:'Webinar Slides'},
  ],
  'Diagrams': [
    {key:'flowchart', label:'Flowchart'}, {key:'org-chart', label:'Org Chart'},
    {key:'architecture', label:'Architecture Diagram'}, {key:'timeline', label:'Timeline'},
    {key:'mind-map', label:'Mind Map'}, {key:'wireframe', label:'Wireframe'},
    {key:'infographic', label:'Infographic'}, {key:'process-map', label:'Process Map'}, {key:'er-diagram', label:'ER Diagram'},
  ],
  'Code': [
    {key:'code', label:'Code / Script'}, {key:'config', label:'Configuration'}, {key:'html', label:'HTML / Web'},
  ],
  'Structured': [
    {key:'plan', label:'Project Plan'}, {key:'checklist', label:'Checklist'},
    {key:'questionnaire', label:'Questionnaire / Form'}, {key:'rubric', label:'Rubric / Scorecard'},
  ],
  'Communication': [
    {key:'email', label:'Email'}, {key:'social', label:'Social Media'}, {key:'talking-points', label:'Talking Points'},
  ],
};

// Resolve artifact icon for any type key
function _composeArtifactIcon(type) {
  const cat = _COMPOSE_TYPE_TO_CATEGORY[type] || 'default';
  return _COMPOSE_CATEGORY_SVGS[cat] || _COMPOSE_CATEGORY_SVGS.default;
}

// Label for any artifact type key
function _composeArtifactLabel(type) {
  for (const cat of Object.values(COMPOSE_ARTIFACT_TYPES)) {
    const found = cat.find(t => t.key === type);
    if (found) return found.label;
  }
  return type || 'Document';
}

// Legacy compat — old code references COMPOSE_ARTIFACT_ICONS[type]
const COMPOSE_ARTIFACT_ICONS = new Proxy({}, {
  get(_, key) { return _composeArtifactIcon(key); }
});

function _renderComposeSectionCards() {
  const board = document.getElementById('compose-sections-board');
  if (!board) return;

  if (!_composeSections || _composeSections.length === 0) {
    board.innerHTML = `
      <div class="compose-empty-board">
        <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="var(--text-muted)" stroke-width="1.5" stroke-linecap="round">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/>
        </svg>
        <div style="font-size:14px;font-weight:500;color:var(--text);margin:8px 0 4px;">No sections yet</div>
        <div style="font-size:12px;color:var(--text-muted);margin-bottom:14px;">Plan your composition with AI or start manually.</div>
        <button class="pm-btn pm-btn-primary" style="font-size:14px;padding:8px 24px;margin-bottom:8px;" onclick="_openComposePlanner()">Plan with AI</button>
        <button class="pm-btn" style="font-size:13px;padding:7px 20px;margin-bottom:8px;" onclick="_composeShowTemplates()">Use a Template</button>
        <div style="font-size:12px;color:var(--text-muted);cursor:pointer;text-decoration:underline;opacity:0.7;" onclick="composeAddSection()">or add a section manually</div>
      </div>`;
    return;
  }

  // Only show root sections (no parent) on the board, applying tag filter
  let rootSections = _composeSections.filter(s => !s.parent_id);
  if (_composeActiveTagFilter.length > 0) {
    rootSections = rootSections.filter(s => {
      const tags = s.tags || [];
      return _composeActiveTagFilter.every(t => tags.includes(t));
    });
  }

  let html = '<div class="kanban-columns-wrapper compose-columns-wrapper">';

  for (const col of COMPOSE_STATUS_COLUMNS) {
    const colSectionsRaw = rootSections.filter(s => s.status === col.key);
    const colSections = _composeSortSections(colSectionsRaw, col.key);
    const sortMode = _composeColumnSorts[col.key] || 'manual';
    const sortActive = sortMode !== 'manual' ? ' compose-sort-gear-active' : '';
    html += `<div class="kanban-column compose-column" data-status="${col.key}">
      <div class="kanban-column-header">
        <div class="kanban-column-color-bar" style="background:${col.color};"></div>
        <span class="kanban-column-name">${col.label}</span>
        <span class="kanban-column-count">${colSections.length}</span>
        <button class="compose-sort-gear${sortActive}" onclick="_composeToggleSortMenu('${col.key}', event)" title="Sort">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
        </button>
      </div>
      <div class="kanban-column-body" data-status="${col.key}"
           ondragover="_composeDragOver(event)" ondragleave="_composeDragLeave(event)"
           ondrop="_composeDrop(event, '${col.key}')">`;

    for (const sec of colSections) {
      const artifactIcon = COMPOSE_ARTIFACT_ICONS[sec.artifact_type] || COMPOSE_ARTIFACT_ICONS.default;
      const changingDot = sec.changing
        ? `<span class="compose-changing-dot" title="${typeof escHtml === 'function' ? escHtml(sec.change_note || 'Change in progress') : (sec.change_note || 'Change in progress')}"></span>`
        : '';
      const summary = sec.summary
        ? `<div class="compose-card-summary">${typeof escHtml === 'function' ? escHtml(sec.summary) : sec.summary}</div>`
        : '';
      const selectedClass = (_composeSelectedSection === sec.id) ? ' compose-card-selected' : '';
      const completeClass = (sec.status === 'complete') ? ' compose-card-complete' : '';
      const boardSelectedClass = _composeBoardSelected.has(sec.id) ? ' compose-board-card-selected' : '';
      const readyBadge = (sec.ready_for_review && sec.status !== 'complete')
        ? '<span class="compose-ready-badge"><span class="compose-ready-dot"></span>Ready for review</span>'
        : '';

      // P4-A: tag pills
      let tagPillsHtml = '';
      if (sec.tags && sec.tags.length > 0) {
        tagPillsHtml = '<div class="compose-card-tags">';
        for (const tag of sec.tags) {
          const tc = _composeTagColor(tag);
          tagPillsHtml += '<span class="compose-tag-pill" style="background:' + tc + '22;color:' + tc + ';border-color:' + tc + '44;" onclick="event.stopPropagation();_composeToggleTagFilter(\'' + (typeof escHtml === 'function' ? escHtml(tag) : tag) + '\')">' + (typeof escHtml === 'function' ? escHtml(tag) : tag) + '</span>';
        }
        tagPillsHtml += '</div>';
      }

      // P4-D: board selection checkbox
      const boardCb = '<input type="checkbox" class="compose-board-cb" ' + (_composeBoardSelected.has(sec.id) ? 'checked' : '') + ' onclick="event.stopPropagation();_composeBoardToggleSelect(event,\'' + sec.id + '\')" title="Select">';

      html += `<div class="kanban-card compose-card${selectedClass}${completeClass}${boardSelectedClass}" data-section-id="${sec.id}"
                   draggable="true"
                   ondragstart="_composeDragStart(event, '${sec.id}', '${col.key}')"
                   ondragend="_composeDragEnd(event)"
                   onclick="_composeBoardCardClick(event, '${sec.id}')"
                   oncontextmenu="event.preventDefault();event.stopPropagation();_composeCardContextMenu('${sec.id}', event)">
        ${boardCb}
        <span class="compose-drag-handle">&#8942;&#8942;</span>
        <div class="compose-card-header">
          <span class="compose-card-artifact-icon">${artifactIcon}</span>
          <div class="compose-card-title-row">
            <span class="compose-card-title">${typeof escHtml === 'function' ? escHtml(sec.name) : sec.name}</span>
            ${changingDot}
            ${(() => {
              if (!sec.session_id) return '';
              const _isRunning = typeof runningIds !== 'undefined' && runningIds.has(sec.session_id);
              return '<span class="compose-session-dot ' + (_isRunning ? 'running' : 'idle') + '"></span>';
            })()}
          </div>
          <span class="kanban-context-btn" onclick="event.stopPropagation();_composeCardContextMenu('${sec.id}', event)" title="Actions">&#8943;</span>
        </div>
        <div class="compose-card-meta">
          <span class="compose-card-status" style="background:${col.color}22;color:${col.color};">${col.label}</span>
          ${sec.artifact_type ? '<span class="compose-card-time">' + _composeArtifactLabel(sec.artifact_type) + '</span>' : ''}
          ${sec.updated_at ? '<span class="compose-card-time">' + _composeTimeAgo(Date.parse(sec.updated_at)) + '</span>' : ''}
          ${readyBadge}
        </div>
        ${tagPillsHtml}
        ${summary}
        ${(() => {
          const children = _composeSections.filter(c => c.parent_id === sec.id);
          if (children.length === 0) return '';
          const done = children.filter(c => c.status === 'complete').length;
          return '<div class="compose-card-subsection-count" style="font-size:11px;color:var(--text-dim);padding:4px 12px 6px;display:flex;align-items:center;gap:4px;"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg> ' + done + '/' + children.length + ' subsection' + (children.length !== 1 ? 's' : '') + '</div>';
        })()}
      </div>`;
    }

    html += '</div></div>';
  }

  html += '</div>';

  // Tag filter active indicator
  if (_composeActiveTagFilter.length > 0) {
    html = '<div class="compose-tag-filter-banner">Filtering by: ' + _composeActiveTagFilter.map(t => {
      const tc = _composeTagColor(t);
      return '<span class="compose-tag-pill" style="background:' + tc + '22;color:' + tc + ';border-color:' + tc + '44;">' + (typeof escHtml === 'function' ? escHtml(t) : t) + '</span>';
    }).join(' ') + ' <button class="compose-tag-filter-clear-btn" onclick="_composeClearTagFilter()">Clear</button></div>' + html;
  }

  // Conflict banner
  if (_composeConflicts && _composeConflicts.length > 0) {
    html = '<div class="compose-conflict-banner">\u26A0 ' + _composeConflicts.length + ' directive conflict' + (_composeConflicts.length !== 1 ? 's' : '') + ' need your attention <button onclick="_openConflictResolution()">Review</button></div>' + html;
  }

  // P4-D: Bulk action bar for board cards
  if (_composeBoardSelected.size > 0) {
    let bulkHtml = '<div class="compose-bulk-action-bar">';
    bulkHtml += '<span class="compose-bulk-action-count">' + _composeBoardSelected.size + ' selected</span>';
    bulkHtml += '<div class="compose-bulk-action-group">';
    // Move to dropdown
    bulkHtml += '<div class="compose-bulk-action-dropdown">';
    bulkHtml += '<button class="compose-bulk-action-btn" onclick="_composeBoardBulkMoveMenu(event)">Move to &#9662;</button>';
    bulkHtml += '</div>';
    // Launch All
    bulkHtml += '<button class="compose-bulk-action-btn" onclick="_composeBoardBulkLaunch()">Launch All</button>';
    // Delete
    bulkHtml += '<button class="compose-bulk-action-btn compose-bulk-action-danger" onclick="_composeBoardBulkDelete()">Delete</button>';
    bulkHtml += '</div>';
    bulkHtml += '<button class="compose-bulk-action-clear" onclick="_composeBoardClearSelection()">Clear selection</button>';
    bulkHtml += '</div>';
    html += bulkHtml;
  }

  board.innerHTML = html;
}

// --- End NB-11 ---

// ═══════════════════════════════════════════════════════════════
// COMPOSE BOARD DRAG-AND-DROP — mirrors kanban card drag between columns
// ═══════════════════════════════════════════════════════════════

let _composeDragState = null; // {sectionId, sourceStatus}

function _composeDragStart(event, sectionId, sourceStatus) {
  _composeDragState = { sectionId, sourceStatus };
  event.dataTransfer.effectAllowed = 'move';
  event.dataTransfer.setData('text/plain', sectionId);
  const card = event.currentTarget;
  requestAnimationFrame(() => card.classList.add('dragging'));
}

function _composeDragOver(event) {
  event.preventDefault();
  event.dataTransfer.dropEffect = 'move';
  const col = event.currentTarget.closest('.compose-column');
  if (col) col.classList.add('kanban-drop-target');
}

function _composeDragLeave(event) {
  const col = event.currentTarget.closest('.compose-column');
  if (col && !col.contains(event.relatedTarget)) {
    col.classList.remove('kanban-drop-target');
  }
}

function _composeDrop(event, targetStatus) {
  event.preventDefault();
  const col = event.currentTarget.closest('.compose-column');
  if (col) col.classList.remove('kanban-drop-target');
  if (!_composeDragState || !_composeProject) return;
  const { sectionId, sourceStatus } = _composeDragState;
  _composeDragState = null;
  if (sourceStatus === targetStatus) return;
  // Optimistic local update
  const sec = _composeSections.find(s => s.id === sectionId);
  if (sec) sec.status = targetStatus;
  _renderComposeSectionCards();
  // Persist to server
  const projId = _composeProject ? _composeProject.id : '';
  fetch('/api/compose/projects/' + encodeURIComponent(projId) + '/sections/' + sectionId, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ status: targetStatus }),
  }).then(r => r.json()).then(data => {
    if (!data.ok && !data.section) {
      // Revert on failure
      if (sec) sec.status = sourceStatus;
      _renderComposeSectionCards();
      if (typeof showToast === 'function') showToast('Move failed', true);
    }
  }).catch(() => {
    if (sec) sec.status = sourceStatus;
    _renderComposeSectionCards();
    if (typeof showToast === 'function') showToast('Move failed', true);
  });
}

function _composeDragEnd(event) {
  _composeDragState = null;
  event.currentTarget.classList.remove('dragging');
  document.querySelectorAll('.kanban-drop-target').forEach(el => el.classList.remove('kanban-drop-target'));
}

// ═══════════════════════════════════════════════════════════════
// P4-D: BOARD BULK SELECTION
// ═══════════════════════════════════════════════════════════════

function _composeBoardCardClick(event, sectionId) {
  // If ctrl/meta held, toggle selection instead of navigating
  if (event.ctrlKey || event.metaKey) {
    event.preventDefault();
    _composeBoardToggleSelect(event, sectionId);
    return;
  }
  // If shift held, range select
  if (event.shiftKey && _composeBoardLastClicked) {
    event.preventDefault();
    const rootSections = _composeSections.filter(s => !s.parent_id);
    const ids = rootSections.map(s => s.id);
    const from = ids.indexOf(_composeBoardLastClicked);
    const to = ids.indexOf(sectionId);
    if (from !== -1 && to !== -1) {
      const start = Math.min(from, to);
      const end = Math.max(from, to);
      for (let i = start; i <= end; i++) _composeBoardSelected.add(ids[i]);
      _renderComposeSectionCards();
    }
    return;
  }
  // Normal click — navigate if nothing selected, or if clicking outside checkbox
  if (_composeBoardSelected.size === 0) {
    navigateToSection(sectionId);
  } else {
    _composeBoardToggleSelect(event, sectionId);
  }
}

function _composeBoardToggleSelect(event, sectionId) {
  if (_composeBoardSelected.has(sectionId)) {
    _composeBoardSelected.delete(sectionId);
  } else {
    _composeBoardSelected.add(sectionId);
  }
  _composeBoardLastClicked = sectionId;
  _renderComposeSectionCards();
}

function _composeBoardClearSelection() {
  _composeBoardSelected = new Set();
  _composeBoardLastClicked = null;
  _renderComposeSectionCards();
}

function _composeBoardBulkMoveMenu(event) {
  event.stopPropagation();
  const existing = document.getElementById('compose-bulk-move-menu');
  if (existing) { existing.remove(); return; }
  const btn = event.currentTarget;
  const rect = btn.getBoundingClientRect();
  const menu = document.createElement('div');
  menu.id = 'compose-bulk-move-menu';
  menu.className = 'compose-sort-menu';
  menu.style.position = 'fixed';
  menu.style.bottom = (window.innerHeight - rect.top + 4) + 'px';
  menu.style.left = rect.left + 'px';
  for (const opt of COMPOSE_STATUS_COLUMNS) {
    menu.innerHTML += '<div class="compose-sort-item" onclick="_composeBoardBulkMove(\'' + opt.key + '\')">' + opt.label + '</div>';
  }
  document.body.appendChild(menu);
  const close = (e) => { if (!menu.contains(e.target)) { menu.remove(); document.removeEventListener('click', close, true); } };
  setTimeout(() => document.addEventListener('click', close, true), 0);
}

async function _composeBoardBulkMove(status) {
  const m = document.getElementById('compose-bulk-move-menu');
  if (m) m.remove();
  if (!_composeProject) return;
  const ids = [..._composeBoardSelected];
  const label = (COMPOSE_STATUS_COLUMNS.find(o => o.key === status) || {}).label || status;
  let moved = 0;
  for (const sid of ids) {
    const sec = _composeSections.find(s => s.id === sid);
    if (sec) sec.status = status;
    try {
      await fetch('/api/compose/projects/' + encodeURIComponent(_composeProject.id) + '/sections/' + sid + '/status', {
        method: 'PUT', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({status: status}),
      });
      moved++;
    } catch (e) { /* continue */ }
  }
  _composeBoardSelected = new Set();
  _renderComposeSectionCards();
  _updateComposeRootHeader();
  if (typeof showToast === 'function') showToast('Moved ' + moved + ' section' + (moved !== 1 ? 's' : '') + ' to ' + label);
}

async function _composeBoardBulkLaunch() {
  if (!_composeProject) return;
  const ids = [..._composeBoardSelected];
  const unlinked = ids.filter(id => { const s = _composeSections.find(x => x.id === id); return s && !s.session_id; });
  if (unlinked.length === 0) {
    if (typeof showToast === 'function') showToast('All selected sections already have sessions');
    return;
  }
  if (typeof showToast === 'function') showToast('Launching ' + unlinked.length + ' session' + (unlinked.length !== 1 ? 's' : '') + '...');
  let ok = 0;
  for (const sid of unlinked) {
    try {
      const resp = await fetch('/api/compose/projects/' + encodeURIComponent(_composeProject.id) + '/sections/' + sid + '/launch', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
      });
      const data = await resp.json();
      if (data.ok) {
        const sec = _composeSections.find(s => s.id === sid);
        if (sec) sec.session_id = data.session_id;
        ok++;
      }
    } catch (e) { /* continue */ }
  }
  _composeBoardSelected = new Set();
  _renderComposeSectionCards();
  _updateComposeRootHeader();
  if (typeof showToast === 'function') showToast(ok + ' session' + (ok !== 1 ? 's' : '') + ' launched');
}

async function _composeBoardBulkDelete() {
  if (!_composeProject) return;
  const ids = [..._composeBoardSelected];
  const names = ids.map(id => { const s = _composeSections.find(x => x.id === id); return s ? s.name : id; });
  if (!confirm('Delete ' + ids.length + ' section' + (ids.length !== 1 ? 's' : '') + '?\n\n' + names.join('\n'))) return;
  for (const sid of ids) {
    try {
      await fetch('/api/compose/projects/' + encodeURIComponent(_composeProject.id) + '/sections/' + sid, { method: 'DELETE' });
      _composeSections = _composeSections.filter(s => s.id !== sid);
    } catch (e) { /* continue */ }
  }
  _composeBoardSelected = new Set();
  _renderComposeSectionCards();
  _updateComposeRootHeader();
  if (typeof showToast === 'function') showToast('Deleted ' + ids.length + ' section' + (ids.length !== 1 ? 's' : ''));
}

// ═══════════════════════════════════════════════════════════════
// P4-A: TAG MANAGEMENT IN DRILL-DOWN
// ═══════════════════════════════════════════════════════════════

async function _composeAddTag(sectionId) {
  const input = document.getElementById('compose-tag-add-input');
  const tag = input ? input.value.trim().toLowerCase() : '';
  if (!tag || !_composeProject) return;
  input.value = '';
  try {
    const resp = await fetch('/api/compose/projects/' + encodeURIComponent(_composeProject.id) + '/sections/' + sectionId + '/tags', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({tag: tag}),
    });
    const data = await resp.json();
    if (data.ok) {
      const sec = _composeSections.find(s => s.id === sectionId);
      if (sec) sec.tags = data.tags;
      renderSectionDetail(sectionId);
    } else {
      if (typeof showToast === 'function') showToast(data.error || 'Failed to add tag', 'error');
    }
  } catch (e) {
    if (typeof showToast === 'function') showToast('Failed to add tag', 'error');
  }
}

async function _composeRemoveTag(sectionId, tag) {
  if (!_composeProject) return;
  try {
    const resp = await fetch('/api/compose/projects/' + encodeURIComponent(_composeProject.id) + '/sections/' + sectionId + '/tags/' + encodeURIComponent(tag), {
      method: 'DELETE',
    });
    const data = await resp.json();
    if (data.ok) {
      const sec = _composeSections.find(s => s.id === sectionId);
      if (sec) sec.tags = data.tags;
      renderSectionDetail(sectionId);
    }
  } catch (e) {
    if (typeof showToast === 'function') showToast('Failed to remove tag', 'error');
  }
}

// ═══════════════════════════════════════════════════════════════
// P4-C: SECTION DEPENDENCY ANALYSIS
// ═══════════════════════════════════════════════════════════════

function _composeGetRelatedSections(sectionId) {
  // Analyze facts from the board context — find sections that share fact keys
  if (!_composeProject) return [];
  const section = _composeSections.find(s => s.id === sectionId);
  if (!section) return [];

  // Build a map of fact keys per section from _composeSections summaries and names
  // Simple keyword overlap approach: find sections whose names share significant words
  const stopWords = new Set(['the','a','an','and','or','of','to','in','for','is','on','at','by','with','from','as','it','its']);
  const getKeywords = (text) => {
    if (!text) return new Set();
    return new Set(text.toLowerCase().replace(/[^a-z0-9\s]/g, '').split(/\s+/).filter(w => w.length > 2 && !stopWords.has(w)));
  };

  const thisKeywords = new Set([...getKeywords(section.name), ...getKeywords(section.summary)]);
  if (thisKeywords.size === 0) return [];

  const related = [];
  for (const other of _composeSections) {
    if (other.id === sectionId) continue;
    const otherKeywords = new Set([...getKeywords(other.name), ...getKeywords(other.summary)]);
    let shared = 0;
    for (const kw of thisKeywords) {
      if (otherKeywords.has(kw)) shared++;
    }
    if (shared > 0) {
      related.push({ id: other.id, name: other.name, shared: shared });
    }
  }
  related.sort((a, b) => b.shared - a.shared);
  return related.slice(0, 5); // top 5
}

// ═══════════════════════════════════════════════════════════════
// COMPOSE SECTION DETAIL — Drill-down view (mirrors kanban task detail)
// ═══════════════════════════════════════════════════════════════

const COMPOSE_STATUS_OPTIONS = [
  { key: 'drafting',    label: 'Drafting',    color: '#4ecdc4' },
  { key: 'reviewing',   label: 'Reviewing',   color: '#f0ad4e' },
  { key: 'complete',    label: 'Complete',     color: '#3fb950' },
];

function _composeSectionSkeleton() {
  return `
    <div class="kanban-drill-titlebar">
      <div class="kanban-drill-breadcrumb">
        <div class="skel-shimmer" style="width:50px;height:13px;border-radius:4px;"></div>
        <div class="skel-shimmer" style="width:6px;height:13px;border-radius:2px;margin:0 4px;"></div>
        <div class="skel-shimmer" style="width:100px;height:13px;border-radius:4px;"></div>
      </div>
    </div>
    <div class="kanban-drill-body">
      <div class="kanban-drill-split">
        <div class="kanban-drill-left">
          <div class="skel-shimmer" style="width:80px;height:24px;border-radius:6px;margin-bottom:16px;"></div>
          <div class="skel-shimmer" style="width:70%;height:22px;border-radius:5px;margin-bottom:8px;"></div>
          <div class="skel-shimmer" style="width:40%;height:11px;border-radius:3px;margin-bottom:20px;"></div>
          <div class="skel-shimmer" style="width:100%;height:72px;border-radius:8px;"></div>
        </div>
        <div class="kanban-drill-right">
          <div class="skel-shimmer" style="width:120px;height:12px;border-radius:3px;margin-bottom:14px;"></div>
          <div style="border:1px solid var(--border);border-radius:10px;padding:6px 8px;">
            <div class="skel-shimmer" style="width:100%;height:38px;border-radius:8px;margin-bottom:4px;"></div>
            <div class="skel-shimmer" style="width:100%;height:38px;border-radius:8px;"></div>
          </div>
        </div>
      </div>
    </div>`;
}

function navigateToSection(sectionId) {
  const content = document.getElementById('compose-sections-board');
  if (!content) return;
  // Hide the header and input target during drill-down
  const header = document.getElementById('compose-root-header');
  const target = document.getElementById('compose-input-target');
  if (header) header.style.display = 'none';
  if (target) target.style.display = 'none';
  content.innerHTML = _composeSectionSkeleton();
  const state = { view: 'compose', sectionId };
  history.pushState(state, '', window.location.pathname + '#compose/section/' + sectionId);
  renderSectionDetail(sectionId);
}

function _renderComposeBoard() {
  _composeSelectedSection = null;
  const header = document.getElementById('compose-root-header');
  const target = document.getElementById('compose-input-target');
  if (header) header.style.display = 'flex';
  if (target) target.style.display = 'flex';
  _updateComposeRootHeader();
  _updateComposeInputTarget();
  _renderComposeSectionCards();
}

function navigateToComposeBoard() {
  const state = { view: 'compose', sectionId: null };
  history.pushState(state, '', window.location.pathname + '#compose');
  _renderComposeBoard();
}

function renderSectionDetail(sectionId) {
  const board = document.getElementById('compose-sections-board');
  if (!board) return;

  // Hide header/target during drill-down
  const _hdr = document.getElementById('compose-root-header');
  const _tgt = document.getElementById('compose-input-target');
  if (_hdr) _hdr.style.display = 'none';
  if (_tgt) _tgt.style.display = 'none';

  const section = _composeSections.find(s => s.id === sectionId);
  if (!section) {
    board.innerHTML = '<div class="kanban-empty-state"><div style="font-size:15px;font-weight:500;margin-bottom:6px;">Section not found</div><button class="kanban-create-first-btn" onclick="navigateToComposeBoard()">Back to Board</button></div>';
    return;
  }

  // Update selection state
  _composeSelectedSection = sectionId;
  composeDetailTaskId = 'section:' + _composeProject.id + ':' + sectionId;

  const statusOpt = COMPOSE_STATUS_OPTIONS.find(o => o.key === section.status) || COMPOSE_STATUS_OPTIONS[0];
  const artifactIcon = COMPOSE_ARTIFACT_ICONS[section.artifact_type] || COMPOSE_ARTIFACT_ICONS.default;
  const projectName = _composeProject ? _composeProject.name : 'Composition';

  // ── Breadcrumb ──
  const _bcSep = '<span class="kanban-drill-sep"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="9 18 15 12 9 6"/></svg></span>';
  let html = '<div class="kanban-drill-titlebar">';
  html += '<div class="kanban-drill-breadcrumb">';
  html += '<span class="kanban-drill-crumb kanban-board-crumb" onclick="navigateToComposeBoard()"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></svg> Board</span>';
  if (section.parent_id) {
    const parentSec = _composeSections.find(s => s.id === section.parent_id);
    if (parentSec) {
      html += _bcSep;
      html += '<span class="kanban-drill-crumb" onclick="navigateToSection(\'' + parentSec.id + '\')" style="cursor:pointer;">' + (typeof escHtml === 'function' ? escHtml(parentSec.name) : parentSec.name) + '</span>';
    }
  }
  html += _bcSep;
  html += '<span class="kanban-drill-crumb current">' + (typeof escHtml === 'function' ? escHtml(section.name) : section.name) + '</span>';
  html += '</div></div>';

  // ── Detail body — left/right split ──
  html += '<div class="kanban-drill-body">';
  html += '<div class="kanban-drill-split">';

  // ════════════ LEFT: Section info ════════════
  html += '<div class="kanban-drill-left">';

  // Status badge (clickable)
  html += '<div class="kanban-drill-status kanban-status-clickable" style="background:' + statusOpt.color + '26;color:' + statusOpt.color + ';cursor:pointer;" onclick="event.stopPropagation();_composeStatusMenu(\'' + section.id + '\', \'' + section.status + '\', event)" title="Click to change status">' + statusOpt.label + ' <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="9 18 15 12 9 6"/></svg></div>';

  // Title (click to edit)
  html += '<div class="kanban-drill-title" id="compose-drill-title" onclick="_composeStartTitleEdit(\'' + section.id + '\', this)" title="Click to edit">' + (typeof escHtml === 'function' ? escHtml(section.name) : section.name) + '</div>';

  // Artifact type badge
  html += '<div style="display:flex;align-items:center;gap:6px;margin:4px 0 16px;font-size:12px;color:var(--text-dim);">';
  html += '<span>' + artifactIcon + '</span>';
  html += '<span>' + (section.artifact_type || 'text') + '</span>';
  if (section.changing) {
    html += '<span style="color:var(--warning);margin-left:8px;" title="' + (typeof escHtml === 'function' ? escHtml(section.change_note || 'Change in progress') : (section.change_note || 'Change in progress')) + '">&#9679; changing</span>';
  }
  html += '</div>';

  // Summary / description area
  html += '<div class="kanban-drill-desc-wrap">';
  if (section.summary) {
    html += '<div class="kanban-drill-desc" style="min-height:60px;">' + (typeof escHtml === 'function' ? escHtml(section.summary) : section.summary) + '</div>';
  } else {
    html += '<div class="kanban-drill-desc" style="min-height:60px;color:var(--text-dim);font-style:italic;">No summary yet. The AI agent will update this as it works.</div>';
  }
  html += '</div>';

  // ── P4-A: Tags section ──
  html += '<div class="compose-drill-tags" style="margin:12px 0;">';
  html += '<div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.8px;color:var(--text-dim);margin-bottom:6px;">Tags</div>';
  html += '<div class="compose-drill-tags-list">';
  if (section.tags && section.tags.length > 0) {
    for (const tag of section.tags) {
      const tc = _composeTagColor(tag);
      html += '<span class="compose-tag-pill" style="background:' + tc + '22;color:' + tc + ';border-color:' + tc + '44;">' + (typeof escHtml === 'function' ? escHtml(tag) : tag) + '<button class="compose-tag-remove" onclick="event.stopPropagation();_composeRemoveTag(\'' + sectionId + '\',\'' + (typeof escHtml === 'function' ? escHtml(tag) : tag) + '\')" title="Remove tag">&times;</button></span>';
    }
  }
  html += '<span class="compose-tag-add-wrap"><input type="text" id="compose-tag-add-input" class="compose-tag-add-input" placeholder="Add tag\u2026" onkeydown="if(event.key===\'Enter\'){event.preventDefault();_composeAddTag(\'' + sectionId + '\');}">';
  html += '<button class="compose-tag-add-btn" onclick="_composeAddTag(\'' + sectionId + '\')" title="Add tag">+</button></span>';
  html += '</div></div>';

  // ── Output preview panel (collapsed by default) ──
  html += '<div class="compose-preview-panel" id="compose-preview-panel-' + sectionId + '">';
  html += '<div class="compose-preview-header" onclick="_composeTogglePreview(\'' + sectionId + '\')">';
  html += '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" id="compose-preview-chevron-' + sectionId + '" style="transition:transform 0.2s;transform:rotate(0deg);"><polyline points="9 18 15 12 9 6"/></svg>';
  html += '<span>Output</span>';
  html += '</div>';
  html += '<div class="compose-preview-body" id="compose-preview-body-' + sectionId + '" style="display:none;"></div>';
  html += '</div>';

  // ── P4-C: Related sections (dependencies) ──
  const relatedSections = _composeGetRelatedSections(sectionId);
  if (relatedSections.length > 0) {
    html += '<div class="compose-dependencies" style="margin-top:16px;">';
    html += '<div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.8px;color:var(--text-dim);margin-bottom:6px;">Related Sections</div>';
    html += '<div class="compose-dependencies-list">';
    for (const rel of relatedSections) {
      html += '<span class="compose-dependency-link" onclick="navigateToSection(\'' + rel.id + '\')" title="Shares keywords">' + (typeof escHtml === 'function' ? escHtml(rel.name) : rel.name) + '</span>';
    }
    html += '</div></div>';
  }

  // ── Subsections list (children of this section) ──
  const _childSections = _composeSections.filter(c => c.parent_id === sectionId);
  if (_childSections.length > 0 || !section.parent_id) {
    html += '<div style="margin-top:20px;">';
    html += '<div class="kanban-drill-panel-header" style="display:flex;align-items:center;justify-content:space-between;">';
    html += '<span style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.8px;color:var(--text-dim);">Subsections</span>';
    const _childDone = _childSections.filter(c => c.status === 'complete').length;
    if (_childSections.length > 0) {
      const _childPct = Math.round((_childDone / _childSections.length) * 100);
      html += '<span class="kanban-drill-inline-progress"><span class="kanban-drill-inline-bar"><span class="kanban-drill-inline-fill" style="width:' + _childPct + '%"></span></span><span class="kanban-drill-inline-pct">' + _childPct + '%</span></span>';
    }
    html += '</div>';
    html += '<div class="kanban-drill-panel"><div class="kanban-drill-panel-body">';
    for (const child of _childSections) {
      const _cStatus = COMPOSE_STATUS_OPTIONS.find(o => o.key === child.status) || COMPOSE_STATUS_OPTIONS[0];
      html += '<div class="kanban-drill-subtask-row" data-section-id="' + child.id + '" style="cursor:pointer;" onclick="navigateToSection(\'' + child.id + '\')">';
      html += '<div class="kanban-drill-subtask-status" style="background:' + _cStatus.color + '26;color:' + _cStatus.color + ';">' + _cStatus.label + '</div>';
      html += '<span class="kanban-drill-subtask-title">' + (typeof escHtml === 'function' ? escHtml(child.name) : child.name) + '</span>';
      // Grandchild count
      const _gcCount = _composeSections.filter(gc => gc.parent_id === child.id).length;
      if (_gcCount > 0) {
        html += '<span class="kanban-drill-subtask-meta">' + _gcCount + ' subsection' + (_gcCount !== 1 ? 's' : '') + '</span>';
      }
      html += '<span class="kanban-drill-subtask-chevron"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="9 18 15 12 9 6"/></svg></span>';
      html += '</div>';
    }
    // Add Subsection button
    html += '<div class="kanban-drill-subtask-row kanban-drill-ghost-row" onclick="composeAddSection(\'' + sectionId + '\')" style="cursor:pointer;justify-content:center;">';
    html += '<span style="font-size:12px;color:var(--text-dim);">+ Add Subsection</span>';
    html += '</div>';
    html += '</div></div>';
    html += '</div>';
  }

  html += '</div>'; // drill-left

  // ════════════ RIGHT: Session panel ════════════
  html += '<div class="kanban-drill-right">';

  if (section.session_id) {
    // Session exists — show it
    const sess = (typeof allSessions !== 'undefined') ? allSessions.find(s => s.id === section.session_id) : null;
    const sessTitle = sess ? (sess.custom_title || sess.display_title || 'Session') : 'Session';
    const isRunning = (typeof runningIds !== 'undefined') ? runningIds.has(section.session_id) : false;

    html += '<div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.8px;color:var(--text-dim);margin-bottom:8px;">Session</div>';
    html += '<div class="kanban-drill-panel"><div class="kanban-drill-panel-body">';
    const _eSid = (typeof escHtml === 'function' ? escHtml(section.session_id) : section.session_id);
    const _eSessTitle = (typeof escHtml === 'function' ? escHtml(sessTitle) : sessTitle);
    html += '<div class="kanban-drill-subtask-row" style="cursor:pointer;" onclick="_composeOpenSession(\'' + _eSid + '\')">';
    html += '<span class="kanban-drill-subtask-status" style="background:' + (isRunning ? 'var(--green)26' : 'var(--bg-subtle)') + ';color:' + (isRunning ? 'var(--green)' : 'var(--text-dim)') + ';">' + (isRunning ? 'running' : 'idle') + '</span>';
    html += '<span class="kanban-drill-subtask-title">' + _eSessTitle + '</span>';
    html += '<div class="kanban-drill-subtask-actions" style="margin-left:auto;display:flex;gap:4px;" onclick="event.stopPropagation();">';
    html += '<button class="kanban-drill-action-btn" title="Rename" onclick="_composeRenameSession(\'' + _eSid + '\')"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg></button>';
    html += '<button class="kanban-drill-action-btn" title="Unlink" onclick="_composeUnlinkSession(\'' + section.id + '\',\'' + _eSid + '\')"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button>';
    html += '</div>';
    html += '<span class="kanban-drill-subtask-chevron"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="9 18 15 12 9 6"/></svg></span>';
    html += '</div>';
    html += '</div></div>';
  } else {
    // No session yet — show chooser
    html += '<div class="kanban-drill-chooser">';
    html += '<div style="font-size:12px;color:var(--text-dim);margin-bottom:6px;text-transform:uppercase;letter-spacing:0.5px;font-weight:600;">How to proceed</div>';

    html += '<div class="kanban-drill-chooser-card" onclick="_composeSpawnSession(\'' + section.id + '\')">';
    html += '<div class="kanban-drill-chooser-icon" style="color:var(--green);"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg></div>';
    html += '<div><div class="kanban-drill-chooser-title">Spawn session</div>';
    html += '<div class="kanban-drill-chooser-desc">Start an AI agent scoped to this section. It will read the shared context and work on ' + (section.artifact_type || 'text') + ' output.</div></div>';
    html += '</div>';

    html += '<div class="kanban-drill-chooser-card" onclick="_composeLinkSession(\'' + section.id + '\')">';
    html += '<div class="kanban-drill-chooser-icon" style="color:var(--accent);"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg></div>';
    html += '<div><div class="kanban-drill-chooser-title">Link existing session</div>';
    html += '<div class="kanban-drill-chooser-desc">Attach a session that\'s already running.</div></div>';
    html += '</div>';

    html += '<div class="kanban-drill-chooser-card" onclick="_openComposePlanner(\'' + section.id + '\')">';
    html += '<div class="kanban-drill-chooser-icon" style="color:var(--blue, #58a6ff);"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 2a7 7 0 0 1 7 7c0 2.38-1.19 4.47-3 5.74V17a1 1 0 0 1-1 1H9a1 1 0 0 1-1-1v-2.26C6.19 13.47 5 11.38 5 9a7 7 0 0 1 7-7z"/><line x1="9" y1="21" x2="15" y2="21"/><line x1="10" y1="24" x2="14" y2="24"/></svg></div>';
    html += '<div><div class="kanban-drill-chooser-title">Plan subsections with AI</div>';
    html += '<div class="kanban-drill-chooser-desc">Break this section into subsections using the AI planner.</div></div>';
    html += '</div>';

    html += '</div>';
  }

  html += '</div>'; // drill-right
  html += '</div>'; // drill-split
  html += '</div>'; // drill-body

  board.innerHTML = html;
}

// --- Compose status menu (mirrors kanban showStatusMenu) ---
function _composeStatusMenu(sectionId, currentStatus, event) {
  // Remove existing menu
  const old = document.querySelector('.kanban-status-dropdown');
  if (old) old.remove();

  const menu = document.createElement('div');
  menu.className = 'kanban-status-dropdown';
  menu.style.position = 'fixed';
  menu.style.left = event.clientX + 'px';
  menu.style.top = event.clientY + 'px';
  menu.style.zIndex = '9999';

  for (const opt of COMPOSE_STATUS_OPTIONS) {
    const item = document.createElement('div');
    item.className = 'kanban-status-option' + (opt.key === currentStatus ? ' active' : '');
    item.innerHTML = '<span class="kanban-status-dot" style="background:' + opt.color + ';"></span> ' + opt.label;
    item.onclick = async () => {
      menu.remove();
      try {
        await fetch('/api/compose/projects/' + _composeProject.id + '/sections/' + sectionId + '/status', {
          method: 'PUT',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({status: opt.key}),
        });
        // Update local state and re-render
        const sec = _composeSections.find(s => s.id === sectionId);
        if (sec) sec.status = opt.key;
        renderSectionDetail(sectionId);
      } catch (e) {
        console.error('Failed to update status:', e);
        if (typeof showToast === 'function') showToast('Failed to update status', 'error');
      }
    };
    menu.appendChild(item);
  }

  document.body.appendChild(menu);
  const close = (e) => { if (!menu.contains(e.target)) { menu.remove(); document.removeEventListener('click', close); } };
  setTimeout(() => document.addEventListener('click', close), 0);
}

// --- Compose inline title edit ---
function _composeStartTitleEdit(sectionId, el) {
  const section = _composeSections.find(s => s.id === sectionId);
  if (!section) return;
  const current = section.name;
  el.innerHTML = '<input type="text" class="kanban-drill-title-input" value="' + (typeof escHtml === 'function' ? escHtml(current) : current) + '" style="width:100%;font-size:inherit;font-weight:inherit;font-family:inherit;background:var(--bg-subtle);border:1px solid var(--border);border-radius:6px;padding:4px 8px;color:var(--text);outline:none;">';
  const input = el.querySelector('input');
  input.focus();
  input.select();
  const save = async () => {
    const newName = input.value.trim();
    if (!newName || newName === current) {
      el.textContent = current;
      return;
    }
    el.textContent = newName;
    try {
      await fetch('/api/compose/projects/' + _composeProject.id + '/sections/' + sectionId, {
        method: 'PUT',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name: newName}),
      });
      section.name = newName;
      if (typeof showToast === 'function') showToast('Renamed section');
    } catch (e) {
      el.textContent = current;
      if (typeof showToast === 'function') showToast('Failed to rename', 'error');
    }
  };
  input.onblur = save;
  input.onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); input.blur(); } if (e.key === 'Escape') { el.textContent = current; } };
}

// --- Compose session spawning (via /launch endpoint) ---
async function _composeSpawnSession(sectionId) {
  const section = _composeSections.find(s => s.id === sectionId);
  if (!section || !_composeProject) return;

  if (typeof showToast === 'function') showToast('Launching session for: ' + section.name);

  try {
    const resp = await fetch('/api/compose/projects/' + encodeURIComponent(_composeProject.id) + '/sections/' + sectionId + '/launch', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
    });
    const data = await resp.json();
    if (data.ok && data.session_id) {
      section.session_id = data.session_id;
      _composeSelectedSection = sectionId;
      composeDetailTaskId = 'section:' + _composeProject.id + ':' + sectionId;
      if (typeof showToast === 'function') showToast('Session started for: ' + section.name);
      if (_composeSelectedSection === sectionId) renderSectionDetail(sectionId);
      _renderComposeSectionCards();
    } else {
      if (typeof showToast === 'function') showToast(data.error || 'Failed to launch session', true);
    }
  } catch (e) {
    if (typeof showToast === 'function') showToast('Failed to launch session', true);
  }
}

// --- Open existing compose session ---
function _composeOpenSession(sessionId) {
  if (typeof openInGUI === 'function') {
    openInGUI(sessionId);
  } else if (typeof selectSession === 'function') {
    selectSession(sessionId);
  }
}

// Open a session inside compose view (mirrors _openSessionInKanban)
function _openSessionInCompose(sessionId) {
  const s = (typeof allSessions !== 'undefined') ? allSessions.find(x => x.id === sessionId) : null;
  const sessionName = s ? (s.custom_title || s.display_title || 'Session') : 'Session';
  const sectionId = _composeSelectedSection || null;
  const section = sectionId ? _composeSections.find(x => x.id === sectionId) : null;
  const sectionTitle = section ? (section.name || 'Section') : '';

  // Hide compose board, show main-body
  const cb = document.getElementById('compose-board');
  if (cb) cb.style.display = 'none';
  const mb = document.getElementById('main-body');
  if (mb) mb.style.display = '';

  // Remove old crumb bar
  const old = document.getElementById('compose-session-bar');
  if (old) old.remove();

  // Build breadcrumb bar
  const _esc = typeof escHtml === 'function' ? escHtml : (x => x);
  let crumbHtml = '<div class="kanban-drill-titlebar" id="compose-session-bar">';
  crumbHtml += '<div class="kanban-drill-breadcrumb">';
  crumbHtml += '<span class="kanban-drill-crumb kanban-board-crumb" onclick="_composeSessionClose(\'board\')"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="18" x2="21" y2="18"/></svg> Compose</span>';
  if (sectionId && sectionTitle) {
    crumbHtml += '<span class="kanban-drill-sep"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="9 18 15 12 9 6"/></svg></span>';
    crumbHtml += '<span class="kanban-drill-crumb" onclick="_composeSessionClose(\'' + _esc(sectionId) + '\')">' + _esc(sectionTitle) + '</span>';
  }
  crumbHtml += '<span class="kanban-drill-sep"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="9 18 15 12 9 6"/></svg></span>';
  crumbHtml += '<span class="kanban-drill-crumb current">' + _esc(sessionName) + '</span>';
  crumbHtml += '</div>';
  crumbHtml += '<div class="kanban-drill-actions">';
  crumbHtml += '<span class="btn-group-label" onclick="openActionsPopup()">Actions</span>';
  crumbHtml += '</div>';
  crumbHtml += '</div>';

  if (mb) mb.insertAdjacentHTML('beforebegin', crumbHtml);

  window._composeSessionSectionId = sectionId;

  // Open the session in the live panel
  _guiFocusPending = true;
  activeId = sessionId;
  localStorage.setItem('activeSessionId', sessionId);
  if (typeof runningIds !== 'undefined' && runningIds.has(sessionId)) guiOpenAdd(sessionId);
  if (typeof liveSessionId !== 'undefined' && liveSessionId && liveSessionId !== sessionId) { stopLivePanel(); }
  filterSessions();
}

// Rename a session linked in compose
function _composeRenameSession(sessionId) {
  const s = (typeof allSessions !== 'undefined') ? allSessions.find(x => x.id === sessionId) : null;
  const current = s ? (s.custom_title || s.display_title || '') : '';
  const newName = prompt('Rename session:', current);
  if (newName === null || !newName.trim()) return;
  const proj = localStorage.getItem('activeProject') || '';
  fetch('/api/rename/' + sessionId, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({title: newName.trim(), project: proj})
  }).then(r => r.json()).then(data => {
    if (data.ok) {
      if (s) { s.custom_title = newName.trim(); s.display_title = newName.trim(); }
      if (typeof showToast === 'function') showToast('Session renamed');
      // Re-render section detail to show new name
      if (_composeSelectedSection) renderSectionDetail(_composeSelectedSection);
    }
  }).catch(() => { if (typeof showToast === 'function') showToast('Rename failed', true); });
}

// Unlink a session from a compose section
function _composeUnlinkSession(sectionId, sessionId) {
  if (!confirm('Unlink this session from the section? The session will still exist in the sessions view.')) return;
  const projId = _composeProject ? _composeProject.id : '';
  fetch('/api/compose/projects/' + encodeURIComponent(projId) + '/sections/' + sectionId, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({session_id: ''})
  }).then(r => r.json()).then(data => {
    if (data.ok || data.section) {
      // Update local state
      const sec = _composeSections.find(x => x.id === sectionId);
      if (sec) sec.session_id = '';
      if (typeof showToast === 'function') showToast('Session unlinked');
      renderSectionDetail(sectionId);
    }
  }).catch(() => { if (typeof showToast === 'function') showToast('Unlink failed', true); });
}

// Close session view and return to compose board
function _composeSessionClose(target) {
  if (typeof liveSessionId !== 'undefined' && liveSessionId) { if (typeof stopLivePanel === 'function') stopLivePanel(); }
  activeId = null;
  if (typeof liveSessionId !== 'undefined') liveSessionId = null;
  window._composeSessionSectionId = null;
  localStorage.removeItem('activeSessionId');
  // Remove crumb bar
  const bar = document.getElementById('compose-session-bar');
  if (bar) bar.remove();
  // Restore panels
  const mb = document.getElementById('main-body');
  if (mb) mb.style.display = 'none';
  const cb = document.getElementById('compose-board');
  if (cb) cb.style.display = '';
  // Navigate back
  if (target === 'board') {
    navigateToComposeBoard();
  } else {
    renderSectionDetail(target);
  }
}

// --- Restore compose view from hash ---
function _restoreComposeSectionFromHash() {
  const hash = window.location.hash || '';
  if (hash.startsWith('#compose/section/')) {
    const sectionId = hash.replace('#compose/section/', '');
    if (sectionId && _composeSections.find(s => s.id === sectionId)) {
      renderSectionDetail(sectionId);
      return true;
    }
  }
  return false;
}

// --- Compose card context menu (right-click or dot-menu) ---

function _composeCardContextMenu(sectionId, event) {
  event.stopPropagation();
  // Close any existing menu
  if (typeof closeContextMenu === 'function') closeContextMenu();

  const section = _composeSections.find(s => s.id === sectionId);
  if (!section || !_composeProject) return;

  const menu = document.createElement('div');
  menu.className = 'kanban-context-menu';

  if (event.type === 'contextmenu') {
    menu.style.top = event.clientY + 'px';
    menu.style.left = event.clientX + 'px';
  } else {
    const rect = event.currentTarget.getBoundingClientRect();
    menu.style.top = rect.bottom + 'px';
    menu.style.left = rect.left + 'px';
  }

  let items = '';
  items += '<div class="kanban-context-item" onclick="closeContextMenu();navigateToSection(\'' + sectionId + '\')">Open</div>';
  items += '<div class="kanban-context-item" onclick="closeContextMenu();_composeRenameSection(\'' + sectionId + '\')">Rename</div>';
  items += '<div class="kanban-context-item" onclick="closeContextMenu();composeAddSection(\'' + sectionId + '\')">Add Subsection</div>';

  if (section.session_id) {
    items += '<div class="kanban-context-item" onclick="closeContextMenu();_composeOpenSession(\'' + section.session_id + '\')">Open Session</div>';
  } else {
    items += '<div class="kanban-context-item" onclick="closeContextMenu();_composeSpawnSession(\'' + sectionId + '\')">Spawn Session</div>';
    items += '<div class="kanban-context-item" onclick="closeContextMenu();_composeLinkSession(\'' + sectionId + '\')">Link Session</div>';
  }

  // Move to status
  items += '<div class="kanban-context-separator"></div>';
  for (const opt of COMPOSE_STATUS_OPTIONS) {
    if (opt.key !== section.status) {
      items += '<div class="kanban-context-item kanban-context-move" onclick="closeContextMenu();_composeMoveSection(\'' + sectionId + '\',\'' + opt.key + '\')">Move to ' + opt.label + '</div>';
    }
  }

  items += '<div class="kanban-context-separator"></div>';
  items += '<div class="kanban-context-item kanban-context-danger" onclick="closeContextMenu();_composeDeleteSection(\'' + sectionId + '\')">Delete</div>';

  menu.innerHTML = items;
  document.body.appendChild(menu);

  setTimeout(() => {
    document.addEventListener('click', closeContextMenu, { once: true });
  }, 0);
}

function _composeRenameSection(sectionId) {
  const section = _composeSections.find(s => s.id === sectionId);
  if (!section || !_composeProject) return;
  const newName = prompt('Rename section:', section.name);
  if (!newName || !newName.trim() || newName.trim() === section.name) return;
  fetch('/api/compose/projects/' + _composeProject.id + '/sections/' + sectionId, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name: newName.trim()}),
  }).then(r => r.json()).then(data => {
    if (data && data.ok) {
      section.name = newName.trim();
      _renderComposeSectionCards();
      showToast('Renamed section');
    } else {
      showToast(data.error || 'Failed to rename', 'error');
    }
  }).catch(() => showToast('Failed to rename', 'error'));
}

async function _composeMoveSection(sectionId, newStatus) {
  const section = _composeSections.find(s => s.id === sectionId);
  if (!section || !_composeProject) return;
  try {
    const resp = await fetch('/api/compose/projects/' + _composeProject.id + '/sections/' + sectionId + '/status', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({status: newStatus}),
    });
    if (!resp.ok) throw new Error('Failed');
    section.status = newStatus;
    _renderComposeSectionCards();
    const label = (COMPOSE_STATUS_OPTIONS.find(o => o.key === newStatus) || {}).label || newStatus;
    showToast('Moved to ' + label);
  } catch (e) {
    showToast('Failed to move section', 'error');
  }
}

async function _composeDeleteSection(sectionId) {
  const section = _composeSections.find(s => s.id === sectionId);
  const title = section ? section.name : 'this section';

  // Check for children — if any, show cascade confirmation modal
  if (_composeProject) {
    try {
      const resp = await fetch('/api/compose/projects/' + encodeURIComponent(_composeProject.id) + '/sections/' + sectionId + '/children');
      const data = await resp.json();
      if (data.count > 0) {
        _showCascadeDeleteModal(sectionId, title, data.children || [], data.count);
        return;
      }
    } catch (e) { /* fall through to inline confirm */ }
  }

  // No children — use inline confirm on the card
  const card = document.querySelector('.compose-card[data-section-id="' + sectionId + '"]');
  if (card) {
    const old = card.innerHTML;
    card.innerHTML = '<div class="kanban-delete-confirm"><span>Delete "' + escHtml(title.slice(0, 30)) + '"?</span><div class="kanban-delete-btns"><button class="kanban-delete-yes" onclick="event.stopPropagation();_execComposeDelete(\'' + sectionId + '\')">Delete</button><button class="kanban-delete-no" onclick="event.stopPropagation();_cancelComposeDelete(this,\'' + sectionId + '\')">Cancel</button></div></div>';
    card._oldHtml = old;
    card.onclick = null;
  } else {
    _execComposeDelete(sectionId);
  }
}

function _showCascadeDeleteModal(sectionId, title, children, count) {
  const _esc = typeof escHtml === 'function' ? escHtml : (x => x);
  const overlay = document.createElement('div');
  overlay.className = 'pm-overlay';
  overlay.style.cssText = 'display:flex;position:fixed;inset:0;background:rgba(0,0,0,0.6);backdrop-filter:blur(4px);z-index:5000;align-items:center;justify-content:center;';
  overlay.onclick = (e) => { if (e.target === overlay) overlay.remove(); };
  let childList = '';
  for (const c of children) {
    childList += '<li>' + _esc(c.name || c.id) + '</li>';
  }
  overlay.innerHTML = '<div class="pm-card compose-cascade-modal">' +
    '<div class="pm-title">Delete section and ' + count + ' subsection' + (count !== 1 ? 's' : '') + '?</div>' +
    '<div class="pm-body">' +
    '<p style="font-size:13px;color:var(--text-secondary);margin:0 0 8px;">Deleting <strong>' + _esc(title) + '</strong> will also remove:</p>' +
    '<ul class="cascade-children-list">' + childList + '</ul>' +
    '<div class="cascade-warning">This cannot be undone.</div>' +
    '</div>' +
    '<div class="pm-actions">' +
    '<button class="pm-btn" onclick="this.closest(\'.pm-overlay\').remove()">Cancel</button>' +
    '<button class="pm-btn" style="background:#ef4444;color:#fff;border-color:#ef4444;" onclick="this.closest(\'.pm-overlay\').remove();_execCascadeDelete(\'' + sectionId + '\')">Delete All</button>' +
    '</div></div>';
  document.body.appendChild(overlay);
}

async function _execCascadeDelete(sectionId) {
  if (!_composeProject) return;
  try {
    const resp = await fetch('/api/compose/projects/' + encodeURIComponent(_composeProject.id) + '/sections/' + sectionId + '?cascade=true', { method: 'DELETE' });
    if (!resp.ok) throw new Error('Delete failed');
    // Remove section and all its descendants from local state
    const toRemove = new Set([sectionId]);
    let changed = true;
    while (changed) {
      changed = false;
      for (const s of _composeSections) {
        if (s.parent_id && toRemove.has(s.parent_id) && !toRemove.has(s.id)) {
          toRemove.add(s.id);
          changed = true;
        }
      }
    }
    _composeSections = _composeSections.filter(s => !toRemove.has(s.id));
    if (_composeSelectedSection && toRemove.has(_composeSelectedSection)) {
      _composeSelectedSection = null;
      _renderComposeBoard();
    } else {
      _renderComposeSectionCards();
    }
    _updateComposeRootHeader();
    showToast('Section and subsections deleted');
  } catch (e) {
    showToast('Failed to delete section', true);
  }
}

function _cancelComposeDelete(btn, sectionId) {
  const card = document.querySelector('.compose-card[data-section-id="' + sectionId + '"]');
  if (card && card._oldHtml) {
    card.innerHTML = card._oldHtml;
    card.onclick = () => navigateToSection(sectionId);
  }
}

async function _execComposeDelete(sectionId) {
  if (!_composeProject) return;
  const card = document.querySelector('.compose-card[data-section-id="' + sectionId + '"]');
  if (card) {
    const col = card.closest('.compose-column');
    card.remove();
    if (col) {
      const countEl = col.querySelector('.kanban-column-count');
      if (countEl) countEl.textContent = col.querySelectorAll('.kanban-card').length;
    }
  }
  try {
    const resp = await fetch('/api/compose/projects/' + _composeProject.id + '/sections/' + sectionId, { method: 'DELETE' });
    if (!resp.ok) throw new Error('Delete failed');
    _composeSections = _composeSections.filter(s => s.id !== sectionId);
    if (_composeSelectedSection === sectionId) _composeSelectedSection = null;
    showToast('Section deleted');
    _updateComposeRootHeader();
  } catch (e) {
    showToast('Failed to delete section', 'error');
    initCompose();
  }
}

// ── P2: Output preview toggle + fetch ──
function _composeTogglePreview(sectionId) {
  const body = document.getElementById('compose-preview-body-' + sectionId);
  const chevron = document.getElementById('compose-preview-chevron-' + sectionId);
  if (!body) return;
  const isHidden = body.style.display === 'none';
  body.style.display = isHidden ? '' : 'none';
  if (chevron) chevron.style.transform = isHidden ? 'rotate(90deg)' : 'rotate(0deg)';
  if (isHidden && !body.dataset.loaded) {
    body.dataset.loaded = '1';
    _composeLoadPreview(sectionId);
  }
}

async function _composeLoadPreview(sectionId) {
  const body = document.getElementById('compose-preview-body-' + sectionId);
  if (!body || !_composeProject) return;
  body.innerHTML = '<div style="font-size:12px;color:var(--text-muted);padding:8px;">Loading...</div>';
  try {
    const resp = await fetch('/api/compose/projects/' + encodeURIComponent(_composeProject.id) + '/sections/' + sectionId + '/preview');
    const data = await resp.json();
    if (!data.files || data.files.length === 0) {
      body.innerHTML = '<div style="font-size:12px;color:var(--text-dim);padding:8px;font-style:italic;">No output yet \u2014 the agent hasn\'t started writing.</div>';
      return;
    }
    const _esc = typeof escHtml === 'function' ? escHtml : (x => x);
    let html = '';
    for (const f of data.files) {
      html += '<div class="compose-preview-file">';
      html += '<div class="compose-preview-file-name">' + _esc(f.name) + '</div>';
      const ext = (f.name || '').split('.').pop().toLowerCase();
      if (ext === 'md' && typeof mdParse === 'function') {
        html += '<div style="font-size:12px;line-height:1.6;">' + mdParse(f.content || '') + '</div>';
      } else if (ext === 'csv') {
        html += _composeCsvToTable(f.content || '');
      } else {
        html += '<pre>' + _esc(f.content || '') + '</pre>';
      }
      html += '</div>';
    }
    body.innerHTML = html;
  } catch (e) {
    body.innerHTML = '<div style="font-size:12px;color:var(--text-muted);padding:8px;">Failed to load preview.</div>';
  }
}

function _composeCsvToTable(csv) {
  const _esc = typeof escHtml === 'function' ? escHtml : (x => x);
  const lines = csv.trim().split('\n');
  if (lines.length === 0) return '<pre>' + _esc(csv) + '</pre>';
  let html = '<table>';
  for (let i = 0; i < lines.length; i++) {
    const cells = lines[i].split(',');
    html += '<tr>';
    const tag = i === 0 ? 'th' : 'td';
    for (const cell of cells) {
      html += '<' + tag + '>' + _esc(cell.trim()) + '</' + tag + '>';
    }
    html += '</tr>';
  }
  html += '</table>';
  return html;
}

// ── P2: Link session modal ──
function _composeLinkSession(sectionId) {
  if (!_composeProject) return;
  const _esc = typeof escHtml === 'function' ? escHtml : (x => x);

  // Sessions already linked in this composition
  const linkedIds = new Set(_composeSections.filter(s => s.session_id).map(s => s.session_id));
  const available = (typeof allSessions !== 'undefined' ? allSessions : []).filter(s => !linkedIds.has(s.id));

  const overlay = document.createElement('div');
  overlay.className = 'pm-overlay';
  overlay.style.cssText = 'display:flex;position:fixed;inset:0;background:rgba(0,0,0,0.6);backdrop-filter:blur(4px);z-index:5000;align-items:center;justify-content:center;';
  overlay.onclick = (e) => { if (e.target === overlay) overlay.remove(); };

  let listHtml = '';
  if (available.length === 0) {
    listHtml = '<div style="padding:16px;text-align:center;color:var(--text-muted);font-size:13px;">No unlinked sessions available.</div>';
  } else {
    for (const sess of available) {
      const sTitle = _esc(sess.custom_title || sess.display_title || sess.id);
      const isRunning = (typeof runningIds !== 'undefined') && runningIds.has(sess.id);
      const dotClass = isRunning ? 'running' : 'idle';
      listHtml += '<div class="compose-link-session-row" data-sid="' + _esc(sess.id) + '" onclick="_composeLinkSessionSelect(\'' + _esc(sectionId) + '\',\'' + _esc(sess.id) + '\')">';
      listHtml += '<span class="compose-session-dot ' + dotClass + '"></span>';
      listHtml += '<span>' + sTitle + '</span>';
      listHtml += '</div>';
    }
  }

  overlay.innerHTML = '<div class="pm-card compose-link-session-modal">' +
    '<div class="pm-title">Link Existing Session</div>' +
    '<div class="pm-body">' +
    '<input type="text" class="form-input" placeholder="Search sessions\u2026" style="width:100%;margin-bottom:8px;font-size:13px;" oninput="_composeLinkSessionFilter(this.value)">' +
    '<div class="compose-link-session-list" id="compose-link-session-list">' + listHtml + '</div>' +
    '</div>' +
    '<div class="pm-actions"><button class="pm-btn" onclick="this.closest(\'.pm-overlay\').remove()">Cancel</button></div>' +
    '</div>';
  document.body.appendChild(overlay);
}

function _composeLinkSessionFilter(query) {
  const list = document.getElementById('compose-link-session-list');
  if (!list) return;
  const q = query.toLowerCase();
  for (const row of list.querySelectorAll('.compose-link-session-row')) {
    const text = row.textContent.toLowerCase();
    row.style.display = (!q || text.indexOf(q) !== -1) ? '' : 'none';
  }
}

async function _composeLinkSessionSelect(sectionId, sessionId) {
  if (!_composeProject) return;
  // Close modal
  const overlay = document.querySelector('.compose-link-session-modal');
  if (overlay) { const ov = overlay.closest('.pm-overlay'); if (ov) ov.remove(); }

  try {
    const resp = await fetch('/api/compose/projects/' + encodeURIComponent(_composeProject.id) + '/sections/' + sectionId, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ session_id: sessionId }),
    });
    const data = await resp.json();
    if (data.ok || data.section) {
      const sec = _composeSections.find(s => s.id === sectionId);
      if (sec) sec.session_id = sessionId;
      if (typeof showToast === 'function') showToast('Session linked');
      if (_composeSelectedSection === sectionId) renderSectionDetail(sectionId);
      _renderComposeSectionCards();
    } else {
      if (typeof showToast === 'function') showToast(data.error || 'Failed to link session', true);
    }
  } catch (e) {
    if (typeof showToast === 'function') showToast('Failed to link session', true);
  }
}

// ── P2: Conflict resolution modal ──
function _openConflictResolution() {
  if (!_composeConflicts || _composeConflicts.length === 0) return;
  const _esc = typeof escHtml === 'function' ? escHtml : (x => x);

  const overlay = document.createElement('div');
  overlay.className = 'pm-overlay';
  overlay.id = 'compose-conflict-overlay';
  overlay.style.cssText = 'display:flex;position:fixed;inset:0;background:rgba(0,0,0,0.6);backdrop-filter:blur(4px);z-index:5000;align-items:center;justify-content:center;';
  overlay.onclick = (e) => { if (e.target === overlay) overlay.remove(); };

  let cardsHtml = '';
  for (const conflict of _composeConflicts) {
    const tsA = conflict.directive_a_time ? _composeTimeAgo(conflict.directive_a_time) : '';
    const tsB = conflict.directive_b_time ? _composeTimeAgo(conflict.directive_b_time) : '';
    cardsHtml += '<div class="compose-conflict-card" id="conflict-card-' + _esc(conflict.id) + '">';
    cardsHtml += '<div class="conflict-directive"><strong>Directive A:</strong> ' + _esc(conflict.directive_a_content || '') + (tsA ? '<div class="conflict-time">' + tsA + '</div>' : '') + '</div>';
    cardsHtml += '<div class="conflict-directive"><strong>Directive B:</strong> ' + _esc(conflict.directive_b_content || '') + (tsB ? '<div class="conflict-time">' + tsB + '</div>' : '') + '</div>';
    if (conflict.recommendation) {
      cardsHtml += '<div class="conflict-recommendation">AI recommendation: ' + _esc(conflict.recommendation) + '</div>';
    }
    cardsHtml += '<div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap;">';
    cardsHtml += '<button class="compose-conflict-action-btn" onclick="_resolveConflict(\'' + _esc(conflict.id) + '\',\'supersede\')">Supersede (B replaces A)</button>';
    cardsHtml += '<button class="compose-conflict-action-btn" onclick="_resolveConflict(\'' + _esc(conflict.id) + '\',\'keep_both\')">Keep Both</button>';
    cardsHtml += '<button class="compose-conflict-action-btn" onclick="_showConflictClarify(\'' + _esc(conflict.id) + '\')">Let me clarify\u2026</button>';
    cardsHtml += '</div>';
    cardsHtml += '<div id="conflict-clarify-' + _esc(conflict.id) + '" style="display:none;margin-top:8px;">';
    cardsHtml += '<input type="text" class="form-input" style="width:100%;font-size:12px;" placeholder="Type clarifying directive\u2026" id="conflict-clarify-input-' + _esc(conflict.id) + '">';
    cardsHtml += '<button class="compose-conflict-action-btn" style="margin-top:4px;" onclick="_submitConflictClarify(\'' + _esc(conflict.id) + '\')">Submit</button>';
    cardsHtml += '</div>';
    cardsHtml += '</div>';
  }

  overlay.innerHTML = '<div class="pm-card" style="max-width:560px;max-height:80vh;display:flex;flex-direction:column;">' +
    '<div class="pm-title">Resolve Directive Conflicts</div>' +
    '<div class="pm-body" style="overflow-y:auto;flex:1;min-height:0;">' + cardsHtml + '</div>' +
    '<div class="pm-actions"><button class="pm-btn" onclick="this.closest(\'.pm-overlay\').remove()">Close</button></div>' +
    '</div>';
  document.body.appendChild(overlay);
}

function _showConflictClarify(conflictId) {
  const el = document.getElementById('conflict-clarify-' + conflictId);
  if (el) el.style.display = el.style.display === 'none' ? '' : 'none';
}

async function _resolveConflict(conflictId, action) {
  if (!_composeProject) return;
  try {
    const resp = await fetch('/api/compose/projects/' + encodeURIComponent(_composeProject.id) + '/directives/resolve', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ conflict_id: conflictId, action: action }),
    });
    const data = await resp.json();
    if (data.ok) {
      _composeConflicts = _composeConflicts.filter(c => c.id !== conflictId);
      const card = document.getElementById('conflict-card-' + conflictId);
      if (card) card.remove();
      if (typeof showToast === 'function') showToast('Conflict resolved');
      if (_composeConflicts.length === 0) {
        const overlay = document.getElementById('compose-conflict-overlay');
        if (overlay) overlay.remove();
        _renderComposeSectionCards();
      }
      _updateComposeRootHeader();
    } else {
      if (typeof showToast === 'function') showToast(data.error || 'Failed to resolve conflict', true);
    }
  } catch (e) {
    if (typeof showToast === 'function') showToast('Failed to resolve conflict', true);
  }
}

async function _submitConflictClarify(conflictId) {
  const input = document.getElementById('conflict-clarify-input-' + conflictId);
  if (!input || !input.value.trim()) return;
  if (!_composeProject) return;
  try {
    // Resolve conflict as keep_both, then add the clarifying directive
    const resp = await fetch('/api/compose/projects/' + encodeURIComponent(_composeProject.id) + '/directives/resolve', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ conflict_id: conflictId, action: 'keep_both' }),
    });
    const data = await resp.json();
    if (data.ok || data.resolved) {
      // Submit the clarifying directive
      await fetch('/api/compose/projects/' + encodeURIComponent(_composeProject.id) + '/directives', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ content: input.value.trim(), scope: 'global', source: 'user' }),
      });
      _composeConflicts = _composeConflicts.filter(c => c.id !== conflictId);
      const card = document.getElementById('conflict-card-' + conflictId);
      if (card) card.remove();
      if (typeof showToast === 'function') showToast('Clarification submitted');
      if (_composeConflicts.length === 0) {
        const overlay = document.getElementById('compose-conflict-overlay');
        if (overlay) overlay.remove();
        _renderComposeSectionCards();
      }
      _updateComposeRootHeader();
    } else {
      if (typeof showToast === 'function') showToast(data.error || 'Failed to submit clarification', true);
    }
  } catch (e) {
    if (typeof showToast === 'function') showToast('Failed to submit clarification', true);
  }
}

function _updateComposeInputTarget() {
  const nameEl = document.getElementById('compose-input-target-name');
  if (!nameEl) return;

  if (_composeSelectedSection) {
    const section = _composeSections.find(s => s.id === _composeSelectedSection);
    nameEl.textContent = section ? section.name : 'unknown section';
    composeDetailTaskId = 'section:' + _composeProject.id + ':' + _composeSelectedSection;
  } else {
    nameEl.textContent = _composeProject ? _composeProject.name + ' (root)' : 'composition';
    composeDetailTaskId = _composeProject ? 'root:' + _composeProject.id : null;
  }
}

/**
 * Select a compose section (updates input target and compose_task_id).
 * Pass null to target the root orchestrator.
 */
function composeSelectSection(sectionId) {
  _composeSelectedSection = sectionId;
  _updateComposeInputTarget();
}

/**
 * Reset compose state — called when switching away from compose view.
 */
function resetComposeState() {
  _composeProject = null;
  _composeSections = [];
  _composeConflicts = [];
  composeDetailTaskId = null;
  _composeSelectedSection = null;
  _activeComposeProjectId = null;
  _composeProjectsList = [];
  _composeInitToken++;  // cancel any in-flight initCompose()
  _composeSelected = new Set();
  _composeLastClickedId = null;
  _composeSearchFilter = '';
  _composeFlushPendingDeletes();
  _composeFocusedId = null;
  _composeActionHistory = [];
  _composeActiveTagFilter = [];
  _composeBoardSelected = new Set();
  _composeBoardLastClicked = null;
  const header = document.getElementById('compose-root-header');
  const target = document.getElementById('compose-input-target');
  if (header) header.style.display = 'none';
  if (target) target.style.display = 'none';
}

/**
 * Group compose sessions in the sidebar under composition name.
 * Called during session list rendering when in compose mode.
 */
// Socket event handlers for compose updates
function _composeOnBoardRefresh(data) {
  if (viewMode === 'compose') initCompose();
}

function _composeOnTaskCreated(data) {
  if (viewMode === 'compose') initCompose();
}

function _composeOnTaskUpdated(data) {
  if (viewMode === 'compose') initCompose();
}

function _composeOnTaskMoved(data) {
  if (viewMode === 'compose') initCompose();
}

function _composeOnContextUpdated(data) {
  if (viewMode !== 'compose') return;
  if (!_composeProject || !data) return;
  const ctx = data.context;
  if (!ctx) return;
  // Update sections from context
  if (ctx.sections) {
    _composeSections = ctx.sections;
  }
  if (ctx.conflicts) {
    _composeConflicts = ctx.conflicts.filter(c => c.status === 'pending');
  }
  // If the selected section was deleted, fall back to root
  if (_composeSelectedSection && !_composeSections.find(s => s.id === _composeSelectedSection)) {
    _composeSelectedSection = null;
    _renderComposeBoard();
    return;
  }
  // If in drill-down, re-render the detail view; otherwise re-render the board
  if (_composeSelectedSection) {
    renderSectionDetail(_composeSelectedSection);
  } else {
    _updateComposeRootHeader();
    _updateComposeInputTarget();
    _renderComposeSectionCards();
  }
}

function _composeOnChanging(data) {
  if (viewMode !== 'compose') return;
  // Update the section in local state and re-render
  if (data && data.section_id) {
    const sec = _composeSections.find(s => s.id === data.section_id);
    if (sec) {
      sec.changing = data.changing;
      sec.change_note = data.change_note || null;
    }
    // If viewing this section's detail, re-render it; otherwise re-render cards
    if (_composeSelectedSection === data.section_id) {
      renderSectionDetail(data.section_id);
    } else if (!_composeSelectedSection) {
      _renderComposeSectionCards();
    }
  }
}

function getComposeSessionGroups(sessions) {
  if (!_composeProject) return null;

  const groups = [];
  const rootSessionId = _composeProject.root_session_id;
  const sectionSessionIds = new Set(
    _composeSections
      .filter(s => s.session_id)
      .map(s => s.session_id)
  );

  const composeSessions = sessions.filter(s =>
    s.id === rootSessionId || sectionSessionIds.has(s.id)
  );
  const otherSessions = sessions.filter(s =>
    s.id !== rootSessionId && !sectionSessionIds.has(s.id)
  );

  if (composeSessions.length > 0) {
    // Root session first
    const root = composeSessions.find(s => s.id === rootSessionId);
    const sections = composeSessions.filter(s => s.id !== rootSessionId);
    groups.push({
      name: _composeProject.name,
      root: root || null,
      sections: sections,
    });
  }

  return { groups, other: otherSessions };
}
