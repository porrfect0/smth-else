"use strict";

const STATE_LABEL = {
  working: "работает",
  waiting: "ждёт ответа",
  done: "закончил",
  blocked: "заблокирован",
  idle: "свободен",
};
const STATE_ICON = {
  working: "application_hourglass-1",
  waiting: "msg_question-2",
  done: "check-1",
  blocked: "msg_error-2",
  idle: "computer-1",
  stale: "msg_warning-2",
};
const WAIT_LABEL = {
  owner_input: "ждёт вашего ответа",
  owner_forward: "ждёт пересылки",
  reviewer: "ждёт ревью",
  coordinator_review: "ждёт приёмки ведущим",
  resource: "ждёт телефон/сервер",
  model_limit: "закончился лимит модели",
};
const TODO_ICON = { forward: "envelope_closed-1", reply: "msg_question-2", blocked: "msg_error-2", limit: "msg_warning-2" };
const stateLabel = (a) => (a.state === "waiting" && WAIT_LABEL[a.waiting_for]) || STATE_LABEL[a.state] || a.state;
// Forwarded-but-unconfirmed reminders stay visible but no longer need the owner.
const needsAction = (t) => !t.done_at && !(t.kind === "forward" && t.forwarded_at);
const isForwardPending = (t) => t && !t.done_at && t.kind === "forward";
const isLocal = ["localhost", "127.0.0.1", "[::1]"].includes(location.hostname);

let data = null;
let tab = localStorage.getItem("ab.tab") || "*";
let selected = null; // {type: "todo"|"agent", id}
let showClosed = localStorage.getItem("ab.closed") === "1";
let soundOn = localStorage.getItem("ab.sound") !== null ? localStorage.getItem("ab.sound") === "1" : !isLocal;
let seenAlerts = null;
let online = true;

const $ = (sel) => document.querySelector(sel);
const icon = (name) => `icons/${name}.png`;
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function ago(iso) {
  const t = Date.parse(iso);
  if (!t) return "";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return "только что";
  if (s < 3600) return `${Math.floor(s / 60)} мин`;
  if (s < 86400) return `${Math.floor(s / 3600)} ч`;
  return new Date(t).toLocaleDateString("ru-RU", { day: "numeric", month: "short" });
}
const hhmm = (iso) => new Date(iso).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });

function isStale(a) {
  return a.state === "working" && Date.now() - Date.parse(a.updated) > data.config.stale_minutes * 60000;
}

function todoText(t) {
  const who = `<b>${esc(t.agent)}</b>`;
  if (t.kind === "forward") return `Переслать <b class="to">${esc(t.to)}</b> работу от ${who}`;
  if (t.kind === "reply") return `${who} ждёт вашего ответа`;
  if (t.kind === "limit") return `${who}: закончился лимит модели`;
  return `${who} заблокирован`;
}

function todoStatus(t) {
  if (t.kind === "forward") {
    if (t.acked_at) return "получено ✓";
    if (t.forwarded_at) return "переслано, ждём подтверждения";
    if (t.done_at) return "закрыто";
    return "<b>не переслано</b>";
  }
  return t.done_at ? "закрыто" : { reply: "<b>ждёт ответа</b>", limit: "<b>нужна новая сессия</b>", blocked: "<b>нужна помощь</b>" }[t.kind];
}

// --- rendering ------------------------------------------------------------------

