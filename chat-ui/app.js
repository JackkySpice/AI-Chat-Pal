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

let chats = [
  { id: 'c1', title: 'Welcome', messages: [] }
];
let activeChatId = 'c1';

function setTheme(theme) {
  if (theme === 'dark') document.body.classList.add('dark');
  else document.body.classList.remove('dark');
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

function renderMessage(msg) {
  const wrapper = document.createElement('article');
  wrapper.className = 'message ' + (msg.role === 'user' ? 'user' : 'assistant');

  const meta = document.createElement('div');
  meta.className = 'meta';
  meta.innerHTML = `${msg.role === 'user' ? 'You' : 'Assistant'} • ${msg.createdAt.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})}`;
  wrapper.appendChild(meta);

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

    planBtn.onclick = () => {
      planBtn.classList.add('active');
      actBtn.classList.remove('active');
      plan.style.display = '';
      activity.style.display = 'none';
    };
    actBtn.onclick = () => {
      actBtn.classList.add('active');
      planBtn.classList.remove('active');
      activity.style.display = '';
      plan.style.display = 'none';
    };

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
    const p = document.createElement('div');
    p.innerHTML = msg.content
      .split('\n')
      .map(line => line.startsWith('```') ? `<div class="code">${escapeHTML(line.replace(/```/g, ''))}</div>` : `<p>${escapeHTML(line)}</p>`)
      .join('');
    answer.appendChild(p);
  }

  wrapper.appendChild(answer);
  return wrapper;
}

function renderMessages() {
  const chat = getActiveChat();
  chatEl.innerHTML = '';
  chat.messages.forEach((m) => chatEl.appendChild(renderMessage(m)));
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

sendBtn.addEventListener('click', () => {
  const text = inputEl.value.trim();
  if (!text) return;
  inputEl.value = '';
  resizeTextareaToContent();
  addMessage('user', text);
  simulateAssistantResponse(text);
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