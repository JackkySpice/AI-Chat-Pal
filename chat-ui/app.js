const chatEl = document.getElementById('chat');
const sendBtn = document.getElementById('send-btn');
const inputEl = document.getElementById('composer-input');
const toggleThoughtsEl = document.getElementById('toggle-thoughts');
const toggleThemeBtn = document.getElementById('toggle-theme-btn');
const newChatBtn = document.getElementById('new-chat-btn');
const chatListEl = document.getElementById('chat-list');
const toggleSidebarBtn = document.getElementById('toggle-sidebar-btn');
const sidebarEl = document.getElementById('sidebar');
const enterToSendEl = document.getElementById('toggle-enter-send');
const appRoot = document.querySelector('.app');
const settingsBtn = document.getElementById('settings-btn');
const settingsEl = document.getElementById('settings');
const settingsCloseBtn = document.getElementById('settings-close-btn');
const densityToggleEl = document.getElementById('toggle-density');
const suggestionsEl = document.getElementById('suggestions');
const themeLightEl = document.getElementById('theme-light');
const themeDarkEl = document.getElementById('theme-dark');
const themeAmoledEl = document.getElementById('theme-amoled');
const wallpaperToggleEl = document.getElementById('toggle-wallpaper');
const searchEl = document.getElementById('topbar-search');
const exportJsonBtn = document.getElementById('export-json-btn');
const exportMdBtn = document.getElementById('export-md-btn');
const fileInputEl = document.getElementById('file-input');

let chats = [
  { id: 'c1', title: 'Welcome', messages: [] }
];
let activeChatId = 'c1';

function setTheme(theme) {
  document.body.classList.remove('dark', 'amoled');
  if (theme === 'dark') document.body.classList.add('dark');
  if (theme === 'amoled') document.body.classList.add('amoled');
  localStorage.setItem('theme', theme);
}

function toggleTheme() {
  const isDark = document.body.classList.contains('dark');
  setTheme(isDark ? 'light' : 'dark');
}

function renderChatList() {
  chatListEl.innerHTML = '';
  chats.forEach((c) => {
    const item = document.createElement('div');
    item.className = 'sidebar-item' + (c.id === activeChatId ? ' active' : '');
    item.textContent = c.title;
    item.onclick = () => {
      activeChatId = c.id;
      renderChatList();
      renderMessages();
      closeSidebarOnMobile();
    };
    chatListEl.appendChild(item);
  });
}

function getActiveChat() { return chats.find(c => c.id === activeChatId); }

function addMessage(role, content, meta = {}) {
  const chat = getActiveChat();
  const message = {
    id: 'm_' + Math.random().toString(36).slice(2),
    role,
    content,
    createdAt: new Date(),
    thoughts: meta.thoughts || null, // { plan: string[], activity: string[] }
    streaming: !!meta.streaming,
  };
  chat.messages.push(message);
  renderMessages();
  return message;
}

function updateMessage(id, updates) {
  const chat = getActiveChat();
  const idx = chat.messages.findIndex(m => m.id === id);
  if (idx >= 0) {
    chat.messages[idx] = { ...chat.messages[idx], ...updates };
    renderMessages();
  }
}

function escapeHTML(html) {
  const div = document.createElement('div');
  div.textContent = html;
  return div.innerHTML;
}