function render() {
  if (!data) return;
  const agents = Object.values(data.agents).sort((a, b) =>
    a.project.localeCompare(b.project) || a.first_seen.localeCompare(b.first_seen));
  const projects = [...new Set(agents.map((a) => a.project))].sort();
  if (tab !== "*" && tab !== "log" && !projects.includes(tab)) tab = "*";

  renderTodos();

  const tabs = [["*", "Все агенты"], ...projects.map((p) => [p, p]), ["log", "Журнал"]];
  $("#tabs").innerHTML = tabs.map(([id, label]) =>
    `<li role="tab" data-tab="${esc(id)}" aria-selected="${id === tab}"><a href="#">${esc(label)}</a></li>`).join("");

  const showLog = tab === "log";
  $("#agent-table").classList.toggle("hidden", showLog);
  $("#log-table").classList.toggle("hidden", !showLog);

  if (showLog) {
    $("#log-table tbody").innerHTML = data.events.slice().reverse().map((e) => `
      <tr><td class="c-ico"><img src="${icon(STATE_ICON[e.state])}" alt=""></td>
      <td class="nowrap">${hhmm(e.time)} <span class="sub">${esc(ago(e.time))}</span></td>
      <td class="nowrap">${esc(e.agent)} <span class="sub">${esc(e.project)}</span></td>
      <td class="nowrap">${esc(stateLabel(e))}${e.to ? ` → ${esc(e.to)}` : ""}${e.ack ? ` (подтвердил ${esc(e.ack)})` : ""}</td>
      <td class="task opt" title="${esc(e.task)}">${esc(e.task)}</td></tr>`).join("");
    $("#agents-empty").classList.toggle("hidden", data.events.length > 0);
  } else {
    const list = agents.filter((a) => tab === "*" || a.project === tab);
    $("#agent-table tbody").innerHTML = list.map((a) => {
      const key = `${a.project}/${a.agent}`;
      const stale = isStale(a);
      const label = esc(stale ? `${stateLabel(a)}? активность не подтверждена ${ago(a.updated)}` : stateLabel(a));
      const sel = selected?.type === "agent" && selected.id === key ? " highlighted" : "";
      return `<tr class="${stale ? "stale" : ""}${sel}" data-agent="${esc(key)}">
        <td class="c-ico"><img src="${icon(stale ? STATE_ICON.stale : STATE_ICON[a.state])}" alt=""></td>
        <td class="nowrap"><b>${esc(a.agent)}</b><span class="sub mob">${esc(a.project)}</span></td>
        <td class="nowrap opt">${esc(a.project)}</td>
        <td class="nowrap state">${label}${a.state === "done" && a.to ? ` → ${esc(a.to)}` : ""}</td>
        <td class="task opt" title="${esc(a.task)}">${esc(a.task)}</td>
        <td class="nowrap">${esc(ago(a.updated))}</td></tr>`;
    }).join("");
    $("#agents-empty").classList.toggle("hidden", list.length > 0);
  }

  const count = (s) => agents.filter((a) => a.state === s).length;
  const open = data.todos.filter(needsAction).length;
  $("#sb-main").textContent = online ? (open ? `Нужно сделать: ${open}` : "Всё под контролем") : "Нет связи с Agent Board на ПК";
  $("#sb-counts").textContent = `Агентов: ${agents.length} · работают: ${count("working")} · ждут: ${count("waiting")}`;
  $("#sb-time").textContent = `Обновлено ${new Date().toLocaleTimeString("ru-RU")}`;
  const title = open ? `(${open}) Agent Board` : "Agent Board";
  if (document.title !== title) {
    document.title = title;
    native()?.set_title(title);
  }

  $("#sound-check").classList.toggle("on", soundOn);
  $("#notify-check").classList.toggle("on", !!data.config.notify);
  $("#closed-check").classList.toggle("on", showClosed);
  $("#tb-sound").src = icon(soundOn ? "loudspeaker_rays-0" : "loudspeaker_muted-0");
  applySelection();
}

// Selection only toggles classes so rows are not replaced between the clicks of a double-click.
function applySelection() {
  document.querySelectorAll("tr[data-todo], tr[data-agent]").forEach((tr) => {
    const on = selected && (selected.type === "todo" ? tr.dataset.todo === String(selected.id) : tr.dataset.agent === selected.id);
    tr.classList.toggle("highlighted", !!on);
  });
  const todo = selected?.type === "todo" ? data.todos.find((t) => t.id === selected.id) : null;
  $("#tb-copy").disabled = !todo?.message;
  $("#tb-forwarded").disabled = !(isForwardPending(todo) && !todo.forwarded_at);
  $("#tb-done").disabled = !(todo && !todo.done_at);
  $("#tb-details").disabled = !selected;
  $("#tb-forget").disabled = selected?.type !== "agent";
}

