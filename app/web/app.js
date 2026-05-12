const DOCUMENT_KIND_LABELS = {
  analytics: "Аналитика прошлых периодов",
  sources: "Релевантные источники",
};

const DOCUMENT_KIND_ORDER = ["analytics", "sources"];

const state = {
  authMode: "login",
  user: null,
  threads: [],
  activeThreadId: null,
  activeThread: null,
  documents: [],
  messages: [],
  conversionPollId: null,
  generationPollId: null,
};

function setResult(el, message, isError = false) {
  el.textContent = message;
  el.classList.toggle("error", isError);
}

async function readError(response) {
  try {
    const payload = await response.json();
    if (typeof payload?.detail === "string") {
      return payload.detail;
    }
    return JSON.stringify(payload, null, 2);
  } catch {
    return await response.text();
  }
}

async function apiFetch(url, options = {}) {
  const response = await fetch(url, options);
  if (response.status === 204) {
    return null;
  }
  if (!response.ok) {
    const error = new Error(await readError(response));
    error.status = response.status;
    throw error;
  }
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    return await response.json();
  }
  return null;
}

function stopPolling() {
  if (state.conversionPollId !== null) {
    clearTimeout(state.conversionPollId);
    state.conversionPollId = null;
  }
  if (state.generationPollId !== null) {
    clearTimeout(state.generationPollId);
    state.generationPollId = null;
  }
}

function documentKindLabel(kind) {
  return DOCUMENT_KIND_LABELS[kind] || kind;
}

function groupedDocuments() {
  const grouped = new Map(DOCUMENT_KIND_ORDER.map((kind) => [kind, []]));
  for (const documentItem of state.documents) {
    if (!grouped.has(documentItem.kind)) {
      grouped.set(documentItem.kind, []);
    }
    grouped.get(documentItem.kind).push(documentItem);
  }
  return grouped;
}

const authShell = document.getElementById("auth-shell");
const appShell = document.getElementById("app-shell");
const loginTab = document.getElementById("login-tab");
const registerTab = document.getElementById("register-tab");
const authForm = document.getElementById("auth-form");
const authEmail = document.getElementById("auth-email");
const authPassword = document.getElementById("auth-password");
const authSubmit = document.getElementById("auth-submit");
const authResult = document.getElementById("auth-result");

const userEmail = document.getElementById("user-email");
const logoutButton = document.getElementById("logout-button");
const createThreadButton = document.getElementById("create-thread-button");
const threadList = document.getElementById("thread-list");
const threadTitle = document.getElementById("thread-title");
const threadActions = document.getElementById("thread-actions");
const renameThreadButton = document.getElementById("rename-thread-button");
const deleteThreadButton = document.getElementById("delete-thread-button");
const threadEmpty = document.getElementById("thread-empty");
const threadDetail = document.getElementById("thread-detail");

const analyticsConvertForm = document.getElementById("analytics-convert-form");
const sourcesConvertForm = document.getElementById("sources-convert-form");
const analyticsFileInput = document.getElementById("analytics-files");
const sourcesFileInput = document.getElementById("sources-files");
const analyticsConvertSubmit = document.getElementById("analytics-convert-submit");
const sourcesConvertSubmit = document.getElementById("sources-convert-submit");
const convertResult = document.getElementById("convert-result");
const refreshDocumentsButton = document.getElementById("refresh-documents-button");
const documentsList = document.getElementById("documents-list");

const messagesList = document.getElementById("messages-list");
const generationForm = document.getElementById("generation-form");
const periodDescriptionInput = document.getElementById("period-description");
const generationSubmit = document.getElementById("generation-submit");
const generationResult = document.getElementById("generation-result");

function setAuthMode(mode) {
  state.authMode = mode;
  loginTab.classList.toggle("is-active", mode === "login");
  registerTab.classList.toggle("is-active", mode === "register");
  authSubmit.textContent = mode === "login" ? "Войти" : "Создать аккаунт";
  authPassword.autocomplete = mode === "login" ? "current-password" : "new-password";
  setResult(authResult, "");
}

function showAuth() {
  stopPolling();
  authShell.classList.remove("hidden");
  appShell.classList.add("hidden");
}

function showApp() {
  authShell.classList.add("hidden");
  appShell.classList.remove("hidden");
  userEmail.textContent = state.user?.email || "";
}

