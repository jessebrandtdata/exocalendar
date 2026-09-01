"use strict";

/* ============================== state ==================================== */

const state = {
  view: localStorage.getItem("view") || "month",
  cursor: new Date(),
  calendars: [],
  occs: [],
  hidden: new Set(JSON.parse(localStorage.getItem("hiddenCals") || "[]")),
  editing: null, // {occ} or {defaults}
};

const HOUR_PX = 48;
const SNAP_MIN = 15;
const TZID = Intl.DateTimeFormat().resolvedOptions().timeZone;
const WEEKDAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"];
const $ = (sel) => document.querySelector(sel);

/* ============================ date helpers =============================== */

const pad = (n) => String(n).padStart(2, "0");
const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;

function toISO(d) {
  const off = -d.getTimezoneOffset();
  const sign = off >= 0 ? "+" : "-";
  const a = Math.abs(off);
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
    `T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}` +
    `${sign}${pad(Math.floor(a / 60))}:${pad(a % 60)}`
  );
}

function parseDate(s) {
  // all-day "2026-06-01" parses as LOCAL midnight (not UTC)
  if (/^\d{4}-\d{2}-\d{2}$/.test(s)) {
    const [y, m, d] = s.split("-").map(Number);
    return new Date(y, m - 1, d);
  }
  return new Date(s);
}

const startOfDay = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate());
const addDays = (d, n) => {
  const out = new Date(d);
  out.setDate(out.getDate() + n);
  return out;
};
const addMinutes = (d, n) => new Date(d.getTime() + n * 60000);
const sameDay = (a, b) => ymd(a) === ymd(b);
const startOfWeek = (d) => addDays(startOfDay(d), -((d.getDay() + 6) % 7)); // Monday

function fmtTime(d) {
  return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}
function fmtMonthYear(d) {
  return d.toLocaleDateString([], { month: "long", year: "numeric" });
}

/* ============================== api ====================================== */

async function api(method, path, body, raw = false) {
  const opts = { method, credentials: "same-origin", headers: {} };
  if (body !== undefined) {
    opts.body = raw ? body : JSON.stringify(body);
    if (!raw) opts.headers["Content-Type"] = "application/json";
  }
  const resp = await fetch(path, opts);
  if (resp.status === 204) return null;
  const isJson = (resp.headers.get("Content-Type") || "").includes("json");
  const data = isJson ? await resp.json() : await resp.text();
  if (!resp.ok) throw new Error(data && data.error ? data.error : `HTTP ${resp.status}`);
  return data;
}

function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(el._t);
  el._t = setTimeout(() => (el.hidden = true), 3500);
}

/* ============================ data loading =============================== */

function visibleRange() {
  const c = state.cursor;
  if (state.view === "month") {
    const first = new Date(c.getFullYear(), c.getMonth(), 1);
    const gridStart = startOfWeek(first);
    return [gridStart, addDays(gridStart, 42)];
  }
  if (state.view === "week") {
    const s = startOfWeek(c);
    return [s, addDays(s, 7)];
  }
  return [startOfDay(c), addDays(startOfDay(c), 1)];
}

async function refresh() {
  const [start, end] = visibleRange();
  const shown = state.calendars.filter((c) => !state.hidden.has(c.id)).map((c) => c.id);
  try {
    state.occs = shown.length
      ? await api(
          "GET",
          `/api/occurrences?start=${encodeURIComponent(toISO(start))}` +
            `&end=${encodeURIComponent(toISO(end))}&calendars=${shown.join(",")}`
        )
      : [];
  } catch (err) {
    toast(`Could not load events: ${err.message}`);
    state.occs = [];
  }
  render();
}

async function loadCalendars() {
  state.calendars = await api("GET", "/api/calendars");
  if (!state.calendars.length) {
    const cal = await api("POST", "/api/calendars", { displayname: "Personal" });
    state.calendars = [cal];
  }
  renderSidebar();
}

/* ============================= rendering ================================= */

function render() {
  const label = $("#period-label");
  const c = state.cursor;
  document.querySelectorAll("#view-switch button").forEach((b) => {
    b.setAttribute("aria-selected", b.dataset.view === state.view ? "true" : "false");
  });
  if (state.view === "month") {
    label.textContent = fmtMonthYear(c);
    renderMonth();
  } else if (state.view === "week") {
    const s = startOfWeek(c);
    const e = addDays(s, 6);
    label.textContent =
      s.getMonth() === e.getMonth()
        ? fmtMonthYear(s)
        : `${s.toLocaleDateString([], { month: "short" })} – ${e.toLocaleDateString([], { month: "short", year: "numeric" })}`;
    renderTimeView([...Array(7)].map((_, i) => addDays(s, i)));
  } else {
    label.textContent = c.toLocaleDateString([], {
      weekday: "long", month: "long", day: "numeric", year: "numeric",
    });
    renderTimeView([startOfDay(c)]);
  }
}

