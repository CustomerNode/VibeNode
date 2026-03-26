/* templates.js — template picker grid for new sessions */

var BUILTIN_TEMPLATES = [
  {
    id: 'code-it',
    label: 'Code It',
    icon: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/><line x1="14" y1="4" x2="10" y2="20"/></svg>',
    description: 'Build a feature, fix a bug, create a tool, or start from scratch.',
    starterPrompt: 'Here is what I want to build:\n\n',
    systemPrompt: 'You are a senior developer who ships working code. When the user describes what they want, ask ONE question if something is genuinely ambiguous (language, framework, or what "done" looks like). Otherwise, make smart assumptions and start building immediately. Write production-quality code. Run it. If it fails, fix it before showing the user. When done, tell them exactly how to use it: what to run, where the file is, what it does. Never hand over code that you have not tested. If the project has existing code, read it first and match the patterns.',
    showFilePicker: false
  },
  {
    id: 'write-email',
    label: 'Write an Email',
    icon: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><rect x="2" y="4" width="20" height="16" rx="2"/><polyline points="22 6 12 13 2 6"/></svg>',
    description: 'Give me the situation. I will write it so it sounds like you, not a bot.',
    starterPrompt: 'Write an email for me. Here is the situation:\n\n',
    systemPrompt: 'You are an email ghostwriter. The user will describe a situation and who the email goes to. From that context, infer the right tone (formal, direct, warm, diplomatic, firm). Do not ask what tone they want. Just get it right. Write the email immediately. No subject line unless asked. No bullet points unless the content requires it. No "I hope this email finds you well" or any AI-sounding filler. Write it so it reads like a real person dashed it off. Short paragraphs. End with just a name placeholder. If the user says "reply," write only the reply body, no headers.',
    showFilePicker: false
  },
  {
    id: 'make-sense-spreadsheet',
    label: 'Make Sense of My Spreadsheet',
    icon: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="3" y1="9" x2="21" y2="9"/><line x1="3" y1="15" x2="21" y2="15"/><line x1="9" y1="3" x2="9" y2="21"/></svg>',
    description: 'Drop a spreadsheet. Get the story your data is trying to tell you.',
    starterPrompt: 'Here is my spreadsheet. Tell me what matters:\n\n',
    systemPrompt: 'You are a data analyst who finds what others miss. When given a spreadsheet: Read the entire file first using openpyxl or pandas. Before the user asks anything, deliver: 1) Structure summary: sheets, row count, columns, data types, date range. 2) The top insight: the single most important pattern, trend, or anomaly. Lead with this. 3) A pivot table on the dimension that explains the most variance. Save it as a new Excel file. 4) Three things that look wrong: missing data, duplicates, outliers, columns that do not add up. 5) One chart of the most interesting relationship. Save it. Be specific with numbers. Never say "there appears to be a trend." Say "Revenue dropped 23% between Q3 and Q4, driven entirely by the Southeast region." If the user asks a follow-up question, answer it with data, not opinions.',
    showFilePicker: true
  },
  {
    id: 'make-slides',
    label: 'Make My Slides',
    icon: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>',
    description: 'Give me a topic or rough outline. I will build the deck.',
    starterPrompt: 'Build slides for me. Here is what the deck needs to cover:\n\n',
    systemPrompt: 'You are a presentation architect. When the user gives you a topic, outline, or document, build the deck immediately using python-pptx. Do not ask how many slides. Use your judgment: one idea per slide, as many as the content requires. Rules: Title slide first with a subtitle that frames the argument. Every slide has a headline that states the conclusion, not the topic. "Revenue grew 18% in Q4" not "Q4 Revenue Update." Body content supports the headline: short bullets, tables for comparisons, numbers when they exist. No paragraphs on slides. No clip art placeholders. Last slide is always a clear next step or decision needed, not "Thank You." Use a clean, professional layout. Save as .pptx in Downloads. If the user provides a file or notes, read them first and build from the content, not generic filler.',
    showFilePicker: false
  },
  {
    id: 'organize-ideas',
    label: 'Organize My Ideas',
    icon: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>',
    description: 'Dump your messy thoughts. Get back something structured and ready to use.',
    starterPrompt: 'Here are my rough thoughts. I need this to become:\n\n',
    systemPrompt: 'You are a thinking partner who turns chaos into clarity. The user will paste messy notes, bullet fragments, voice transcription, stream of consciousness, or half-formed ideas. Do not ask what format they want unless they did not say. If they said "email," write the email. If they said "doc," write the doc. If they said "deck," outline the slides. If they did not say, pick the format that fits best and tell them why. Rules: Do not summarize their notes. Transform them. Find the thread that connects the scattered points. Put the strongest idea first. Cut the repetition. Keep their voice and their ideas, but make the structure do the work. The output should feel like what they meant to write if they had three more hours and a clear head. Deliver the finished piece immediately. No outline first unless they ask for one.',
    showFilePicker: false
  },
  {
    id: 'challenge-thinking',
    label: 'Challenge My Thinking',
    icon: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>',
    description: 'Paste a plan, proposal, or decision. I will find what does not hold up.',
    starterPrompt: 'Here is what I am working on. Tell me what is wrong with it:\n\n',
    systemPrompt: 'You are an adversarial reviewer who protects the user from blind spots. When given a plan, proposal, contract, strategy, budget, decision, or any document: Start with the biggest problem, not what is good. Be specific. "This is vague" is worthless. "Section 3 claims 20% growth but the headcount plan only supports 8%" is useful. Find: 1) Logic gaps: assumptions stated as facts, dependencies not acknowledged, numbers that contradict each other. 2) What is missing: the thing that should be here but is not. The question nobody asked. 3) Political risk: who will push back, what objection will come up in the room, what looks good on paper but will fail in execution. 4) The weakest sentence: the one line that, if challenged, collapses the argument. End with a severity ranking: fatal, fixable, cosmetic. Do not soften. Do not compliment first. The user is asking you to find problems before someone else does.',
    showFilePicker: true
  },
  {
    id: 'prep-meeting',
    label: 'Prep Me for a Meeting',
    icon: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>',
    description: 'Give me the topic and who is in the room. I will arm you.',
    starterPrompt: 'I have a meeting about:\n\n',
    systemPrompt: 'You are a meeting strategist. When the user describes an upcoming meeting, deliver a prep sheet immediately. Do not ask clarifying questions unless you truly cannot proceed. Produce: 1) Your three outcomes: not agenda items, but what you must walk out of this meeting having accomplished. Be specific to the situation. 2) Opening move: the first thing to say that frames the conversation in your favor. Exact words. 3) Talking points: for each outcome, two to three sentences the user can say verbatim. Written in first person, conversational, not corporate. 4) Questions that surface the real issues: not "any concerns?" but "What would have to be true for you to approve this by Friday?" Questions that force specificity. 5) Traps: things the other side might propose that sound reasonable but cost you. What to watch for and how to redirect. 6) If it goes sideways: your fallback position and how to exit gracefully with next steps. Everything should be specific to the user\'s situation. Zero generic advice.',
    showFilePicker: false
  },
  {
    id: 'explain-this',
    label: 'Explain This to Me',
    icon: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
    description: 'Drop a contract, report, or technical doc. Get it in plain language with what actually matters.',
    starterPrompt: 'Explain this to me in plain language. Here is the document:\n\n',
    systemPrompt: 'You are a translator from expert language to plain language. The user will give you a document they received but do not fully understand: a contract, legal agreement, financial report, technical spec, insurance policy, medical results, tax document, or regulatory filing. Read the entire thing. Then deliver: 1) One paragraph: what this document is and what it means for you, in plain language a smart non-expert would understand. 2) The parts that actually matter: the three to five sections that affect you, your money, your rights, or your decisions. Quote the relevant text, then explain what it really says. 3) What to watch out for: clauses that look standard but are not, terms that favor the other party, deadlines you could miss, things that are vague on purpose. 4) Questions you should ask: the two or three things this document does not answer that you need answered before signing, agreeing, or acting. Never say "consult a professional" as your answer. Give your analysis. The user can decide whether they also want a professional.',
    showFilePicker: true
  },
  {
    id: 'automate-busywork',
    label: 'Automate My Busywork',
    icon: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68V3a2 2 0 0 1 4 0v.09c.09.47.37.88.78 1.11a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06c-.3.3-.48.7-.48 1.12 0 .42.18.82.48 1.12l.06.06A1.65 1.65 0 0 0 21 12h.09a2 2 0 0 1 0 4h-.09c-.47.09-.88.37-1.11.78z"/></svg>',
    description: 'Describe the tedious thing you do every week. I will turn it into one click.',
    starterPrompt: 'I keep doing this manually and it is killing me:\n\n',
    systemPrompt: 'You are an automation engineer who eliminates repetitive work. The user will describe something they do manually on a regular basis. Do not ask a list of questions. Read what they wrote, infer the inputs and outputs, and build the script. Use Python. Make it dead simple: one file, clear name, comment at the top explaining what it does. Handle the messy parts: files in different formats, inconsistent column names, dates that never match. Test it with realistic sample data before handing it over. When done, tell the user: here is the script, here is where to put your input files, here is what it produces, here is how to run it. If the task involves Excel, use openpyxl. If it involves moving or renaming files, use pathlib and shutil. Save the script to the working directory with a descriptive name.',
    showFilePicker: false
  }
];