function renderThreads() {
  threadList.innerHTML = "";

  if (state.threads.length === 0) {
    const empty = document.createElement("div");
    empty.className = "list-empty";
    empty.textContent = "Пока нет тредов.";
    threadList.append(empty);
    return;
  }

  for (const thread of state.threads) {
    const item = document.createElement("div");
    item.className = "thread-item";

    const open = document.createElement("button");
    open.type = "button";
    open.className = "thread-open";
    if (thread.id === state.activeThreadId) {
      open.classList.add("is-active");
    }
    open.addEventListener("click", () => {
      selectThread(thread.id);
    });

    const title = document.createElement("span");
    title.className = "thread-item-title";
    title.textContent = thread.title;

    const actions = document.createElement("span");
    actions.className = "thread-item-actions";

    const rename = document.createElement("button");
    rename.type = "button";
    rename.className = "mini-button";
    rename.textContent = "✎";
    rename.title = "Переименовать";
    rename.addEventListener("click", (event) => {
      event.stopPropagation();
      renameThread(thread);
    });

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "mini-button danger";
    remove.textContent = "×";
    remove.title = "Удалить";
    remove.addEventListener("click", (event) => {
      event.stopPropagation();
      removeThread(thread);
    });

    actions.append(rename, remove);
    open.append(title);
    item.append(open, actions);
    threadList.append(item);
  }
}

function renderDocuments() {
  documentsList.innerHTML = "";
  if (!state.activeThreadId) {
    return;
  }

  if (state.documents.length === 0) {
    const empty = document.createElement("div");
    empty.className = "list-empty";
    empty.textContent = "Документы ещё не загружены.";
    documentsList.append(empty);
    return;
  }

  const grouped = groupedDocuments();
  for (const kind of grouped.keys()) {
    const group = document.createElement("section");
    group.className = "document-kind-group";

    const title = document.createElement("h3");
    title.className = "document-kind-title";
    title.textContent = documentKindLabel(kind);
    group.append(title);

    const items = grouped.get(kind) || [];
    if (items.length === 0) {
      const empty = document.createElement("div");
      empty.className = "list-empty";
      empty.textContent = "Пока пусто.";
      group.append(empty);
      documentsList.append(group);
      continue;
    }

    for (const documentItem of items) {
      const row = document.createElement("div");
      row.className = "list-row";

      const meta = document.createElement("div");
      meta.className = "list-row-meta";
      meta.innerHTML = `
        <strong>${documentItem.original_filename}</strong>
        <span>stem: ${documentItem.stem}</span>
      `;

      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "ghost-button";
      remove.textContent = "Удалить";
      remove.addEventListener("click", () => deleteDocument(documentItem.id));

      row.append(meta, remove);
      group.append(row);
    }

    documentsList.append(group);
  }
}

function renderMessages() {
  messagesList.innerHTML = "";
  if (!state.activeThreadId) {
    return;
  }

  if (state.messages.length === 0) {
    const empty = document.createElement("div");
    empty.className = "list-empty";
    empty.textContent = "История пока пуста.";
    messagesList.append(empty);
    return;
  }

  for (const message of state.messages) {
    const item = document.createElement("article");
    item.className = `message message-${message.role}`;

    const label = document.createElement("p");
    label.className = "message-role";
    label.textContent = message.role === "assistant" ? "Аналитика" : "Целевой период";

    const body = document.createElement("p");
    body.className = "message-content";
    body.textContent = message.content;

    item.append(label, body);
    messagesList.append(item);
  }

  messagesList.scrollTop = messagesList.scrollHeight;
}

function renderWorkspace() {
  const hasActiveThread = Boolean(state.activeThread);
  threadActions.classList.toggle("hidden", !hasActiveThread);
  threadEmpty.classList.toggle("hidden", hasActiveThread);
  threadDetail.classList.toggle("hidden", !hasActiveThread);

  if (!hasActiveThread) {
    threadTitle.textContent = "Выберите тред";
    renderDocuments();
    renderMessages();
    return;
  }

  threadTitle.textContent = state.activeThread.title;
  renderDocuments();
  renderMessages();
}

function setConversionButtonsDisabled(disabled) {
  analyticsConvertSubmit.disabled = disabled;
  sourcesConvertSubmit.disabled = disabled;
}

async function loadThreads(preferredThreadId = null) {
  state.threads = await apiFetch("/api/v1/threads");
  renderThreads();

  const nextThreadId =
    preferredThreadId && state.threads.some((thread) => thread.id === preferredThreadId)
      ? preferredThreadId
      : state.activeThreadId && state.threads.some((thread) => thread.id === state.activeThreadId)
        ? state.activeThreadId
        : state.threads[0]?.id || null;

  if (nextThreadId) {
    await selectThread(nextThreadId);
    return;
  }

  state.activeThreadId = null;
  state.activeThread = null;
  state.documents = [];
  state.messages = [];
  renderWorkspace();
}