function occStart(o) { return parseDate(o.start); }
function occEnd(o) { return parseDate(o.end); }

function occsOnDay(day) {
  const dayEnd = addDays(day, 1);
  return state.occs.filter((o) => occStart(o) < dayEnd && occEnd(o) > day);
}

/* ------------------------------ month ----------------------------------- */

function renderMonth() {
  const root = $("#grid-root");
  root.innerHTML = "";
  const grid = document.createElement("div");
  grid.className = "month-grid";
  const gridStart = visibleRange()[0];
  for (let i = 0; i < 7; i++) {
    const dow = document.createElement("div");
    dow.className = "dow";
    dow.textContent = addDays(gridStart, i).toLocaleDateString([], { weekday: "short" });
    grid.appendChild(dow);
  }
  const today = new Date();
  for (let i = 0; i < 42; i++) {
    const day = addDays(gridStart, i);
    const cell = document.createElement("div");
    cell.className = "month-cell";
    if (day.getMonth() !== state.cursor.getMonth()) cell.classList.add("other");
    if (sameDay(day, today)) cell.classList.add("today");
    const num = document.createElement("span");
    num.className = "daynum";
    num.textContent = day.getDate() === 1
      ? day.toLocaleDateString([], { month: "short", day: "numeric" })
      : day.getDate();
    num.onclick = (e) => { e.stopPropagation(); state.cursor = day; setView("day"); };
    cell.appendChild(num);

    const dayOccs = occsOnDay(day).filter(
      (o) => o.all_day || sameDay(occStart(o), day) || !sameDay(day, gridStart)
    );
    const shown = dayOccs.slice(0, 4);
    const extra = dayOccs.length - shown.length;
    for (const o of shown) cell.appendChild(monthChip(o, day));
    if (extra > 0) {
      const more = document.createElement("span");
      more.className = "more-link";
      more.textContent = `+${extra} more`;
      more.onclick = (e) => { e.stopPropagation(); state.cursor = day; setView("day"); };
      cell.appendChild(more);
    }
    cell.onclick = () => openEditor({ defaults: { day } });
    cell.ondblclick = () => openEditor({ defaults: { day } });
    grid.appendChild(cell);
  }
  root.appendChild(grid);
}

function monthChip(o, day) {
  const chip = document.createElement("span");
  const isBar = o.all_day || !sameDay(occStart(o), occEnd(o));
  chip.className = "chip" + (isBar ? "" : " timed");
  if (isBar) {
    chip.style.background = o.color;
    chip.textContent = o.summary;
  } else {
    const dot = document.createElement("span");
    dot.className = "dot";
    dot.style.background = o.color;
    const t = document.createElement("span");
    t.className = "t";
    t.textContent = fmtTime(occStart(o));
    const s = document.createElement("span");
    s.textContent = o.summary;
    s.style.overflow = "hidden";
    s.style.textOverflow = "ellipsis";
    chip.append(dot, t, s);
  }
  chip.title = o.summary;
  chip.onclick = (e) => { e.stopPropagation(); openEditor({ occ: o }); };
  return chip;
}

/* ---------------------------- week / day --------------------------------- */