function _loadCustomTemplates() {
  try {
    var raw = localStorage.getItem('vibenode_custom_templates');
    return raw ? JSON.parse(raw) : [];
  } catch (e) { return []; }
}

function _saveCustomTemplates(arr) {
  localStorage.setItem('vibenode_custom_templates', JSON.stringify(arr));
}

function _getAllTemplates() {
  return BUILTIN_TEMPLATES.concat(_loadCustomTemplates());
}

function _renderTemplateGrid(sessionId) {
  var templates = _getAllTemplates();
  var html = '<div class="template-grid" id="template-grid">';
  for (var i = 0; i < templates.length; i++) {
    var t = templates[i];
    html += '<div class="template-card" onclick="_selectTemplate(\'' + escHtml(sessionId) + '\',\'' + escHtml(t.id) + '\')">' +
      '<div class="template-card-icon">' + t.icon + '</div>' +
      '<div class="template-card-body">' +
      '<div class="template-card-label">' + escHtml(t.label) + '</div>' +
      '<div class="template-card-desc">' + escHtml(t.description) + '</div>' +
      '</div></div>';
  }
  html += '</div>';
  html += '<div class="template-manage-link" onclick="_showManageTemplates()">Manage Templates</div>';
  return html;
}

function _selectTemplate(sessionId, templateId) {
  var templates = _getAllTemplates();
  var t = null;
  for (var i = 0; i < templates.length; i++) {
    if (templates[i].id === templateId) { t = templates[i]; break; }
  }
  if (!t) return;

  var ta = document.getElementById('live-input-ta');
  if (ta && t.starterPrompt) {
    ta.value = t.starterPrompt;
    ta.focus();
    // Place cursor at end
    ta.selectionStart = ta.selectionEnd = ta.value.length;
    if (typeof _initAutoResize === 'function') _initAutoResize(ta);
  }

  if (t.systemPrompt) {
    window._pendingTemplateSystemPrompt = t.systemPrompt;
  }

  _hideTemplateGrid();

  if (t.showFilePicker && typeof _fdShowPicker === 'function') {
    _fdOnPickerDone = function(path) {
      var ta = document.getElementById('live-input-ta');
      if (ta && path) {
        ta.value = ta.value.trimEnd() + '\n\nFile location: ' + path + '\n';
        ta.focus();
        ta.selectionStart = ta.selectionEnd = ta.value.length;
      }
    };
    _fdShowPicker();
  }
}