function renderTodos() {
  const todos = data.todos
    .filter((t) => showClosed || !t.done_at)
    .sort((a, b) => (needsAction(b) - needsAction(a)) || (!!a.done_at - !!b.done_at) || b.created.localeCompare(a.created));
  const open = data.todos.filter(needsAction).length;
  $("#todo-legend").textContent = open ? `Нужно сделать (${open})` : "Нужно сделать";
  $("#todo-table tbody").innerHTML = todos.map((t) => {
    const sel = selected?.type === "todo" && selected.id === t.id ? " highlighted" : "";
    const cls = t.done_at ? "closed" : needsAction(t) ? "" : "pending";
    const ico = t.kind === "forward" && t.forwarded_at ? "envelope_open_sheet-1" : TODO_ICON[t.kind];
    return `<tr class="${cls}${sel}" data-todo="${t.id}">
      <td class="c-ico"><img src="${icon(ico)}" alt=""></td>
      <td>${todoText(t)}<span class="sub mob"> (${esc(t.project)})</span>${t.task ? `: <span class="sub">${esc(t.task)}</span>` : ""}</td>
      <td class="nowrap opt">${esc(t.project)}</td>
      <td class="nowrap status">${todoStatus(t)}</td>
      <td class="nowrap opt">${esc(ago(t.created))}</td></tr>`;
  }).join("");
  $("#todo-table").classList.toggle("hidden", todos.length === 0);
  $("#todo-empty").classList.toggle("hidden", todos.length > 0);
}

// --- alerts on this device ------------------------------------------------------

let audioCtx = null;
function chime() {
  try {
    audioCtx = audioCtx || new AudioContext();
    const t0 = audioCtx.currentTime;
    [[880, 0], [1318.5, 0.12]].forEach(([f, dt]) => {
      const o = audioCtx.createOscillator();
      const g = audioCtx.createGain();
      o.type = "triangle";
      o.frequency.value = f;
      g.gain.setValueAtTime(0.0001, t0 + dt);
      g.gain.exponentialRampToValueAtTime(0.3, t0 + dt + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, t0 + dt + 0.5);
      o.connect(g).connect(audioCtx.destination);
      o.start(t0 + dt);
      o.stop(t0 + dt + 0.55);
    });
  } catch { /* audio not allowed until the first tap */ }
  navigator.vibrate?.([120, 60, 120]);
}
document.addEventListener("pointerdown", () => { if (audioCtx?.state === "suspended") audioCtx.resume(); }, { passive: true });

function checkAlerts() {
  const ids = new Set(data.todos.filter(needsAction).map((t) => `${t.id}:${t.created}`));
  if (seenAlerts && soundOn && [...ids].some((id) => !seenAlerts.has(id))) chime();
  seenAlerts = ids;
}

// --- data -----------------------------------------------------------------------

async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`${r.status}`);
  return r.json();
}

let lastVersion = -1;
async function poll() {
  try {
    const next = await api("/api/state");
    online = true;
    if (next.version !== lastVersion || !data) {
      data = next;
      lastVersion = next.version;
      checkAlerts();
    }
  } catch {
    online = false;
  }
  render();
}

async function act(path, body) {
  try { await api(path, body); } catch (e) { alertBox("Ошибка", `Не получилось: ${e.message}`, "msg_error-0"); }
  await poll();
}

// --- dialogs --------------------------------------------------------------------

function dialog(title, html, onMount) {
  const layer = $("#modal-layer");
  const node = $("#tpl-dialog").content.firstElementChild.cloneNode(true);
  node.querySelector(".title-bar-text").textContent = title;
  node.querySelector(".window-body").innerHTML = html;
  const close = () => { layer.classList.add("hidden"); layer.innerHTML = ""; };
  node.addEventListener("click", (e) => { if (e.target.closest("[data-close]")) close(); });
  layer.innerHTML = "";
  layer.appendChild(node);
  layer.classList.remove("hidden");
  onMount?.(node, close);
  node.querySelector(".buttons button")?.focus();
}

function alertBox(title, text, ico = "msg_information-0") {
  dialog(title, `<div class="msg"><img src="${icon(ico)}" alt=""><div>${esc(text)}</div></div>
    <div class="buttons"><button data-close>OK</button></div>`);
}