function renderTimeView(days) {
  const root = $("#grid-root");
  root.innerHTML = "";
  const view = document.createElement("div");
  view.className = "time-view";
  const cols = `52px repeat(${days.length}, 1fr)`;
  const today = new Date();

  const header = document.createElement("div");
  header.className = "time-header";
  header.style.gridTemplateColumns = cols;
  header.appendChild(Object.assign(document.createElement("div"), { className: "corner" }));
  for (const day of days) {
    const h = document.createElement("div");
    h.className = "day-head" + (sameDay(day, today) ? " today" : "");
    h.innerHTML = `<div class="dow">${day.toLocaleDateString([], { weekday: "short" })}</div><div class="num">${day.getDate()}</div>`;
    h.onclick = () => { state.cursor = day; setView("day"); };
    header.appendChild(h);
  }
  view.appendChild(header);

  const alldayRow = document.createElement("div");
  alldayRow.className = "allday-row";
  alldayRow.style.gridTemplateColumns = cols;
  const cornerLbl = document.createElement("div");
  cornerLbl.className = "corner";
  cornerLbl.textContent = "all-day";
  alldayRow.appendChild(cornerLbl);
  for (const day of days) {
    const cell = document.createElement("div");
    cell.className = "allday-cell";
    for (const o of occsOnDay(day).filter((x) => x.all_day)) {
      const chip = document.createElement("span");
      chip.className = "chip";
      chip.style.background = o.color;
      chip.textContent = o.summary;
      chip.onclick = (e) => { e.stopPropagation(); openEditor({ occ: o }); };
      cell.appendChild(chip);
    }
    cell.onclick = () => openEditor({ defaults: { day, allDay: true } });
    alldayRow.appendChild(cell);
  }
  view.appendChild(alldayRow);

  const scroll = document.createElement("div");
  scroll.className = "time-scroll";
  const body = document.createElement("div");
  body.className = "time-body";
  body.style.gridTemplateColumns = cols;
  body.style.height = `${24 * HOUR_PX}px`;

  const gutter = document.createElement("div");
  gutter.className = "gutter";
  for (let h = 1; h < 24; h++) {
    const lbl = document.createElement("div");
    lbl.className = "hlabel";
    lbl.style.top = `${h * HOUR_PX}px`;
    lbl.textContent = new Date(2000, 0, 1, h).toLocaleTimeString([], { hour: "numeric" });
    gutter.appendChild(lbl);
  }
  body.appendChild(gutter);

  for (const day of days) {
    const col = document.createElement("div");
    col.className = "day-col";
    col.dataset.day = ymd(day);
    for (let h = 1; h < 24; h++) {
      const line = document.createElement("div");
      line.className = "hline";
      line.style.top = `${h * HOUR_PX}px`;
      col.appendChild(line);
    }
    layoutDayEvents(col, day);
    if (sameDay(day, today)) {
      const now = document.createElement("div");
      now.className = "now-line";
      now.style.top = `${((today.getHours() * 60 + today.getMinutes()) / 60) * HOUR_PX}px`;
      col.appendChild(now);
    }
    attachGridCreate(col, day);
    body.appendChild(col);
  }
  scroll.appendChild(body);
  view.appendChild(scroll);
  root.appendChild(view);
  scroll.scrollTop = 7.5 * HOUR_PX;
}

function layoutDayEvents(col, day) {
  const dayEnd = addDays(day, 1);
  const evs = occsOnDay(day)
    .filter((o) => !o.all_day)
    .map((o) => {
      const s = occStart(o) < day ? day : occStart(o);
      const e = occEnd(o) > dayEnd ? dayEnd : occEnd(o);
      return { o, s, e };
    })
    .sort((a, b) => a.s - b.s || b.e - a.e);

  // greedy column packing for overlaps
  const lanes = [];
  for (const ev of evs) {
    let lane = lanes.findIndex((last) => last <= ev.s);
    if (lane === -1) { lanes.push(ev.e); lane = lanes.length - 1; }
    else lanes[lane] = ev.e;
    ev.lane = lane;
  }
  const laneCount = Math.max(lanes.length, 1);

  for (const ev of evs) {
    const top = ((ev.s - day) / 3600000) * HOUR_PX;
    const height = Math.max(((ev.e - ev.s) / 3600000) * HOUR_PX, 14);
    const block = document.createElement("div");
    block.className = "event-block";
    block.style.top = `${top}px`;
    block.style.height = `${height - 2}px`;
    block.style.left = `${(ev.lane / laneCount) * 100}%`;
    block.style.width = `${100 / laneCount - 1}%`;
    block.style.background = ev.o.color;
    block.innerHTML = `<div class="ev-summary"></div><div class="ev-time"></div>`;
    block.querySelector(".ev-summary").textContent = ev.o.summary;
    block.querySelector(".ev-time").textContent = `${fmtTime(occStart(ev.o))} – ${fmtTime(occEnd(ev.o))}`;
    const handle = document.createElement("div");
    handle.className = "resize-handle";
    block.appendChild(handle);
    attachEventDrag(block, handle, ev.o);
    block.onclick = (e) => { e.stopPropagation(); if (!block._moved) openEditor({ occ: ev.o }); };
    col.appendChild(block);
  }
}

/* ------------------------- drag interactions ----------------------------- */

function snap(minutes) { return Math.round(minutes / SNAP_MIN) * SNAP_MIN; }

function minutesFromEvent(col, clientY) {
  const rect = col.getBoundingClientRect();
  return snap(((clientY - rect.top) / HOUR_PX) * 60);
}