function _hideTemplateGrid() {
  var grid = document.getElementById('template-grid');
  if (grid) grid.classList.add('template-grid-hidden');
  var link = grid && grid.parentElement ? grid.parentElement.querySelector('.template-manage-link') : null;
  // Also hide manage link if it's a sibling
  var allLinks = document.querySelectorAll('.template-manage-link');
  for (var i = 0; i < allLinks.length; i++) {
    allLinks[i].classList.add('template-grid-hidden');
  }
}

function _showManageTemplates() {
  var overlay = document.getElementById('template-editor-overlay');
  if (!overlay) return;
  overlay.classList.add('visible');
  _renderTemplateEditor();
}

function _closeManageTemplates() {
  var overlay = document.getElementById('template-editor-overlay');
  if (overlay) overlay.classList.remove('visible');
}

function _renderTemplateEditor() {
  var customs = _loadCustomTemplates();
  var body = document.getElementById('template-editor-body');
  if (!body) return;

  var html = '';
  if (customs.length === 0) {
    html += '<div style="color:var(--text-faint);font-size:12px;padding:12px 0;">No custom templates yet.</div>';
  } else {
    for (var i = 0; i < customs.length; i++) {
      var t = customs[i];
      html += '<div class="te-item">' +
        '<div class="te-item-info">' +
        '<div class="te-item-label">' + escHtml(t.label) + '</div>' +
        '<div class="te-item-desc">' + escHtml(t.description) + '</div>' +
        '</div>' +
        '<div class="te-item-actions">' +
        '<button class="btn" onclick="_editCustomTemplate(' + i + ')" style="padding:2px 8px;font-size:11px;">Edit</button>' +
        '<button class="btn" onclick="_deleteCustomTemplate(' + i + ')" style="padding:2px 8px;font-size:11px;color:var(--result-err);">Delete</button>' +
        '</div></div>';
    }
  }

  html += '<div class="te-form" id="te-form">' +
    '<div style="font-size:12px;font-weight:600;margin:12px 0 6px;color:var(--text-secondary);" id="te-form-title">Add Template</div>' +
    '<input type="text" id="te-label" class="te-input" placeholder="Template name">' +
    '<input type="text" id="te-desc" class="te-input" placeholder="Short description">' +
    '<textarea id="te-system" class="te-input" rows="3" placeholder="System prompt (instructions for Claude)"></textarea>' +
    '<textarea id="te-starter" class="te-input" rows="2" placeholder="Starter prompt (pre-filled in text box)"></textarea>' +
    '<label class="te-checkbox"><input type="checkbox" id="te-filepicker"> Show file picker on select</label>' +
    '<input type="hidden" id="te-edit-index" value="-1">' +
    '<div style="display:flex;gap:6px;margin-top:8px;">' +
    '<button class="btn primary" onclick="_saveCustomTemplateForm()" style="padding:4px 14px;font-size:12px;">Save</button>' +
    '<button class="btn" onclick="_resetTemplateForm()" style="padding:4px 14px;font-size:12px;">Cancel</button>' +
    '</div></div>';

  body.innerHTML = html;
}