async function selectThread(threadId) {
  stopPolling();
  const previousThreadId = state.activeThreadId;
  state.activeThreadId = threadId;
  renderThreads();

  const [thread, documents, messages] = await Promise.all([
    apiFetch(`/api/v1/threads/${encodeURIComponent(threadId)}`),
    apiFetch(`/api/v1/threads/${encodeURIComponent(threadId)}/documents`),
    apiFetch(`/api/v1/threads/${encodeURIComponent(threadId)}/messages`),
  ]);

  state.activeThread = thread;
  state.documents = documents;
  state.messages = messages;
  if (previousThreadId !== threadId) {
    setResult(convertResult, "");
    setResult(generationResult, "");
  }
  renderWorkspace();
}

async function refreshDocuments() {
  if (!state.activeThreadId) {
    return;
  }
  state.documents = await apiFetch(`/api/v1/threads/${encodeURIComponent(state.activeThreadId)}/documents`);
  renderDocuments();
}

async function refreshMessages() {
  if (!state.activeThreadId) {
    return;
  }
  state.messages = await apiFetch(`/api/v1/threads/${encodeURIComponent(state.activeThreadId)}/messages`);
  renderMessages();
}

async function renameThread(thread = state.activeThread) {
  if (!thread) {
    return;
  }
  const nextTitle = window.prompt("Новое имя треда", thread.title);
  if (nextTitle === null) {
    return;
  }

  try {
    const updated = await apiFetch(`/api/v1/threads/${encodeURIComponent(thread.id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: nextTitle }),
    });
    state.activeThread = updated;
    await loadThreads(updated.id);
  } catch (error) {
    setResult(convertResult, String(error.message || error), true);
  }
}

async function removeThread(thread = state.activeThread) {
  if (!thread) {
    return;
  }
  const confirmed = window.confirm(`Удалить тред "${thread.title}" вместе с документами и историей?`);
  if (!confirmed) {
    return;
  }

  try {
    await apiFetch(`/api/v1/threads/${encodeURIComponent(thread.id)}`, { method: "DELETE" });
    if (state.activeThreadId === thread.id) {
      state.activeThreadId = null;
      state.activeThread = null;
      state.documents = [];
      state.messages = [];
    }
    await loadThreads();
  } catch (error) {
    setResult(convertResult, String(error.message || error), true);
  }
}

async function deleteDocument(documentId) {
  if (!state.activeThreadId) {
    return;
  }
  const confirmed = window.confirm("Удалить документ из треда?");
  if (!confirmed) {
    return;
  }

  try {
    await apiFetch(
      `/api/v1/threads/${encodeURIComponent(state.activeThreadId)}/documents/${encodeURIComponent(documentId)}`,
      { method: "DELETE" },
    );
    await refreshDocuments();
    await loadThreads(state.activeThreadId);
  } catch (error) {
    setResult(convertResult, String(error.message || error), true);
  }
}

function scheduleConversionPolling(threadId, taskId, fileInput) {
  state.conversionPollId = window.setTimeout(() => {
    state.conversionPollId = null;
    pollConversionStatus(threadId, taskId, fileInput);
  }, 2000);
}

async function pollConversionStatus(threadId, taskId, fileInput) {
  try {
    const data = await apiFetch(
      `/api/v1/threads/${encodeURIComponent(threadId)}/conversions/${encodeURIComponent(taskId)}`,
    );
    if (data.status === "completed") {
      setResult(convertResult, JSON.stringify(data, null, 2));
      await refreshDocuments();
      await loadThreads(threadId);
      setConversionButtonsDisabled(false);
      fileInput.value = "";
      return;
    }
    if (data.status === "failed") {
      setResult(convertResult, JSON.stringify(data, null, 2), true);
      setConversionButtonsDisabled(false);
      return;
    }

    setResult(convertResult, `Конвертация ${taskId}: ${data.status}...`);
    scheduleConversionPolling(threadId, taskId, fileInput);
  } catch (error) {
    setResult(convertResult, String(error.message || error), true);
    setConversionButtonsDisabled(false);
  }
}

function scheduleGenerationPolling(threadId, taskId) {
  state.generationPollId = window.setTimeout(() => {
    state.generationPollId = null;
    pollGenerationStatus(threadId, taskId);
  }, 2000);
}

async function pollGenerationStatus(threadId, taskId) {
  try {
    const data = await apiFetch(
      `/api/v1/threads/${encodeURIComponent(threadId)}/generations/${encodeURIComponent(taskId)}`,
    );
    if (data.status === "completed") {
      setResult(generationResult, data.analysis_markdown || "<empty markdown>");
      await refreshMessages();
      await loadThreads(threadId);
      generationSubmit.disabled = false;
      periodDescriptionInput.value = "";
      return;
    }
    if (data.status === "failed") {
      setResult(generationResult, data.error || "Generation task failed.", true);
      await refreshMessages();
      generationSubmit.disabled = false;
      return;
    }

    setResult(generationResult, `Генерация ${taskId}: ${data.status}...`);
    scheduleGenerationPolling(threadId, taskId);
  } catch (error) {
    setResult(generationResult, String(error.message || error), true);
    generationSubmit.disabled = false;
  }
}

async function startConversion(documentKind, fileInput) {
  if (!state.activeThreadId) {
    setResult(convertResult, "Сначала выберите тред.", true);
    return;
  }

  const files = fileInput.files;
  if (!files || files.length === 0) {
    setResult(convertResult, "Выберите хотя бы один документ.", true);
    return;
  }

  const formData = new FormData();
  formData.append("document_kind", documentKind);
  for (const file of files) {
    formData.append("files", file);
  }

  setConversionButtonsDisabled(true);
  setResult(convertResult, "Создаю задачу конвертации...");

  try {
    const data = await apiFetch(
      `/api/v1/threads/${encodeURIComponent(state.activeThreadId)}/conversions`,
      {
        method: "POST",
        body: formData,
      },
    );
    setResult(convertResult, `Конвертация ${data.task_id} поставлена в очередь.`);
    await pollConversionStatus(state.activeThreadId, data.task_id, fileInput);
  } catch (error) {
    setResult(convertResult, String(error.message || error), true);
    setConversionButtonsDisabled(false);
  }
}

loginTab.addEventListener("click", () => setAuthMode("login"));
registerTab.addEventListener("click", () => setAuthMode("register"));

authForm.addEventListener("submit", async (event) => {
  event.preventDefault();

  authSubmit.disabled = true;
  setResult(authResult, state.authMode === "login" ? "Выполняю вход..." : "Создаю аккаунт...");

  try {
    const payload = {
      email: authEmail.value.trim(),
      password: authPassword.value,
    };
    const endpoint = state.authMode === "login" ? "/api/v1/auth/login" : "/api/v1/auth/register";
    state.user = await apiFetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    showApp();
    await loadThreads();
    authForm.reset();
  } catch (error) {
    setResult(authResult, String(error.message || error), true);
  } finally {
    authSubmit.disabled = false;
  }
});

logoutButton.addEventListener("click", async () => {
  try {
    await apiFetch("/api/v1/auth/logout", { method: "POST" });
  } finally {
    state.user = null;
    state.threads = [];
    state.activeThreadId = null;
    state.activeThread = null;
    state.documents = [];
    state.messages = [];
    renderThreads();
    renderWorkspace();
    showAuth();
  }
});

createThreadButton.addEventListener("click", async () => {
  const title = window.prompt("Имя нового треда", "");
  if (title === null) {
    return;
  }

  try {
    const created = await apiFetch("/api/v1/threads", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    });
    await loadThreads(created.id);
  } catch (error) {
    setResult(convertResult, String(error.message || error), true);
  }
});

renameThreadButton.addEventListener("click", () => renameThread());
deleteThreadButton.addEventListener("click", () => removeThread());
refreshDocumentsButton.addEventListener("click", () => refreshDocuments());

analyticsConvertForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  await startConversion("analytics", analyticsFileInput);
});

sourcesConvertForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  await startConversion("sources", sourcesFileInput);
});

generationForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!state.activeThreadId) {
    setResult(generationResult, "Сначала выберите тред.", true);
    return;
  }

  const periodDescription = periodDescriptionInput.value.trim();
  if (!periodDescription) {
    setResult(generationResult, "Введите целевой период.", true);
    return;
  }

  generationSubmit.disabled = true;
  setResult(generationResult, "Создаю задачу генерации...");

  try {
    const data = await apiFetch(
      `/api/v1/threads/${encodeURIComponent(state.activeThreadId)}/generations`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ period_description: periodDescription }),
      },
    );
    await refreshMessages();
    setResult(generationResult, `Генерация ${data.task_id} поставлена в очередь.`);
    await pollGenerationStatus(state.activeThreadId, data.task_id);
  } catch (error) {
    setResult(generationResult, String(error.message || error), true);
    generationSubmit.disabled = false;
  }
});

async function boot() {
  setAuthMode("login");

  try {
    state.user = await apiFetch("/api/v1/auth/me");
    showApp();
    await loadThreads();
  } catch (error) {
    if (error.status !== 401) {
      setResult(authResult, String(error.message || error), true);
    }
    showAuth();
  }
}

boot();