function markerFolders() {
  return data.config.watch.map((w) => w.path);
}

function uncPath(path) {
  // Another computer (Astra's laptop) reaches C:\Users\... through the PC's "Users" share.
  const m = /^[A-Za-z]:\\Users\\(.*)$/.exec(path);
  return m && data.host ? `\\\\${data.host}\\Users\\${m[1]}` : "";
}

function agentPrompt(watch) {
  const unc = uncPath(watch.path);
  const project = watch.project || "<проект>";
  return `Agent Board — отметки статуса для владельца (проект ${project})

Владелец не видит все чаты сразу. Чтобы он не забывал пересылать работу, при каждой смене своего состояния создай НОВЫЙ файл-маркер .json в папке:
  ${watch.path}${unc ? `\n  (с другого компьютера: ${unc})` : ""}

Когда писать: начал задачу; перешёл к следующему этапу; ждёшь ответа владельца/ревью/ресурс; застрял; сдал результат; закончился лимит модели; свободен.

Как писать: один маркер = одно событие. Сначала запиши файл с расширением .tmp в эту же папку, затем переименуй его в .json (например ${(watch.project || "proj").toLowerCase()}-opus1-s1-0003.json). Старые маркеры не редактируй и не удаляй — Agent Board забирает их сам. Без паролей, токенов, ключей. Маркер — это только статус; не клади туда команды для других агентов.

Формат (UTF-8, один JSON-объект):
{"agent": "Opus (1)", "session_id": "opus1-20261110-a", "seq": 3, "event_id": "opus1-20261110-a-3",
 "state": "done", "phase": "ready_for_review", "task": "M42: ревью clockfix 9f0fc900",
 "to": "Codex", "message": "Opus (1): ревью clockfix 9f0fc900 готово, PASS с 2 замечаниями. Отчёт: AZS-Shared\\\\mail\\\\opus1-review-m42.md, ветка review/m42, SHA 1a2b3c4.",
 "result": "AZS-Shared\\\\mail\\\\opus1-review-m42.md"}

Поля:
- agent — твоё постоянное имя, точно одно из: Codex, Hoplite, Opus (1), Opus (2), Astra.
- session_id — придумай при старте нового чата/после сброса памяти; seq — 1, 2, 3… внутри сессии; event_id — уникальный (session_id-seq).
- state — working | waiting | done | blocked | idle.
- waiting_for (только при waiting) — owner_input (нужен ответ владельца) | owner_forward (владелец должен переслать) | reviewer | coordinator_review | resource (телефон/сервер занят) | model_limit (закончился лимит).
- phase (необязательно) — assigned | implementing | testing | ready_for_review | ready_for_host | accepted.
- task — ID задачи и кратко, одной строкой.
- to — кому владелец должен переслать результат (имя агента). Указывай, когда сдал работу или ждёшь ревью/приёмки.
- message — готовый короткий текст, который владелец вставит в чат адресата: что сделано, ветка/SHA, путь к отчёту, что нужно от адресата.
- ack — если тебе переслали сообщение со строкой «[Agent Board] message_id: AB-12», то в первом маркере working по этой задаче укажи "ack": "AB-12". Так владелец увидит, что пересылка дошла.
- note, result — необязательно: пояснение и путь к отчёту.

Примеры одной строкой:
{"agent":"Hoplite","session_id":"hop-m41-a","seq":1,"event_id":"hop-m41-a-1","state":"working","phase":"assigned","task":"M41: офлайн-сверка O6","ack":"AB-7"}
{"agent":"Opus (2)","session_id":"o2-v7-b","seq":5,"event_id":"o2-v7-b-5","state":"waiting","waiting_for":"owner_input","task":"v7: откатывать PR #41 или чинить?"}
{"agent":"Astra","session_id":"astra-1110","seq":2,"event_id":"astra-1110-2","state":"waiting","waiting_for":"model_limit","task":"Ревью clockfix","note":"лимит до 03:00"}`;
}