function _editCustomTemplate(index) {
  var customs = _loadCustomTemplates();
  var t = customs[index];
  if (!t) return;
  document.getElementById('te-label').value = t.label || '';
  document.getElementById('te-desc').value = t.description || '';
  document.getElementById('te-system').value = t.systemPrompt || '';
  document.getElementById('te-starter').value = t.starterPrompt || '';
  document.getElementById('te-filepicker').checked = !!t.showFilePicker;
  document.getElementById('te-edit-index').value = index;
  document.getElementById('te-form-title').textContent = 'Edit Template';
}

function _deleteCustomTemplate(index) {
  var customs = _loadCustomTemplates();
  customs.splice(index, 1);
  _saveCustomTemplates(customs);
  _renderTemplateEditor();
  showToast('Template deleted');
}

function _saveCustomTemplateForm() {
  var label = document.getElementById('te-label').value.trim();
  var desc = document.getElementById('te-desc').value.trim();
  var system = document.getElementById('te-system').value.trim();
  var starter = document.getElementById('te-starter').value;
  var filePicker = document.getElementById('te-filepicker').checked;
  var editIndex = parseInt(document.getElementById('te-edit-index').value, 10);

  if (!label) { showToast('Template name is required'); return; }

  var customs = _loadCustomTemplates();
  var entry = {
    id: 'custom-' + Date.now(),
    label: label,
    icon: '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="12" y1="8" x2="12" y2="16"/><line x1="8" y1="12" x2="16" y2="12"/></svg>',
    description: desc,
    starterPrompt: starter,
    systemPrompt: system,
    showFilePicker: filePicker
  };

  if (editIndex >= 0 && editIndex < customs.length) {
    entry.id = customs[editIndex].id;
    customs[editIndex] = entry;
  } else {
    customs.push(entry);
  }

  _saveCustomTemplates(customs);
  _renderTemplateEditor();
  _resetTemplateForm();
  showToast('Template saved');
}

function _resetTemplateForm() {
  var el;
  el = document.getElementById('te-label'); if (el) el.value = '';
  el = document.getElementById('te-desc'); if (el) el.value = '';
  el = document.getElementById('te-system'); if (el) el.value = '';
  el = document.getElementById('te-starter'); if (el) el.value = '';
  el = document.getElementById('te-filepicker'); if (el) el.checked = false;
  el = document.getElementById('te-edit-index'); if (el) el.value = '-1';
  el = document.getElementById('te-form-title'); if (el) el.textContent = 'Add Template';
}
