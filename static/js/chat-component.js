/* chat-component.js — Reusable chat UI component
 *
 * Usage:
 *   const chat = new ChatComponent(containerEl, {
 *     placeholder: 'Type a message...',
 *     onSend: async (text) => { ... return response string or {role, content} },
 *     systemMessage: 'I can help you find projects.',
 *     suggestions: ['Find Python projects', 'Show git repos'],
 *   });
 *   chat.addMessage('assistant', 'How can I help?');
 *   chat.destroy();
 */

class ChatComponent {
  constructor(container, opts = {}) {
    this.container = container;
    this.onSend = opts.onSend || (() => {});
    this.messages = [];
    this.sending = false;

    this.container.innerHTML = `
      <div class="chat-messages"></div>
      <div class="chat-suggestions"></div>
      <div class="chat-input-bar">
        <textarea class="chat-input" rows="1" placeholder="${escHtml(opts.placeholder || 'Type a message\u2026')}"
                  autocomplete="off" spellcheck="false"></textarea>
        <button class="chat-send-btn" title="Send">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
        </button>
      </div>`;
    this.container.classList.add('chat-component');

    this.messagesEl = this.container.querySelector('.chat-messages');
    this.inputEl = this.container.querySelector('.chat-input');
    this.sendBtn = this.container.querySelector('.chat-send-btn');
    this.suggestionsEl = this.container.querySelector('.chat-suggestions');

    // Set up suggestions
    if (opts.suggestions && opts.suggestions.length) {
      this.suggestionsEl.innerHTML = opts.suggestions.map(s =>
        `<button class="chat-suggestion">${escHtml(s)}</button>`
      ).join('');
      this.suggestionsEl.querySelectorAll('.chat-suggestion').forEach(btn => {
        btn.onclick = () => this.send(btn.textContent);
      });
    }

    // Show system/welcome message
    if (opts.systemMessage) {
      this.addMessage('assistant', opts.systemMessage);
    }

    // Event listeners
    this.sendBtn.onclick = () => this.send();
    this.inputEl.onkeydown = (e) => {
      if (_shouldSend(e)) { e.preventDefault(); this.send(); }
    };
    // Auto-resize textarea
    this.inputEl.oninput = () => {
      this.inputEl.style.height = 'auto';
      this.inputEl.style.height = Math.min(this.inputEl.scrollHeight, 120) + 'px';
    };
  }

  addMessage(role, content) {
    this.messages.push({role, content});
    const div = document.createElement('div');
    div.className = 'chat-msg chat-msg-' + role;
    div.innerHTML = role === 'assistant'
      ? '<div class="chat-msg-bubble chat-bubble-asst">' + (typeof mdParse === 'function' ? mdParse(content) : escHtml(content)) + '</div>'
      : '<div class="chat-msg-bubble chat-bubble-user">' + escHtml(content) + '</div>';
    this.messagesEl.appendChild(div);
    this.messagesEl.scrollTop = this.messagesEl.scrollHeight;
    return div;
  }

  addLoading() {
    const div = document.createElement('div');
    div.className = 'chat-msg chat-msg-assistant chat-loading';
    div.innerHTML = '<div class="chat-msg-bubble chat-bubble-asst"><span class="spinner"></span> Thinking\u2026</div>';
    this.messagesEl.appendChild(div);
    this.messagesEl.scrollTop = this.messagesEl.scrollHeight;
    return div;
  }

  removeLoading() {
    const el = this.messagesEl.querySelector('.chat-loading');
    if (el) el.remove();
  }

  async send(text) {
    if (this.sending) return;
    const msg = text || this.inputEl.value.trim();
    if (!msg) return;

    this.inputEl.value = '';
    this.inputEl.style.height = 'auto';
    this.suggestionsEl.innerHTML = '';  // hide suggestions after first message

    this.addMessage('user', msg);
    this.sending = true;
    this.sendBtn.disabled = true;
    this.inputEl.disabled = true;

    const loading = this.addLoading();
    try {
      const result = await this.onSend(msg, this.messages);
      this.removeLoading();
      if (typeof result === 'string') {
        this.addMessage('assistant', result);
      } else if (result && result.content) {
        this.addMessage(result.role || 'assistant', result.content);
      }
      // If result has suggestions, show them
      if (result && result.suggestions && result.suggestions.length) {
        this.suggestionsEl.innerHTML = result.suggestions.map(s =>
          `<button class="chat-suggestion">${escHtml(s)}</button>`
        ).join('');
        this.suggestionsEl.querySelectorAll('.chat-suggestion').forEach(btn => {
          btn.onclick = () => this.send(btn.textContent);
        });
      }
    } catch(e) {
      this.removeLoading();
      this.addMessage('assistant', 'Something went wrong. Please try again.');
    } finally {
      this.sending = false;
      this.sendBtn.disabled = false;
      this.inputEl.disabled = false;
      this.inputEl.focus();
    }
  }

  clear() {
    this.messages = [];
    this.messagesEl.innerHTML = '';
    this.suggestionsEl.innerHTML = '';
  }

  destroy() {
    this.container.innerHTML = '';
    this.container.classList.remove('chat-component');
  }
}