function forwardText(t) {
  if (t.kind !== "forward") return t.message;
  return `${t.message}\n\n[Agent Board] message_id: ${t.message_id}. Начав работу по этому сообщению, укажи в маркере "ack": "${t.message_id}".`;
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // Plain-http pages on the phone have no Clipboard API.
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    ta.remove();
    return ok;
  }
}

const ACTIONS = {
  about() {
    dialog("О программе", `<div class="msg"><img src="${icon("network_normal_two_pcs-0")}" alt="">
      <div><b>Agent Board</b><br>Кто из агентов работает, кто ждёт ответа и что нужно переслать.<br><br>
      Агенты появляются сами после первого маркера. Папки: ${esc(markerFolders().join("; "))}</div></div>
      <div class="buttons"><button data-close>OK</button></div>`);
  },
  howto() {
    const watches = data.config.watch;
    const tabs = watches.map((w, i) => `<li role="tab" data-howto="${i}" aria-selected="${i === 0}"><a href="#">${esc(w.project || w.path)}</a></li>`).join("");
    dialog("Инструкция для агентов", `<p>Отправьте этот текст каждому агенту проекта один раз (или попросите ведущего разослать). У каждого проекта своя папка.</p>
      <menu role="tablist">${tabs}</menu>
      <div class="window" role="tabpanel"><div class="window-body"><textarea readonly id="prompt-text"></textarea></div></div>
      <div class="buttons"><button id="copy-prompt">Копировать</button><button data-close>Закрыть</button></div>`,
    (node) => {
      const ta = node.querySelector("#prompt-text");
      const show = (i) => {
        ta.value = agentPrompt(watches[i]);
        node.querySelectorAll("[data-howto]").forEach((li) => li.setAttribute("aria-selected", li.dataset.howto === String(i)));
      };
      show(0);
      node.addEventListener("click", (e) => {
        const li = e.target.closest("[data-howto]");
        if (li) { e.preventDefault(); e.stopPropagation(); show(Number(li.dataset.howto)); }
      });
      node.querySelector("#copy-prompt").onclick = async (e) => {
        e.target.textContent = (await copyText(ta.value)) ? "Скопировано" : "Не удалось";
      };
    });
  },
  async pair() {
    const p = await api("/api/pairing");
    dialog("Подключить телефон", `<div class="msg"><img src="${icon("world-0")}" alt=""><div>
      <p>Телефон и ПК должны быть в одной Wi-Fi сети. Откройте на телефоне эту ссылку (например, отправьте её себе в мессенджер):</p>
      <p class="select-all">${esc(p.url)}</p>
      <p>Ссылка содержит ключ доступа — не пересылайте её другим. На телефоне коснитесь страницы один раз, чтобы разрешить звук.</p>
      <p>Если страница не открывается, разрешите Python в брандмауэре Windows для частной сети.</p></div></div>
      <div class="buttons"><button data-close>OK</button></div>`);
  },
  folders() {
    const rows = data.config.watch.map((w, i) => `<div class="field-row">
      <input type="text" data-i="${i}" data-f="path" value="${esc(w.path)}" style="flex:3">
      <input type="text" data-i="${i}" data-f="project" value="${esc(w.project)}" placeholder="проект" style="flex:1">
      <button data-remove="${i}">Удалить</button></div>`).join("");
    dialog("Папки с маркерами", `<p>Agent Board следит за этими папками. Если агенту нельзя писать вне папки проекта, добавьте папку внутри проекта (например <code>D:\\AZS\\.agent-board</code>) и укажите проект.</p>
      <div id="watch-rows">${rows}</div>
      <div class="field-row"><button id="add-watch">Добавить папку</button></div>
      <div class="buttons"><button id="save-watch">OK</button><button data-close>Отмена</button></div>`,
    (node, close) => {
      const collect = () => {
        const watch = [];
        node.querySelectorAll("#watch-rows .field-row").forEach((row) => {
          const [path, project] = row.querySelectorAll("input");
          if (path.value.trim()) watch.push({ path: path.value.trim(), project: project.value.trim() });
        });
        return watch;
      };
      node.addEventListener("click", (e) => {
        const rm = e.target.closest("[data-remove]");
        if (rm) rm.closest(".field-row").remove();
      });
      node.querySelector("#add-watch").onclick = () => {
        node.querySelector("#watch-rows").insertAdjacentHTML("beforeend", `<div class="field-row">
          <input type="text" placeholder="C:\\путь\\к\\папке" style="flex:3"><input type="text" placeholder="проект" style="flex:1">
          <button data-remove>Удалить</button></div>`);
      };
      node.querySelector("#save-watch").onclick = async () => { close(); await act("/api/config", { watch: collect() }); };
    });
  },
  details() {
    if (!selected) return;
    let item, history;
    if (selected.type === "todo") {
      item = data.todos.find((t) => t.id === selected.id);
    } else {
      item = data.agents[selected.id];
    }
    if (!item) return;
    history = data.events.filter((e) => e.project === item.project && e.agent === item.agent).slice(-8).reverse();
    const kv = (k, v) => (v ? `<dt>${k}</dt><dd>${esc(v)}</dd>` : "");
    const when = (iso) => iso && `${hhmm(iso)}, ${ago(iso)}`;
    const isTodo = selected.type === "todo";
    const head = isTodo
      ? `${kv("Задача", item.task)}${kv("Кому переслать", item.to)}${kv("Заметка", item.note)}${kv("Результат", item.result)}
         ${kv("message_id", item.kind === "forward" && item.message_id)}${kv("Переслано", when(item.forwarded_at))}${kv("Подтверждено", when(item.acked_at))}`
      : `${kv("Состояние", stateLabel(item))}${kv("Этап", item.phase)}${kv("Задача", item.task)}${kv("Кому переслать", item.to)}
         ${kv("Заметка", item.note)}${kv("Результат", item.result)}${kv("Сессия", item.session)}${kv("Обновлено", when(item.updated))}`;
    const msg = isTodo && item.message ? `<p>Текст для пересылки:</p><textarea readonly class="msg-text"></textarea>` : "";
    dialog(`${item.agent} — ${item.project}`, `<dl class="kv">${head}</dl>${msg}
      <div class="sunken-panel" style="max-height:160px"><table><thead><tr><th>Время</th><th>Событие</th><th>Задача</th></tr></thead><tbody>
      ${history.map((e) => `<tr><td class="nowrap">${hhmm(e.time)}</td><td class="nowrap">${esc(stateLabel(e))}${e.to ? ` → ${esc(e.to)}` : ""}</td><td>${esc(e.task)}</td></tr>`).join("")}
      </tbody></table></div>
      <div class="buttons">${msg ? `<button data-action="copy-message">Копировать</button>` : ""}<button data-close>OK</button></div>`,
    (node) => { const ta = node.querySelector(".msg-text"); if (ta) ta.value = forwardText(item); });
  },
  async "copy-message"(e) {
    const todo = selected?.type === "todo" && data.todos.find((t) => t.id === selected.id);
    if (!todo?.message) return;
    const ok = await copyText(forwardText(todo));
    const btn = e?.target?.closest("button");
    if (btn && ok) {
      const label = btn.querySelector("span") || btn;
      const old = label.textContent;
      label.textContent = "Скопировано";
      setTimeout(() => { label.textContent = old; }, 1500);
    } else if (!ok) {
      alertBox("Копирование", "Браузер не дал скопировать. Откройте «Подробно» и выделите текст вручную.", "msg_warning-2");
    }
  },
  "mark-forwarded"() {
    if (selected?.type === "todo") act(`/api/todo/${selected.id}/forwarded`, {});
  },
  "done-selected"() {
    if (selected?.type === "todo") act(`/api/todo/${selected.id}/done`, {});
  },
  forget() {
    if (selected?.type !== "agent") return;
    const a = data.agents[selected.id];
    dialog("Убрать агента", `<div class="msg"><img src="${icon("msg_question-0")}" alt="">
      <div>Убрать «${esc(a.agent)}» (${esc(a.project)}) из списка? Он вернётся после следующего маркера.</div></div>
      <div class="buttons"><button id="yes">Да</button><button data-close>Нет</button></div>`,
    (node, close) => {
      node.querySelector("#yes").onclick = () => { close(); selected = null; act("/api/agent/forget", { project: a.project, agent: a.agent }); };
    });
  },
  "clear-done"() {
    showClosed = false;
    localStorage.setItem("ab.closed", "0");
    render();
  },
  "toggle-sound"() {
    soundOn = !soundOn;
    localStorage.setItem("ab.sound", soundOn ? "1" : "0");
    if (soundOn) chime();
    render();
  },
  "toggle-notify"() { act("/api/config", { notify: !data.config.notify }); },
  "toggle-closed"() {
    showClosed = !showClosed;
    localStorage.setItem("ab.closed", showClosed ? "1" : "0");
    render();
  },
};