function attachGridCreate(col, day) {
  let ghost = null;
  let startMin = null;
  col.addEventListener("pointerdown", (e) => {
    if (e.target !== col && !e.target.classList.contains("hline")) return;
    startMin = minutesFromEvent(col, e.clientY);
    ghost = document.createElement("div");
    ghost.className = "ghost-block";
    ghost.style.left = "2%";
    ghost.style.width = "96%";
    col.appendChild(ghost);
    col.setPointerCapture(e.pointerId);
    positionGhost(startMin, startMin + 30);
  });
  col.addEventListener("pointermove", (e) => {
    if (ghost === null) return;
    const cur = minutesFromEvent(col, e.clientY);
    positionGhost(Math.min(startMin, cur), Math.max(startMin, cur, Math.min(startMin, cur) + SNAP_MIN));
  });
  col.addEventListener("pointerup", (e) => {
    if (ghost === null) return;
    const cur = minutesFromEvent(col, e.clientY);
    const a = Math.min(startMin, cur);
    const b = Math.max(startMin, cur, a + 30);
    ghost.remove();
    ghost = null;
    openEditor({
      defaults: {
        start: addMinutes(day, a),
        end: addMinutes(day, b),
      },
    });
  });
  function positionGhost(a, b) {
    ghost.style.top = `${(a / 60) * HOUR_PX}px`;
    ghost.style.height = `${((b - a) / 60) * HOUR_PX}px`;
  }
}

function attachEventDrag(block, handle, occ) {
  let mode = null; // "move" | "resize"
  let startY = 0;
  let origStart, origEnd, dayOffsetPx = 0, targetCol = null;

  const onDown = (e, m) => {
    mode = m;
    startY = e.clientY;
    origStart = occStart(occ);
    origEnd = occEnd(occ);
    block._moved = false;
    block.setPointerCapture(e.pointerId);
    e.stopPropagation();
    e.preventDefault();
  };
  block.addEventListener("pointerdown", (e) => {
    if (e.target === handle) return onDown(e, "resize");
    onDown(e, "move");
  });

  block.addEventListener("pointermove", (e) => {
    if (!mode) return;
    const deltaMin = snap(((e.clientY - startY) / HOUR_PX) * 60);
    if (Math.abs(e.clientY - startY) > 4) block._moved = true;
    if (!block._moved) return;
    block.classList.add("dragging");
    if (mode === "move") {
      // horizontal day change
      targetCol = document
        .elementsFromPoint(e.clientX, e.clientY)
        .find((el) => el.classList && el.classList.contains("day-col"));
      dayOffsetPx = 0;
      block.style.transform = `translateY(${((deltaMin / 60) * HOUR_PX)}px)`;
      block._deltaMin = deltaMin;
    } else {
      const newH = Math.max(((origEnd - origStart) / 60000 + deltaMin) / 60 * HOUR_PX, 14);
      block.style.height = `${newH}px`;
      block._deltaMin = deltaMin;
    }
  });

  block.addEventListener("pointerup", async (e) => {
    if (!mode) return;
    const m = mode;
    mode = null;
    block.classList.remove("dragging");
    block.style.transform = "";
    if (!block._moved) return;
    const deltaMin = block._deltaMin || 0;
    let newStart = origStart, newEnd = origEnd;
    if (m === "move") {
      newStart = addMinutes(origStart, deltaMin);
      newEnd = addMinutes(origEnd, deltaMin);
      if (targetCol && targetCol.dataset.day) {
        const [y, mo, d] = targetCol.dataset.day.split("-").map(Number);
        const dayDelta = Math.round(
          (new Date(y, mo - 1, d) - startOfDay(origStart)) / 86400000
        );
        newStart = addDays(newStart, dayDelta);
        newEnd = addDays(newEnd, dayDelta);
      }
    } else {
      newEnd = addMinutes(origEnd, deltaMin);
      if (newEnd <= newStart) newEnd = addMinutes(newStart, SNAP_MIN);
    }
    await saveOccurrenceTimes(occ, newStart, newEnd);
  });
}

async function saveOccurrenceTimes(occ, newStart, newEnd) {
  const payload = {
    etag: occ.etag,
    summary: occ.summary,
    location: occ.location,
    description: occ.description,
    all_day: false,
    tzid: TZID,
    start: toISO(newStart),
    end: toISO(newEnd),
  };
  try {
    if (occ.is_recurring) {
      const scope = await askScope("edit");
      if (!scope) return refresh();
      payload.scope = scope;
      payload.recurrence_id = occ.recurrence_id;
      if (scope === "all") {
        // shift the master by the same delta instead of absolute times
        const delta = newStart - occStart(occ);
        const masterOccs = state.occs.filter((o) => o.href === occ.href && o.cal === occ.cal);
        void masterOccs;
        payload.start = toISO(newStart);
        payload.end = toISO(newEnd);
      }
    } else {
      payload.scope = "all";
    }
    await api("PUT", `/api/events/${occ.cal}/${occ.href}`, payload);
  } catch (err) {
    toast(`Save failed: ${err.message}`);
  }
  refresh();
}