function formatDay(date) {
  return date.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

function createAvatar(role) {
  const el = document.createElement('div');
  el.className = 'avatar';
  el.textContent = role === 'user' ? '🙂' : '🤖';
  return el;
}

// Markdown rendering helpers
function renderMarkdownToHtml(text) {
  if (window.marked) {
    marked.setOptions({
      breaks: true,
      highlight: function(code, lang) {
        if (window.Prism) {
          try {
            if (lang && Prism.languages[lang]) {
              return Prism.highlight(code, Prism.languages[lang], lang);
            }
          } catch {}
        }
        return code;
      }
    });
    return marked.parse(text || '');
  }
  return (text || '').replace(/\n/g, '<br>');
}

// Attachments state
let pendingAttachments = [];
function addAttachment(file, dataUrl) {
  pendingAttachments.push({ file, dataUrl });
  renderPendingAttachments();
}
function clearAttachments() {
  pendingAttachments = [];
  renderPendingAttachments();
}

function renderPendingAttachments() {
  // Ensure attachments container exists above textarea
  let container = document.querySelector('.attachments');
  if (!container) {
    container = document.createElement('div');
    container.className = 'attachments';
    const composerInner = document.querySelector('.composer-inner');
    composerInner.parentNode.insertBefore(container, composerInner);
  }
  container.innerHTML = '';
  pendingAttachments.forEach((att, idx) => {
    const pill = document.createElement('div');
    pill.className = 'attachment';
    if (att.dataUrl && att.file.type.startsWith('image/')) {
      const img = document.createElement('img');
      img.src = att.dataUrl;
      pill.appendChild(img);
    } else {
      const span = document.createElement('span');
      span.textContent = att.file.name;
      pill.appendChild(span);
    }
    const x = document.createElement('button');
    x.className = 'btn icon';
    x.textContent = '✕';
    x.title = 'Remove';
    x.addEventListener('click', () => {
      pendingAttachments.splice(idx, 1);
      renderPendingAttachments();
    });
    pill.appendChild(x);
    container.appendChild(pill);
  });
  container.style.display = pendingAttachments.length ? '' : 'none';
}

function handleFiles(files) {
  Array.from(files).forEach((file) => {
    if (file.type.startsWith('image/')) {
      const reader = new FileReader();
      reader.onload = () => addAttachment(file, reader.result);
      reader.readAsDataURL(file);
    } else {
      addAttachment(file, null);
    }
  });
}

// Modify message renderer to use markdown
function renderMessage(msg, options = {}) {
  const wrapper = document.createElement('article');
  wrapper.className = 'message ' + (msg.role === 'user' ? 'user' : 'assistant');
  if (options.continued) wrapper.classList.add('continued');
  wrapper.classList.add('appear');

  const avatar = createAvatar(msg.role);
  wrapper.appendChild(avatar);

  const meta = document.createElement('div');
  meta.className = 'meta';
  meta.innerHTML = `${msg.role === 'user' ? 'You' : 'Assistant'} • ${msg.createdAt.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})}`;
  wrapper.appendChild(meta);

  // reactions
  const reactions = document.createElement('div');
  reactions.className = 'reactions';
  ['👍','⭐️','👎'].forEach((emoji) => {
    const btn = document.createElement('button');
    btn.className = 'reaction';
    btn.textContent = emoji;
    btn.addEventListener('click', () => {
      btn.classList.toggle('active');
    });
    reactions.appendChild(btn);
  });

  if (msg.role === 'assistant' && toggleThoughtsEl.checked && msg.thoughts) {
    const thoughts = document.createElement('div');
    thoughts.className = 'thoughts';
    const label = document.createElement('div');
    label.className = 'label';
    label.innerHTML = `<span class="lock">🔒</span> Thoughts (concise summary & activity log)`;
    thoughts.appendChild(label);
    const tabs = document.createElement('div');
    tabs.className = 'tabs';
    const planBtn = document.createElement('button');
    planBtn.className = 'tab active';
    planBtn.textContent = 'Plan';
    const actBtn = document.createElement('button');
    actBtn.className = 'tab';
    actBtn.textContent = 'Activity';
    tabs.appendChild(planBtn);
    tabs.appendChild(actBtn);
    thoughts.appendChild(tabs);
    const content = document.createElement('div');
    content.className = 'content';
    const plan = document.createElement('div');
    plan.innerHTML = (msg.thoughts.plan || []).map(s => `• ${escapeHTML(s)}`).join('<br>');
    const activity = document.createElement('div');
    activity.style.display = 'none';
    activity.innerHTML = (msg.thoughts.activity || []).map(s => `• ${escapeHTML(s)}`).join('<br>');
    content.appendChild(plan);
    content.appendChild(activity);
    planBtn.onclick = () => { planBtn.classList.add('active'); actBtn.classList.remove('active'); plan.style.display = ''; activity.style.display = 'none'; };
    actBtn.onclick = () => { actBtn.classList.add('active'); planBtn.classList.remove('active'); activity.style.display = ''; plan.style.display = 'none'; };
    thoughts.appendChild(content);
    wrapper.appendChild(thoughts);
  }

  const answer = document.createElement('div');
  answer.className = 'answer';

  if (msg.streaming) {
    const typing = document.createElement('div');
    typing.className = 'typing';
    typing.innerHTML = '<span class="dot"></span><span class="dot"></span><span class="dot"></span>';
    answer.appendChild(typing);
  } else {
    const contentHtml = renderMarkdownToHtml(msg.content || '');
    const p = document.createElement('div');
    p.innerHTML = contentHtml;
    answer.appendChild(p);
  }

  wrapper.appendChild(answer);
  wrapper.appendChild(reactions);
  return wrapper;
}

function enhanceCodeBlocks(container) {
  const blocks = container.querySelectorAll('.code');
  blocks.forEach((block) => {
    if (block.querySelector('.copy-btn')) return;
    const btn = document.createElement('button');
    btn.className = 'copy-btn';
    btn.innerHTML = 'Copy';
    btn.addEventListener('click', async () => {
      const text = block.textContent || '';
      try {
        await navigator.clipboard.writeText(text);
        btn.textContent = 'Copied!';
        setTimeout(() => (btn.textContent = 'Copy'), 1200);
      } catch (e) {
        btn.textContent = 'Failed';
        setTimeout(() => (btn.textContent = 'Copy'), 1200);
      }
    });
    block.appendChild(btn);
  });
}

function attachMessageActions(container) {
  container.querySelectorAll('.message').forEach((msgEl) => {
    if (msgEl.querySelector('.msg-actions')) return;
    const actions = document.createElement('div');
    actions.className = 'msg-actions';
    actions.style.position = 'absolute';
    actions.style.top = '8px';
    actions.style.right = '8px';
    actions.style.display = 'inline-flex';
    actions.style.gap = '6px';

    const copyBtn = document.createElement('button');
    copyBtn.className = 'btn ghost icon';
    copyBtn.title = 'Copy message';
    copyBtn.textContent = '📋';
    copyBtn.addEventListener('click', async () => {
      const text = msgEl.innerText || '';
      await navigator.clipboard.writeText(text);
    });

    const deleteBtn = document.createElement('button');
    deleteBtn.className = 'btn ghost icon';
    deleteBtn.title = 'Delete message';
    deleteBtn.textContent = '🗑️';
    deleteBtn.addEventListener('click', () => {
      const chat = getActiveChat();
      const index = Array.from(chatEl.children).indexOf(msgEl);
      // account for separators by finding matching message index
      const domMessages = Array.from(chatEl.querySelectorAll('.message'));
      const realIndex = domMessages.indexOf(msgEl);
      if (realIndex >= 0) {
        chat.messages.splice(realIndex, 1);
        renderMessages();
      }
    });

    actions.appendChild(copyBtn);
    actions.appendChild(deleteBtn);
    msgEl.appendChild(actions);
  });
}

function getFilteredMessages(chat) {
  const q = (searchEl && searchEl.value.trim().toLowerCase()) || '';
  if (!q) return chat.messages;
  return chat.messages.filter((m) => (m.content || '').toLowerCase().includes(q));
}

function renderMessages() {
  const chat = getActiveChat();
  chatEl.innerHTML = '';

  let prev = null;
  const messages = getFilteredMessages(chat);
  messages.forEach((m, idx) => {
    const isFirstOfDay = !prev || formatDay(prev.createdAt) !== formatDay(m.createdAt);
    if (isFirstOfDay) {
      const sep = document.createElement('div');
      sep.className = 'day-separator';
      sep.textContent = formatDay(m.createdAt);
      chatEl.appendChild(sep);
    }

    const continued = prev && prev.role === m.role;
    chatEl.appendChild(renderMessage(m, { continued }));
    prev = m;
  });

  enhanceCodeBlocks(chatEl);
  attachMessageActions(chatEl);

  chatEl.scrollTop = chatEl.scrollHeight;
  toggleScrollButtonVisibility();
}

function simulateAssistantResponse(userText) {
  const assistantMsg = addMessage('assistant', '', {
    streaming: true,
    thoughts: {
      plan: [
        'Clarify the goal and outline a minimal solution.',
        'Separate Thoughts (summary/log) from Output for clarity.',
        'Return a concise, styled answer with optional details.',
      ],
      activity: [
        'Parsed input and extracted key intents.',
        'Rendered UI with Thoughts panel enabled.',
        'Streaming response content to the chat.',
      ],
    },
  });

  const full = `Here is your response for: "${userText}"\n\n- Designed to separate Thoughts from Output\n- Includes a clean, modern layout and a streaming effect\n\nYou can customize styles in styles.css and behaviors in app.js.`;
  let i = 0;
  const interval = setInterval(() => {
    i += 2;
    const partial = full.slice(0, i);
    updateMessage(assistantMsg.id, { content: partial });
    if (i >= full.length) {
      clearInterval(interval);
      updateMessage(assistantMsg.id, { streaming: false });
    }
  }, 20);
}

function initSeedMessages() {
  const chat = getActiveChat();
  if (chat.messages.length > 0) return;
  addMessage('assistant', 'Welcome! Ask me anything. Thoughts are shown as a concise summary and activity log (no private chain-of-thought).', {
    thoughts: {
      plan: [
        'Greet the user and invite a question.',
        'Explain the Thoughts vs Output separation briefly.',
      ],
      activity: [
        'Initialized chat session.',
      ],
    },
  });
}

function resizeTextareaToContent() {
  inputEl.style.height = 'auto';
  const maxHeight = 180; // px
  const newHeight = Math.min(inputEl.scrollHeight, maxHeight);
  inputEl.style.height = newHeight + 'px';
}

function shouldSendOnEnter(e) {
  const enterToSend = enterToSendEl ? enterToSendEl.checked : true;
  return enterToSend && e.key === 'Enter' && !e.shiftKey;
}

function toggleSidebarOnMobile() {
  if (!sidebarEl) return;
  const isOpen = sidebarEl.classList.toggle('open');
  if (appRoot) appRoot.classList.toggle('sidebar-open', isOpen);
}
function closeSidebarOnMobile() {
  if (!sidebarEl) return;
  sidebarEl.classList.remove('open');
  if (appRoot) appRoot.classList.remove('sidebar-open');
}

function createScrollToBottomButton() {
  const btn = document.createElement('button');
  btn.className = 'btn primary icon scroll-to-bottom';
  btn.innerHTML = '⬇';
  btn.title = 'Scroll to bottom';
  btn.style.display = 'none';
  btn.addEventListener('click', () => {
    chatEl.scrollTo({ top: chatEl.scrollHeight, behavior: 'smooth' });
  });
  document.body.appendChild(btn);
  return btn;
}

const scrollBtn = createScrollToBottomButton();

function toggleScrollButtonVisibility() {
  const threshold = 80; // px from bottom
  const distanceFromBottom = chatEl.scrollHeight - chatEl.clientHeight - chatEl.scrollTop;
  scrollBtn.style.display = distanceFromBottom > threshold ? 'inline-flex' : 'none';
}

// Hook up file actions
if (fileInputEl) fileInputEl.addEventListener('change', (e) => handleFiles(e.target.files));
const attachBtn = document.getElementById('attachment-btn');
if (attachBtn) attachBtn.addEventListener('click', () => fileInputEl && fileInputEl.click());

// Drag & drop on composer
const composerEl = document.querySelector('.composer-inner');
if (composerEl) {
  ['dragenter','dragover'].forEach(ev => composerEl.addEventListener(ev, (e) => { e.preventDefault(); composerEl.classList.add('drag'); }));
  ;['dragleave','drop'].forEach(ev => composerEl.addEventListener(ev, (e) => { e.preventDefault(); composerEl.classList.remove('drag'); }));
  composerEl.addEventListener('drop', (e) => { if (e.dataTransfer && e.dataTransfer.files) handleFiles(e.dataTransfer.files); });
}
// Paste images/files
inputEl.addEventListener('paste', (e) => {
  const items = e.clipboardData && e.clipboardData.items;
  if (!items) return;
  for (const it of items) {
    if (it.kind === 'file') {
      const file = it.getAsFile();
      if (file) handleFiles([file]);
    }
  }
});

// Override send click to include attachments summary
sendBtn.addEventListener('click', () => {
  const text = inputEl.value.trim();
  if (!text && pendingAttachments.length === 0) return;
  inputEl.value = '';
  resizeTextareaToContent();

  let content = text;
  if (pendingAttachments.length) {
    const names = pendingAttachments.map(a => a.file.name).join(', ');
    content += (content ? '\n\n' : '') + `Attachments: ${names}`;
  }
  clearAttachments();

  addMessage('user', content);
  simulateAssistantResponse(content);
});

inputEl.addEventListener('input', resizeTextareaToContent);
inputEl.addEventListener('keydown', (e) => {
  if (shouldSendOnEnter(e)) {
    e.preventDefault();
    sendBtn.click();
  }
});

chatEl.addEventListener('scroll', toggleScrollButtonVisibility, { passive: true });

if (toggleThoughtsEl) toggleThoughtsEl.addEventListener('change', () => renderMessages());
if (toggleThemeBtn) toggleThemeBtn.addEventListener('click', toggleTheme);
if (enterToSendEl) enterToSendEl.addEventListener('change', () => {});
if (toggleSidebarBtn) toggleSidebarBtn.addEventListener('click', toggleSidebarOnMobile);

newChatBtn.addEventListener('click', () => {
  const id = 'c' + (chats.length + 1);
  chats.unshift({ id, title: 'New chat', messages: [] });
  activeChatId = id;
  renderChatList();
  renderMessages();
  initSeedMessages();
  closeSidebarOnMobile();
});

(function boot() {
  const savedTheme = localStorage.getItem('theme');
  if (savedTheme) setTheme(savedTheme);
  renderChatList();
  renderMessages();
  initSeedMessages();
  resizeTextareaToContent();
})();

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeSidebarOnMobile();
});
document.addEventListener('click', (e) => {
  if (!sidebarEl || !sidebarEl.classList.contains('open')) return;
  const clickInsideSidebar = sidebarEl.contains(e.target);
  const clickedToggle = toggleSidebarBtn && toggleSidebarBtn.contains(e.target);
  if (!clickInsideSidebar && !clickedToggle) closeSidebarOnMobile();
});