// --- events ---------------------------------------------------------------------

document.addEventListener("click", (e) => {
  const menu = e.target.closest("[data-menu]");
  document.querySelectorAll(".menu.open").forEach((m) => { if (m !== menu) m.classList.remove("open"); });
  if (menu && e.target.closest(".menu-label")) { menu.classList.toggle("open"); return; }
  if (menu) menu.classList.remove("open");

  const action = e.target.closest("[data-action]");
  if (action && data) { e.preventDefault(); ACTIONS[action.dataset.action]?.(e); return; }

  const t = e.target.closest("[data-tab]");
  if (t) {
    e.preventDefault();
    tab = t.dataset.tab;
    localStorage.setItem("ab.tab", tab);
    render();
    return;
  }
  const todoRow = e.target.closest("tr[data-todo]");
  const agentRow = e.target.closest("tr[data-agent]");
  if (todoRow) selected = { type: "todo", id: Number(todoRow.dataset.todo) };
  else if (agentRow) selected = { type: "agent", id: agentRow.dataset.agent };
  if (todoRow || agentRow) applySelection();
});
document.addEventListener("dblclick", (e) => {
  if (e.target.closest("#title-text")) { ACTIONS["win-max"](); return; }
  if (selected && e.target.closest("tr[data-todo], tr[data-agent]")) ACTIONS.details();
});
document.addEventListener("mouseover", (e) => {
  // Win98 menus: once one is open, hovering another title opens it.
  const menu = e.target.closest("[data-menu]");
  if (menu && document.querySelector(".menu.open") && !menu.classList.contains("open")) {
    document.querySelectorAll(".menu.open").forEach((m) => m.classList.remove("open"));
    menu.classList.add("open");
  }
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { $("#modal-layer").classList.add("hidden"); $("#modal-layer").innerHTML = ""; }
  if (e.key === "Delete" && selected?.type === "todo") ACTIONS["done-selected"]();
});