/* ============================== editor =================================== */

function openEditor(ctx) {
  state.editing = ctx;
  const form = $("#editor-form");
  form.reset();
  const occ = ctx.occ;
  const calSel = $("#ev-cal");
  calSel.innerHTML = "";
  for (const c of state.calendars) {
    const opt = document.createElement("option");
    opt.value = c.id;
    opt.textContent = c.displayname;
    calSel.appendChild(opt);
  }

  let start, end, allDay;
  if (occ) {
    allDay = occ.all_day;
    start = occStart(occ);
    end = occEnd(occ);
    $("#ev-summary").value = occ.summary;
    $("#ev-location").value = occ.location || "";
    $("#ev-description").value = occ.description || "";
    calSel.value = occ.cal;
    calSel.disabled = true;
    $("#ev-delete").hidden = false;
    $("#rec-raw").value = occ.rrule || "";
  } else {
    const d = ctx.defaults || {};
    allDay = !!d.allDay;
    if (d.start) { start = d.start; end = d.end; }
    else {
      const base = d.day ? new Date(d.day) : new Date();
      base.setHours(new Date().getHours() + 1, 0, 0, 0);
      start = base;
      end = addMinutes(base, 60);
    }
    calSel.disabled = false;
    calSel.value = state.calendars.find((c) => !state.hidden.has(c.id))?.id || state.calendars[0]?.id;
    $("#ev-delete").hidden = true;
    $("#rec-raw").value = "";
  }

  $("#ev-allday").checked = allDay;
  $("#ev-start-date").value = ymd(start);
  $("#ev-end-date").value = allDay ? ymd(addDays(end, -1)) : ymd(end);
  $("#ev-start-time").value = `${pad(start.getHours())}:${pad(start.getMinutes())}`;
  $("#ev-end-time").value = `${pad(end.getHours())}:${pad(end.getMinutes())}`;
  syncAllDayUI();
  loadRuleIntoBuilder($("#rec-raw").value);
  $("#editor").showModal();
  $("#ev-summary").focus();
}

function syncAllDayUI() {
  const allDay = $("#ev-allday").checked;
  $("#ev-start-time").style.display = allDay ? "none" : "";
  $("#ev-end-time").style.display = allDay ? "none" : "";
}

function editorTimes() {
  const allDay = $("#ev-allday").checked;
  if (allDay) {
    const start = $("#ev-start-date").value;
    const endDate = parseDate($("#ev-end-date").value);
    return { all_day: true, start, end: ymd(addDays(endDate, 1)) };
  }
  const s = new Date(`${$("#ev-start-date").value}T${$("#ev-start-time").value || "00:00"}`);
  const e = new Date(`${$("#ev-end-date").value}T${$("#ev-end-time").value || "00:00"}`);
  return { all_day: false, tzid: TZID, start: toISO(s), end: toISO(e) };
}

async function saveEditor() {
  const ctx = state.editing;
  const rrule = currentRule();
  const payload = {
    summary: $("#ev-summary").value.trim(),
    location: $("#ev-location").value.trim(),
    description: $("#ev-description").value.trim(),
    rrule: rrule || "",
    ...editorTimes(),
  };
  try {
    if (ctx.occ) {
      payload.etag = ctx.occ.etag;
      const ruleChanged = (ctx.occ.rrule || "") !== (rrule || "");
      if (ctx.occ.is_recurring && !ruleChanged) {
        const scope = await askScope("edit");
        if (!scope) return;
        payload.scope = scope;
        payload.recurrence_id = ctx.occ.recurrence_id;
        if (scope !== "all") delete payload.rrule;
      } else {
        payload.scope = "all";
      }
      await api("PUT", `/api/events/${ctx.occ.cal}/${ctx.occ.href}`, payload);
    } else {
      payload.cal = $("#ev-cal").value;
      await api("POST", "/api/events", payload);
    }
    $("#editor").close();
    refresh();
  } catch (err) {
    toast(`Save failed: ${err.message}`);
  }
}