// Suggestions chips behavior
if (suggestionsEl) {
  suggestionsEl.addEventListener('click', (e) => {
    const target = e.target.closest('[data-suggest]');
    if (!target) return;
    const text = target.getAttribute('data-suggest');
    if (!text) return;
    if (e.shiftKey) {
      inputEl.value = text;
      sendBtn.click();
    } else {
      const cur = inputEl.value.trim();
      inputEl.value = cur ? cur + '\n' + text : text;
      inputEl.focus();
      resizeTextareaToContent();
    }
  });
}

// Settings modal
function openSettings() {
  if (!settingsEl) return;
  settingsEl.classList.add('open');
  settingsEl.setAttribute('aria-hidden', 'false');
}
function closeSettings() {
  if (!settingsEl) return;
  settingsEl.classList.remove('open');
  settingsEl.setAttribute('aria-hidden', 'true');
}
if (settingsBtn) settingsBtn.addEventListener('click', openSettings);
if (settingsCloseBtn) settingsCloseBtn.addEventListener('click', closeSettings);
if (settingsEl) settingsEl.addEventListener('click', (e) => { if (e.target === settingsEl) closeSettings(); });

// Density persistence
(function initDensity() {
  const saved = localStorage.getItem('density') || 'comfortable';
  const compact = saved === 'compact';
  document.body.classList.toggle('compact', compact);
  if (densityToggleEl) densityToggleEl.checked = compact;
  if (densityToggleEl) densityToggleEl.addEventListener('change', () => {
    const isCompact = !!densityToggleEl.checked;
    document.body.classList.toggle('compact', isCompact);
    localStorage.setItem('density', isCompact ? 'compact' : 'comfortable');
  });
})();