// --- frameless desktop window (launch.pyw opens the page with #native) -----------

const isNative = location.hash.includes("native");
document.body.classList.toggle("native", isNative);
const native = () => (isNative && window.pywebview?.api) || null;

Object.assign(ACTIONS, {
  "win-min"() { native()?.minimize(); },
  async "win-max"() {
    const api = native();
    if (!api) return;
    const maximized = await api.toggle_maximize();
    $("#win-max").setAttribute("aria-label", maximized ? "Restore" : "Maximize");
    document.body.classList.toggle("maximized", !!maximized);
  },
  "win-close"() { native()?.close(); },
});

$("#resize-grip").addEventListener("pointerdown", (e) => {
  const api = native();
  if (!api || document.body.classList.contains("maximized")) return;
  e.preventDefault();
  const grip = e.currentTarget;
  grip.setPointerCapture(e.pointerId);
  const start = { x: e.screenX, y: e.screenY, w: window.innerWidth, h: window.innerHeight };
  let pending = null;
  let busy = false;
  const flush = async () => {
    if (busy || !pending) return;
    busy = true;
    const size = pending;
    pending = null;
    try { await api.resize(size.w, size.h); } finally { busy = false; flush(); }
  };
  const move = (ev) => {
    pending = { w: start.w + ev.screenX - start.x, h: start.h + ev.screenY - start.y };
    flush();
  };
  const up = () => {
    grip.removeEventListener("pointermove", move);
    grip.removeEventListener("pointerup", up);
  };
  grip.addEventListener("pointermove", move);
  grip.addEventListener("pointerup", up);
});

poll();
setInterval(poll, 2000);