async function deleteFromEditor() {
  const occ = state.editing.occ;
  try {
    const payload = { etag: occ.etag, scope: "all" };
    if (occ.is_recurring) {
      const scope = await askScope("delete");
      if (!scope) return;
      payload.scope = scope;
      payload.recurrence_id = occ.recurrence_id;
    }
    await api("DELETE", `/api/events/${occ.cal}/${occ.href}`, payload);
    $("#editor").close();
    refresh();
  } catch (err) {
    toast(`Delete failed: ${err.message}`);
  }
}

function askScope(kind) {
  $("#scope-question").textContent =
    kind === "delete" ? "Delete which events?" : "Apply the change to…";
  const dlg = $("#scope-dialog");
  dlg.showModal();
  return new Promise((resolve) => {
    dlg.addEventListener("close", () => resolve(dlg.returnValue || null), { once: true });
  });
}

/* ========================= recurrence builder ============================ */

function builderElements() {
  return {
    freq: $("#rec-freq"),
    intervalWrap: $("#rec-interval-wrap"),
    interval: $("#rec-interval"),
    unit: $("#rec-unit"),
    weekdays: $("#rec-weekdays"),
    monthlyMode: $("#rec-monthly-mode"),
    ends: $("#rec-ends"),
    endMode: $("#rec-end-mode"),
    until: $("#rec-until"),
    count: $("#rec-count"),
    raw: $("#rec-raw"),
    rawNote: $("#rec-raw-note"),
  };
}

function initWeekdayToggles() {
  const box = $("#rec-weekdays");
  box.innerHTML = "";
  const labels = ["M", "T", "W", "T", "F", "S", "S"];
  WEEKDAYS.forEach((wd, i) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "wd-toggle";
    b.dataset.wd = wd;
    b.textContent = labels[i];
    b.setAttribute("aria-pressed", "false");
    b.onclick = () => {
      b.setAttribute("aria-pressed", b.getAttribute("aria-pressed") === "true" ? "false" : "true");
      writeBuilderToRaw();
    };
    box.appendChild(b);
  });
}

function builderVisibility() {
  const el = builderElements();
  const freq = el.freq.value;
  el.intervalWrap.hidden = !freq;
  el.ends.hidden = !freq;
  el.weekdays.hidden = freq !== "WEEKLY";
  el.monthlyMode.hidden = freq !== "MONTHLY";
  el.unit.textContent = { DAILY: "day(s)", WEEKLY: "week(s)", MONTHLY: "month(s)", YEARLY: "year(s)" }[freq] || "";
  el.until.hidden = el.endMode.value !== "until";
  el.count.hidden = el.endMode.value !== "count";
  if (freq === "MONTHLY") {
    const start = parseDate($("#ev-start-date").value || ymd(new Date()));
    $("#monthly-bydate-label").textContent = `on day ${start.getDate()}`;
    const nth = Math.floor((start.getDate() - 1) / 7) + 1;
    const wd = start.toLocaleDateString([], { weekday: "long" });
    $("#monthly-byday-label").textContent = `on the ${["", "1st", "2nd", "3rd", "4th", "5th"][nth]} ${wd}`;
  }
}

function writeBuilderToRaw() {
  const el = builderElements();
  const freq = el.freq.value;
  if (!freq) { el.raw.value = ""; return; }
  const parts = [`FREQ=${freq}`];
  const interval = parseInt(el.interval.value, 10) || 1;
  if (interval > 1) parts.push(`INTERVAL=${interval}`);
  if (freq === "WEEKLY") {
    const days = [...el.weekdays.querySelectorAll('[aria-pressed="true"]')].map((b) => b.dataset.wd);
    if (days.length) parts.push(`BYDAY=${days.join(",")}`);
  }
  if (freq === "MONTHLY") {
    const mode = document.querySelector('input[name="monthly-mode"]:checked').value;
    const start = parseDate($("#ev-start-date").value || ymd(new Date()));
    if (mode === "byday") {
      const nth = Math.floor((start.getDate() - 1) / 7) + 1;
      parts.push(`BYDAY=${nth}${WEEKDAYS[(start.getDay() + 6) % 7]}`);
    }
  }
  if (el.endMode.value === "until" && el.until.value) {
    parts.push(`UNTIL=${el.until.value.replaceAll("-", "")}T235959Z`);
  } else if (el.endMode.value === "count") {
    parts.push(`COUNT=${parseInt(el.count.value, 10) || 1}`);
  }
  el.raw.value = parts.join(";");
  el.rawNote.hidden = true;
}