// Theme controls
(function initThemeFromSettings() {
  const savedTheme = localStorage.getItem('theme') || 'light';
  setTheme(savedTheme);
  if (themeLightEl) themeLightEl.checked = savedTheme === 'light';
  if (themeDarkEl) themeDarkEl.checked = savedTheme === 'dark';
  if (themeAmoledEl) themeAmoledEl.checked = savedTheme === 'amoled';
  const radios = [themeLightEl, themeDarkEl, themeAmoledEl].filter(Boolean);
  radios.forEach((el) => el.addEventListener('change', () => {
    const theme = el.value;
    setTheme(theme);
  }));
})();

// Wallpaper toggle
(function initWallpaper() {
  const saved = localStorage.getItem('wallpaper') === 'true';
  if (wallpaperToggleEl) wallpaperToggleEl.checked = saved;
  document.body.classList.toggle('wallpaper-light', saved);
  if (wallpaperToggleEl) wallpaperToggleEl.addEventListener('change', () => {
    const on = !!wallpaperToggleEl.checked;
    document.body.classList.toggle('wallpaper-light', on);
    localStorage.setItem('wallpaper', on);
  });
})();

// Search events
if (searchEl) searchEl.addEventListener('input', () => renderMessages());

// Export actions
function exportAsJson() {
  const data = JSON.stringify(getActiveChat(), null, 2);
  const blob = new Blob([data], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `${getActiveChat().title || 'chat'}.json`;
  a.click();
  URL.revokeObjectURL(url);
}
function exportAsMarkdown() {
  const chat = getActiveChat();
  const lines = [];
  lines.push(`# ${chat.title || 'Chat export'}`);
  chat.messages.forEach((m) => {
    const who = m.role === 'user' ? 'You' : 'Assistant';
    lines.push(`\n**${who}** (${m.createdAt.toLocaleString()}):\n`);
    lines.push(m.content);
  });
  const blob = new Blob([lines.join('\n')], { type: 'text/markdown' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `${chat.title || 'chat'}.md`;
  a.click();
  URL.revokeObjectURL(url);
}
if (exportJsonBtn) exportJsonBtn.addEventListener('click', exportAsJson);
if (exportMdBtn) exportMdBtn.addEventListener('click', exportAsMarkdown);