function loadRuleIntoBuilder(text) {
  const el = builderElements();
  initWeekdayToggles();
  el.raw.value = text || "";
  el.freq.value = "";
  el.interval.value = "1";
  el.endMode.value = "never";
  el.rawNote.hidden = true;
  if (!text) { builderVisibility(); return; }

  const parts = Object.fromEntries(
    text.split(";").filter(Boolean).map((p) => {
      const [k, v] = p.split("=");
      return [k.toUpperCase(), v];
    })
  );
  const simpleKeys = new Set(["FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY", "WKST"]);
  let representable =
    ["DAILY", "WEEKLY", "MONTHLY", "YEARLY"].includes(parts.FREQ) &&
    Object.keys(parts).every((k) => simpleKeys.has(k));
  if (parts.BYDAY) {
    if (parts.FREQ === "WEEKLY" && !/^([A-Z]{2})(,[A-Z]{2})*$/.test(parts.BYDAY)) representable = false;
    if (parts.FREQ === "MONTHLY" && !/^-?\d[A-Z]{2}$/.test(parts.BYDAY)) representable = false;
    if (parts.FREQ === "DAILY" || parts.FREQ === "YEARLY") representable = false;
  }
  if (!representable) {
    el.rawNote.hidden = false;
    $("#rec-raw-wrap").open = true;
    builderVisibility();
    return;
  }
  el.freq.value = parts.FREQ;
  el.interval.value = parts.INTERVAL || "1";
  if (parts.FREQ === "WEEKLY" && parts.BYDAY) {
    for (const wd of parts.BYDAY.split(",")) {
      const b = el.weekdays.querySelector(`[data-wd="${wd}"]`);
      if (b) b.setAttribute("aria-pressed", "true");
    }
  }
  if (parts.FREQ === "MONTHLY" && parts.BYDAY) {
    document.querySelector('input[name="monthly-mode"][value="byday"]').checked = true;
  }
  if (parts.UNTIL) {
    el.endMode.value = "until";
    el.until.value = `${parts.UNTIL.slice(0, 4)}-${parts.UNTIL.slice(4, 6)}-${parts.UNTIL.slice(6, 8)}`;
  } else if (parts.COUNT) {
    el.endMode.value = "count";
    el.count.value = parts.COUNT;
  }
  builderVisibility();
}

function currentRule() {
  return $("#rec-raw").value.trim();
}

/* ============================== sidebar ================================== */

function renderSidebar() {
  const list = $("#calendar-list");
  list.innerHTML = "";
  for (const cal of state.calendars) {
    const li = document.createElement("li");
    if (state.hidden.has(cal.id)) li.classList.add("off");
    const sw = document.createElement("span");
    sw.className = "swatch";
    sw.style.background = cal.color;
    sw.style.color = cal.color;
    sw.textContent = "✓";
    const name = document.createElement("span");
    name.className = "cal-name";
    name.textContent = cal.displayname;
    const gear = document.createElement("button");
    gear.className = "gear";
    gear.textContent = "⚙";
    gear.title = "Calendar settings";
    gear.onclick = (e) => { e.stopPropagation(); openCalDialog(cal); };
    li.append(sw, name, gear);
    li.onclick = () => {
      if (state.hidden.has(cal.id)) state.hidden.delete(cal.id);
      else state.hidden.add(cal.id);
      localStorage.setItem("hiddenCals", JSON.stringify([...state.hidden]));
      renderSidebar();
      refresh();
    };
    list.appendChild(li);
  }
}

function openCalDialog(cal) {
  const dlg = $("#cal-dialog");
  $("#cal-dialog-title").textContent = cal ? "Edit calendar" : "New calendar";
  $("#cal-name").value = cal ? cal.displayname : "";
  $("#cal-color").value = cal ? cal.color : "#1b9e77";
  $("#cal-links").hidden = !cal;
  $("#cal-delete").hidden = !cal;
  dlg.dataset.calId = cal ? cal.id : "";
  dlg.showModal();
}

async function saveCalDialog() {
  const dlg = $("#cal-dialog");
  const id = dlg.dataset.calId;
  const body = { displayname: $("#cal-name").value.trim() || "Calendar", color: $("#cal-color").value };
  try {
    if (id) await api("PATCH", `/api/calendars/${id}`, body);
    else await api("POST", "/api/calendars", body);
    dlg.close();
    await loadCalendars();
    refresh();
  } catch (err) {
    toast(`Could not save calendar: ${err.message}`);
  }
}

/* ============================ wiring ===================================== */

function setView(v) {
  state.view = v;
  localStorage.setItem("view", v);
  refresh();
}

function step(dir) {
  const c = state.cursor;
  if (state.view === "month") state.cursor = new Date(c.getFullYear(), c.getMonth() + dir, 1);
  else state.cursor = addDays(c, dir * (state.view === "week" ? 7 : 1));
  refresh();
}

function wire() {
  $("#today-btn").onclick = () => { state.cursor = new Date(); refresh(); };
  $("#prev-btn").onclick = () => step(-1);
  $("#next-btn").onclick = () => step(1);
  $("#menu-toggle").onclick = () => $("#sidebar").classList.toggle("hidden");
  document.querySelectorAll("#view-switch button").forEach((b) => {
    b.onclick = () => setView(b.dataset.view);
  });
  $("#new-event-btn").onclick = () => openEditor({ defaults: { day: state.cursor } });
  $("#new-calendar-btn").onclick = () => openCalDialog(null);

  $("#editor-form").addEventListener("submit", (e) => { e.preventDefault(); saveEditor(); });
  $("#ev-cancel").onclick = () => $("#editor").close();
  $("#ev-delete").onclick = deleteFromEditor;
  $("#ev-allday").onchange = () => { syncAllDayUI(); };
  $("#ev-start-date").onchange = () => {
    // keep end >= start
    if ($("#ev-end-date").value < $("#ev-start-date").value)
      $("#ev-end-date").value = $("#ev-start-date").value;
    builderVisibility();
  };

  const el = builderElements();
  el.freq.onchange = () => { builderVisibility(); writeBuilderToRaw(); };
  el.interval.onchange = writeBuilderToRaw;
  el.endMode.onchange = () => { builderVisibility(); writeBuilderToRaw(); };
  el.until.onchange = writeBuilderToRaw;
  el.count.onchange = writeBuilderToRaw;
  document.querySelectorAll('input[name="monthly-mode"]').forEach((r) => (r.onchange = writeBuilderToRaw));
  el.raw.onchange = () => loadRuleIntoBuilder(el.raw.value.trim());

  $("#cal-form").addEventListener("submit", (e) => { e.preventDefault(); saveCalDialog(); });
  $("#cal-cancel").onclick = () => $("#cal-dialog").close();
  $("#cal-delete").onclick = async () => {
    const id = $("#cal-dialog").dataset.calId;
    const cal = state.calendars.find((c) => c.id === id);
    if (!confirm(`Delete calendar "${cal.displayname}" and all its events?`)) return;
    await api("DELETE", `/api/calendars/${id}`);
    $("#cal-dialog").close();
    await loadCalendars();
    refresh();
  };
  $("#cal-export").onclick = () => {
    const id = $("#cal-dialog").dataset.calId;
    window.open(`/api/export/${id}.ics`, "_blank");
  };
  $("#cal-feed").onclick = async () => {
    const id = $("#cal-dialog").dataset.calId;
    const cal = state.calendars.find((c) => c.id === id);
    const url = `${location.origin}/feed/${id}.ics?t=${cal.feed_token}`;
    await navigator.clipboard.writeText(url);
    toast("Feed URL copied — paste it into any calendar app as a subscription.");
  };
  $("#cal-rotate").onclick = async () => {
    const id = $("#cal-dialog").dataset.calId;
    await api("POST", `/api/calendars/${id}/rotate-feed-token`);
    await loadCalendars();
    toast("Feed URL reset. Old links no longer work.");
  };

  $("#import-file").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const visible = state.calendars.filter((c) => !state.hidden.has(c.id));
    const target = visible[0] || state.calendars[0];
    try {
      const text = await file.text();
      const res = await api("POST", `/api/import?calendar=${target.id}`, text, true);
      toast(`Imported ${res.imported} event(s) into ${target.displayname}.`);
      refresh();
    } catch (err) {
      toast(`Import failed: ${err.message}`);
    }
    e.target.value = "";
  });

  document.addEventListener("keydown", (e) => {
    if (e.target.matches("input, textarea, select") || $("#editor").open || $("#cal-dialog").open) return;
    if (e.key === "t") { state.cursor = new Date(); refresh(); }
    else if (e.key === "m") setView("month");
    else if (e.key === "w") setView("week");
    else if (e.key === "d") setView("day");
    else if (e.key === "ArrowLeft") step(-1);
    else if (e.key === "ArrowRight") step(1);
    else if (e.key === "n" || e.key === "c") openEditor({ defaults: { day: state.cursor } });
  });
}

/* ============================== boot ===================================== */

(async function main() {
  wire();
  initWeekdayToggles();
  try {
    await loadCalendars();
  } catch (err) {
    toast(`Could not load calendars: ${err.message}`);
  }
  refresh();
})();
