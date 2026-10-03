"use strict";

(() => {
  const SESSION = document.querySelector('meta[name="ezgrader-session"]').content;
  const $ = (id) => document.getElementById(id);
  const GRADABLE = new Set(["points", "percent", "letter_grade", "gpa_scale"]);

  // ---------- utils

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  /** Round half up on the decimal value, matching the server (2.675 -> 2.68). */
  function round2(x) {
    if (x == null || !Number.isFinite(x)) return x;
    const shifted = Number(`${x}e2`);
    if (!Number.isFinite(shifted)) return Math.round(x * 100) / 100;
    return Number(`${Math.round(shifted)}e-2`);
  }
  const fmt = (x) => (x == null || !Number.isFinite(x) ? "—"
    : round2(x).toLocaleString(undefined, { maximumFractionDigits: 2 }));
  const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const when = (iso) => new Date(iso).toLocaleString(undefined,
    { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  const storage = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
  };

  // ---------- state

  const state = {
    profile: null,
    mode: "canvas",      // "canvas" (sync to Canvas) or "style" (grade style on Ed)
    profiles: [],
    data: null,          // /api/load response
    ids: null,           // what's loaded: {lesson_id, canvas_course_id, assignment_id}
    loadedKey: null,
    labels: {},
    rowByKey: new Map(),
    maxOverride: null,
    edits: new Map(),    // row key (Canvas user id) -> final score
    filter: "all",
    search: "",
    sort: { key: "name", dir: 1 },
    showSlides: false,
    view: "grades",
    pushResults: new Map(),
    backups: [],
  };

  // ---------- API

  async function api(method, path, body) {
    const headers = { "X-EzGrader-Session": SESSION };
    if (state.profile) headers["X-EzGrader-Profile"] = state.profile;
    const init = { method, headers, cache: "no-store" };
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(path, init);
    } catch {
      throw new Error("Can't reach EzGrader. Is it still running in the terminal?");
    }
    let data = null;
    try { data = await res.json(); } catch { /* not JSON */ }
    if (!res.ok) throw new Error(data?.error || `Request failed (HTTP ${res.status}).`);
    return data;
  }

  async function pollJob(jobId, onUpdate) {
    for (;;) {
      const job = await api("GET", `/api/jobs/${encodeURIComponent(jobId)}`);
      onUpdate(job);
      if (job.status === "done") return job;
      await sleep(350);
    }
  }

  // ---------- toasts & theme

  function toast(message, kind = "info", ms = 4200) {
    const el = document.createElement("div");
    el.className = `toast ${kind}`;
    el.textContent = message;
    $("toasts").appendChild(el);
    const remove = () => { el.classList.add("leaving"); setTimeout(() => el.remove(), 200); };
    setTimeout(remove, kind === "error" ? Math.max(ms, 8000) : ms);
    el.addEventListener("click", remove);
  }

  const THEME_KEY = "ezgrader.theme";
  function applyTheme(theme) {
    if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
  }
  function toggleTheme() {
    const current = document.documentElement.dataset.theme
      || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const next = current === "dark" ? "light" : "dark";
    applyTheme(next);
    storage.set(THEME_KEY, next);
  }

  // ---------- selects

  function setOptions(select, items, { placeholder = "Select…", value = null, group = false } = {}) {
    select.innerHTML = "";
    const ph = new Option(placeholder, "");
    ph.disabled = true;
    select.add(ph);
    let currentGroup, parent = select;
    for (const it of items) {
      if (group && it.group !== currentGroup) {
        currentGroup = it.group;
        parent = document.createElement("optgroup");
        parent.label = currentGroup || "Other";
        select.appendChild(parent);
      }
      parent.appendChild(new Option(it.label, String(it.value)));
    }
    const wanted = value != null && items.some((i) => String(i.value) === String(value)) ? String(value) : "";
    select.value = wanted;
    select.disabled = items.length === 0;
  }

  function setLoadingSelect(select) {
    select.innerHTML = "";
    select.add(new Option("Loading…", ""));
    select.disabled = true;
  }

  const selectedText = (select) => select.selectedOptions[0]?.textContent || "";

  function lastPicks() {
    try { return JSON.parse(storage.get(`ezgrader.last.${state.profile}`) || "{}"); } catch { return {}; }
  }
  function savePick(key, value) {
    storage.set(`ezgrader.last.${state.profile}`, JSON.stringify({ ...lastPicks(), [key]: value }));
  }

  // ---------- profiles & pickers

  const currentProfile = () => state.profiles.find((p) => p.name === state.profile) || null;

  async function refreshProfiles() {
    const { profiles } = await api("GET", "/api/profiles");
    state.profiles = profiles;
    const sel = $("profileSelect");
    sel.innerHTML = "";
    if (!profiles.length) {
      sel.add(new Option("No profile", ""));
      sel.disabled = true;
      return;
    }
    sel.disabled = false;
    for (const p of profiles) sel.add(new Option(p.name, p.name));
    sel.value = profiles.some((p) => p.name === state.profile) ? state.profile : "";
  }

  async function selectProfile(name) {
    state.profile = name || null;
    storage.set("ezgrader.profile", name || "");
    $("profileSelect").value = name || "";
    resetLoaded();
    await loadPickers();
  }

  function updateEmptyState() {
    const p = currentProfile();
    const styleMode = state.mode === "style";
    const missing = [];
    if (p && !p.ed_token) missing.push("an Ed token");
    if (p && !styleMode && !p.canvas_base_url) missing.push("your Canvas address");
    if (p && !styleMode && !p.canvas_token) missing.push("a Canvas token");
    $("emptyState").querySelector(".steps").innerHTML = (styleMode
      ? ["Open each student's code", "Pick rubric items", "Confirm &amp; write to Ed"]
      : ["Load scores from Ed", "Review &amp; adjust", "Confirm &amp; push"])
      .map((text, i) => `<li><span>${i + 1}</span>${text}</li>`).join("");
    const showKeys = !p || missing.length > 0;
    $("emptyKeysBtn").hidden = !showKeys;
    if (!p) {
      $("emptyTitle").textContent = "Welcome to EzGrader";
      $("emptyText").textContent = "Create a profile and add your Ed and Canvas keys to get started. They're kept in your macOS Keychain.";
    } else if (missing.length) {
      $("emptyTitle").textContent = "Almost there";
      $("emptyText").textContent = `Add ${missing.join(", ").replace(/, ([^,]*)$/, " and $1")} to this profile.`;
    } else if (styleMode) {
      $("emptyTitle").textContent = "Grade style on Ed code slides";
      $("emptyText").innerHTML = "Pick an Ed lesson and a code slide above, then press <b>Load</b>. You'll see each student's code next to the slide's rubric. Nothing is saved to Ed until you review and confirm.";
    } else {
      $("emptyTitle").textContent = "Sync Ed lesson scores to Canvas";
      $("emptyText").innerHTML = "Pick an Ed lesson and a Canvas assignment above, then press <b>Load</b>. Nothing goes to Canvas until you review and confirm.";
    }
  }

  async function loadPickers() {
    const p = currentProfile();
    updateEmptyState();
    const last = lastPicks();
    setOptions($("edLesson"), [], { placeholder: "Pick a course first" });
    setOptions($("cvAssignment"), [], { placeholder: "Pick a course first" });
    resetStyleSlides();
    const tasks = [];
    if (p?.ed_token) tasks.push(loadEdCourses(last.edCourse));
    else setOptions($("edCourse"), [], { placeholder: p ? "Add an Ed token under Keys" : "No profile yet" });
    if (p?.canvas_token && p?.canvas_base_url) tasks.push(loadCanvasCourses(last.cvCourse));
    else setOptions($("cvCourse"), [], { placeholder: p ? "Add Canvas keys under Keys" : "No profile yet" });
    await Promise.all(tasks);
    updateLoadButton();
  }

  async function loadEdCourses(preselect) {
    const sel = $("edCourse");
    setLoadingSelect(sel);
    try {
      const { courses } = await api("GET", "/api/ed/courses");
      setOptions(sel, courses.map((c) => ({
        value: c.id,
        label: [c.code, c.name].filter(Boolean).join(" · ") + (c.status === "archived" ? " (archived)" : ""),
      })), { placeholder: courses.length ? "Choose an Ed course" : "No Ed courses found", value: preselect });
      if (sel.value) await loadEdLessons(sel.value);
    } catch (e) {
      setOptions(sel, [], { placeholder: "Couldn't load Ed courses" });
      toast(e.message, "error");
    }
  }

  async function loadEdLessons(courseId) {
    const sel = $("edLesson");
    resetStyleSlides();
    setLoadingSelect(sel);
    try {
      const { lessons } = await api("GET", `/api/ed/courses/${encodeURIComponent(courseId)}/lessons`);
      const order = [...new Set(lessons.map((l) => l.module || ""))];
      const items = lessons
        .map((l) => ({ value: l.id, label: l.title, group: l.module || "" }))
        .sort((a, b) => order.indexOf(a.group) - order.indexOf(b.group));
      setOptions(sel, items, {
        placeholder: lessons.length ? "Choose a lesson" : "No lessons in this course",
        group: order.some(Boolean),
      });
    } catch (e) {
      setOptions(sel, [], { placeholder: "Couldn't load lessons" });
      toast(e.message, "error");
    }
    updateLoadButton();
  }

  async function loadCanvasCourses(preselect) {
    const sel = $("cvCourse");
    setLoadingSelect(sel);
    try {
      const { courses } = await api("GET", "/api/canvas/courses");
      setOptions(sel, courses.map((c) => ({ value: c.id, label: c.term ? `${c.name} · ${c.term}` : c.name })),
        { placeholder: courses.length ? "Choose a Canvas course" : "No Canvas courses found", value: preselect });
      if (sel.value) await loadAssignments(sel.value);
    } catch (e) {
      setOptions(sel, [], { placeholder: "Couldn't load Canvas courses" });
      toast(e.message, "error");
    }
  }

  async function loadAssignments(courseId) {
    const sel = $("cvAssignment");
    setLoadingSelect(sel);
    try {
      const { assignments } = await api("GET", `/api/canvas/courses/${encodeURIComponent(courseId)}/assignments`);
      setOptions(sel, assignments.map((a) => ({
        value: a.id,
        group: a.group,
        label: `${a.name}${a.points_possible != null ? ` · ${fmt(a.points_possible)} pts` : ""}${a.published ? "" : " (unpublished)"}`,
      })), { placeholder: assignments.length ? "Choose an assignment" : "No assignments in this course", group: true });
    } catch (e) {
      setOptions(sel, [], { placeholder: "Couldn't load assignments" });
      toast(e.message, "error");
    }
    updateLoadButton();
  }

  function updateLoadButton() {
    const needed = state.mode === "style" ? ["edCourse", "edLesson", "styleSlide"] : ["edCourse", "edLesson", "cvCourse", "cvAssignment"];
    $("loadBtn").disabled = !needed.every((id) => $(id).value);
  }

  // ---------- loading the review

  function resetLoaded() {
    state.data = null;
    state.ids = null;
    state.loadedKey = null;
    state.edits.clear();
    state.pushResults.clear();
    state.maxOverride = null;
    resetStyle();
    refreshMainView();
  }

  async function loadReview(ids) {
    if (!ids.lesson_id || !ids.canvas_course_id || !ids.assignment_id) return;
    const buttons = [$("loadBtn"), $("reloadBtn")];
    buttons.forEach((b) => b.classList.add("loading"));
    try {
      const data = await api("POST", "/api/load", ids);
      const key = `${state.profile}:${ids.lesson_id}:${ids.canvas_course_id}:${ids.assignment_id}`;
      if (key !== state.loadedKey) {
        state.edits.clear();
        state.pushResults.clear();
        state.maxOverride = null;
        state.filter = "all";
        state.search = "";
        $("searchInput").value = "";
        state.labels = { lesson: selectedText($("edLesson")) };
      }
      state.data = data;
      state.ids = ids;
      state.loadedKey = key;
      state.labels.assignment = data.assignment.name;
      state.rowByKey = new Map(data.rows.map((r) => [r.key, r]));
      // Drop edits for students who are no longer in the list.
      for (const k of [...state.edits.keys()]) if (!state.rowByKey.has(k)) state.edits.delete(k);
      setSetupCollapsed(true);
      renderAll();
      refreshBackups();
    } catch (e) {
      toast(e.message, "error");
    } finally {
      buttons.forEach((b) => b.classList.remove("loading"));
    }
  }

  function setSetupCollapsed(collapsed) {
    const styleMode = state.mode === "style";
    const loaded = styleMode ? !!st.data : !!state.data;
    const show = collapsed && loaded;
    $("setupForm").hidden = show;
    $("setupSummary").hidden = !show;
    if (!loaded) return;
    if (styleMode) {
      $("sumLesson").textContent = st.labels.lesson || "Lesson";
      $("sumSlide").textContent = `${st.labels.slide} · ${fmt(st.data.challenge.rubric_points)} style pts`;
    } else {
      $("sumLesson").textContent = state.labels.lesson || `Lesson ${state.ids.lesson_id}`;
      $("sumAssignment").textContent = `${state.labels.assignment} · ${fmt(state.data.assignment.points_possible)} pts`;
    }
  }

  // ---------- derived values

  const maxPoints = () => state.maxOverride ?? state.data.lesson.total_max;
  const canPush = () => {
    const a = state.data.assignment;
    return GRADABLE.has(a.grading_type) && a.points_possible > 0;
  };

  function derive(r) {
    const max = maxPoints();
    const computed = max > 0 ? round2((r.ed.total / max) * 100) : 0;
    const edited = state.edits.has(r.key);
    const final = edited ? state.edits.get(r.key) : computed;
    const cur = r.canvas.current;
    const pp = state.data.assignment.points_possible || 0;
    const pushable = !cur.excused && canPush();
    // Compare in Canvas points so assignments out of 10 don't flap on rounding.
    const changed = pushable && (cur.points == null || Math.abs((final * pp) / 100 - cur.points) >= 0.005);
    return { computed, final, edited, changed, pushable };
  }

  const allDerived = () => state.data.rows.map((r) => ({ r, c: derive(r) }));

  const sorters = {
    name: (x) => (x.r.canvas.sortable_name || x.r.ed.name).toLowerCase(),
    earned: (x) => x.r.ed.total,
    computed: (x) => x.c.computed,
    final: (x) => x.c.final,
    canvas: (x) => x.r.canvas.current.score_100 ?? -1,
    status: (x) => (x.c.changed ? 0 : 2) - (x.r.flags.length ? 1 : 0),
  };

  function visibleRows() {
    const q = state.search.trim().toLowerCase();
    let list = allDerived();
    if (state.filter === "changed") list = list.filter((x) => x.c.changed);
    else if (state.filter === "flagged") list = list.filter((x) => x.r.flags.length);
    else if (state.filter === "edited") list = list.filter((x) => x.c.edited);
    if (q) {
      list = list.filter((x) => [x.r.ed.name, x.r.canvas.name, x.r.ed.email, x.r.canvas.email, x.r.canvas.login_id]
        .join(" ").toLowerCase().includes(q));
    }
    const key = sorters[state.sort.key] || sorters.name;
    const tiebreak = sorters.name;
    list.sort((a, b) => {
      const ka = key(a), kb = key(b);
      if (ka !== kb) return (ka < kb ? -1 : 1) * state.sort.dir;
      return tiebreak(a) < tiebreak(b) ? -1 : 1;
    });
    return list;
  }

  // ---------- rendering

  function renderAll() {
    refreshMainView();
    renderMax(true);
    renderWarnings();
    renderFilters();
    renderView();
    renderHead();
    renderBody();
    renderUnmatched();
    renderCounts();
  }

  function renderView() {
    const grades = state.view === "grades";
    const showCanvas = state.mode === "canvas" && !!state.data;
    document.querySelectorAll(".tab").forEach((t) => {
      const active = t.dataset.view === state.view;
      t.classList.toggle("active", active);
      t.setAttribute("aria-selected", String(active));
    });
    $("tableWrap").hidden = !(showCanvas && grades);
    $("gradeTools").hidden = !grades;
    $("unmatchedView").hidden = !(showCanvas && !grades);
  }

  function renderFilters() {
    document.querySelectorAll(".chip[data-filter]").forEach((c) => c.classList.toggle("active", c.dataset.filter === state.filter));
    $("slidesBtn").setAttribute("aria-pressed", String(state.showSlides));
  }

  function renderMax(setValue) {
    const input = $("maxInput");
    if (setValue) input.value = String(maxPoints());
    const overridden = state.maxOverride != null;
    $("maxWrap").classList.toggle("overridden", overridden);
    $("maxReset").hidden = !overridden;
    $("maxReset").title = `Back to ${fmt(state.data.lesson.total_max)} (parsed from Ed)`;
  }

  function renderWarnings() {
    const list = state.data.warnings || [];
    $("warnings").hidden = !list.length;
    $("warnings").innerHTML = list.map((w) => `<div class="warning">${esc(w)}</div>`).join("");
  }

  function renderHead() {
    const s = state.sort;
    const th = (key, label, cls = "") => {
      const active = s.key === key;
      const arrow = `<span class="arrow">${active ? (s.dir > 0 ? "↑" : "↓") : ""}</span>`;
      const aria = active ? (s.dir > 0 ? "ascending" : "descending") : "none";
      const content = cls.includes("num") ? `${arrow}${label}` : `${label}${arrow}`;
      return `<th class="sortable ${cls}" data-sort="${key}" aria-sort="${aria}">${content}</th>`;
    };
    const slides = state.data.lesson.slides;
    const slideHeads = state.showSlides ? slides.map((sl, i) => `<th class="col-slide num${i === 0 ? " slide-first" : ""}${
      i === slides.length - 1 ? " slide-last" : ""}" title="${esc(sl.title)}">#${esc(sl.number)}<small>${
      sl.max ? `/${fmt(sl.max)}` : "—"}</small></th>`).join("") : "";
    $("gridHead").innerHTML = `<tr>${th("name", "Student", "col-student")}${slideHeads}${th("earned", "Earned", "num")}${
      th("computed", "Computed", "num col-computed")}${th("final", "Final /100", "num")}${th("canvas", "Canvas", "num")}${
      th("status", "Status")}</tr>`;
  }

  function canvasCell(r) {
    const cur = r.canvas.current;
    const pp = state.data.assignment.points_possible;
    if (cur.excused) return `<span class="none" title="Excused in Canvas">EX</span>`;
    if (cur.points == null) return `<span class="none" title="No grade in Canvas yet">—</span>`;
    const sub = pp && Math.abs(pp - 100) > 1e-9 ? `<small>${fmt(cur.points)}/${fmt(pp)}</small>` : "";
    return `${fmt(cur.score_100)}${sub}`;
  }

  function statusHTML(r, c) {
    const parts = [];
    const res = state.pushResults.get(r.canvas.user_id);
    if (res && !res.ok) parts.push(`<span class="badge bad" title="${esc(res.error || "")}">✕ Push failed</span>`);
    else if (res?.warning) parts.push(`<span class="badge warn" title="${esc(res.warning)}">✓ Pushed (check)</span>`);
    else if (res && !c.changed) parts.push(`<span class="badge ok">✓ Pushed</span>`);

    if (r.canvas.current.excused) parts.push(`<span class="badge excused" title="Excused in Canvas, so it won't be pushed">Excused</span>`);
    else if (c.changed) parts.push(r.canvas.current.points == null
      ? `<span class="badge new">New grade</span>` : `<span class="badge update">Update</span>`);
    else if (!res) parts.push(`<span class="badge same">Up to date</span>`);

    for (const f of r.flags) parts.push(`<span class="badge flag" title="${esc(f.detail)}">${esc(f.label)}</span>`);
    return parts.join("");
  }

  function rowHTML({ r, c }) {
    const slides = state.data.lesson.slides;
    const slideCells = state.showSlides ? r.ed.scores.map((v, i) => {
      const sl = slides[i];
      const atAuto = sl.auto && sl.auto < sl.max && Math.abs(v - sl.auto) < 1e-6;
      const cls = ["col-slide", "num", v === 0 && "zero", atAuto && "at-auto",
        i === 0 && "slide-first", i === slides.length - 1 && "slide-last"].filter(Boolean).join(" ");
      return `<td class="${cls}"${atAuto ? ` title="Exactly the ${fmt(sl.auto)} auto points"` : ""}>${fmt(v)}</td>`;
    }).join("") : "";
    const name = r.canvas.name || r.ed.name;
    const email = r.canvas.email || r.ed.email.trim().toLowerCase() || r.canvas.login_id;
    const finalCls = ["num", "final", c.edited && "edited", c.final > 100 && "over"].filter(Boolean).join(" ");
    return `<tr data-key="${esc(r.key)}"${c.changed ? ' class="is-changed"' : ""}>
      <td class="col-student"><div class="student-name" title="${esc(name)}">${esc(name)}</div><div class="student-email" title="${esc(email)}">${esc(email)}</div></td>
      ${slideCells}
      <td class="num earned">${fmt(r.ed.total)}<small>/ ${fmt(maxPoints())}</small></td>
      <td class="num computed col-computed">${fmt(c.computed)}</td>
      <td class="${finalCls}"><div class="final-wrap"><button class="btn ghost reset" type="button" tabindex="-1" title="Reset to computed (${fmt(c.computed)})" aria-label="Reset to computed">↺</button><input type="number" min="0" step="0.01" inputmode="decimal" value="${c.final}" aria-label="Final score for ${esc(name)}"></div></td>
      <td class="num canvas-cell">${canvasCell(r)}</td>
      <td class="status-cell"><div class="status">${statusHTML(r, c)}</div></td>
    </tr>`;
  }

  function renderBody() {
    const list = visibleRows();
    $("gridBody").innerHTML = list.map(rowHTML).join("");
    $("noRows").hidden = list.length > 0;
    $("grid").classList.toggle("slides-open", state.showSlides);
  }

  function updateRow(tr) {
    const r = state.rowByKey.get(tr.dataset.key);
    const c = derive(r);
    const td = tr.querySelector("td.final");
    td.classList.toggle("edited", c.edited);
    td.classList.toggle("over", c.final > 100);
    td.querySelector(".reset").title = `Reset to computed (${fmt(c.computed)})`;
    tr.classList.toggle("is-changed", c.changed);
    tr.querySelector(".status").innerHTML = statusHTML(r, c);
  }

  function renderCounts() {
    const all = allDerived();
    const changed = all.filter((x) => x.c.changed).length;
    const pushable = all.filter((x) => x.c.pushable).length;
    const flagged = all.filter((x) => x.r.flags.length).length;
    const edited = all.filter((x) => x.c.edited).length;
    const unmatched = state.data.ed_only.length + state.data.canvas_only.length;
    $("countGrades").textContent = all.length;
    $("countUnmatched").textContent = unmatched;
    document.querySelector('.tab[data-view="unmatched"]').classList.toggle("has-unmatched", unmatched > 0);
    $("countChanged").textContent = changed;
    $("countFlagged").textContent = flagged;
    $("countEdited").textContent = edited;

    const bits = [`<b>${changed}</b> to update`, `<b>${pushable - changed}</b> unchanged`];
    if (flagged) bits.push(`<b>${flagged}</b> flagged`);
    if (unmatched) bits.push(`<b>${unmatched}</b> unmatched`);
    $("footerSummary").innerHTML = bits.join(" · ");

    const btn = $("reviewBtn");
    btn.disabled = !canPush() || pushable === 0 || !(maxPoints() > 0);
    btn.textContent = changed ? `Review ${plural(changed, "change")}` : "Review changes";
  }

  function renderUnmatched() {
    const d = state.data;
    $("countEdOnly").textContent = d.ed_only.length;
    $("countCanvasOnly").textContent = d.canvas_only.length;
    $("edOnlyList").innerHTML = `<ul class="plain-list">${d.ed_only.length ? d.ed_only.map((s) => `
      <li><div class="who"><div class="student-name">${esc(s.name || "(no name)")}</div>
        <div class="student-email">${esc(s.email || "no email")}</div><div class="reason">${esc(s.reason)}</div></div>
        <div class="num">${fmt(s.total)}<span class="muted"> / ${fmt(maxPoints())}</span></div></li>`).join("")
      : '<li class="empty-li">Everyone in Ed matched a Canvas student.</li>'}</ul>`;
    $("canvasOnlyList").innerHTML = `<ul class="plain-list">${d.canvas_only.length ? d.canvas_only.map((s) => `
      <li><div class="who"><div class="student-name">${esc(s.name)}</div>
        <div class="student-email">${esc(s.email || s.login_id || "no email visible")}</div></div>
        <span class="muted">not changed</span></li>`).join("")
      : '<li class="empty-li">Every Canvas student has Ed results.</li>'}</ul>`;
  }

  // ---------- table interactions

  function onFinalInput(input) {
    const tr = input.closest("tr");
    const r = state.rowByKey.get(tr.dataset.key);
    const { computed } = derive(r);
    const raw = input.value.trim();
    if (raw === "") {
      state.edits.delete(r.key);
      input.classList.remove("invalid");
    } else {
      const n = Number(raw);
      if (!Number.isFinite(n) || n < 0) {
        input.classList.add("invalid");
        return;
      }
      input.classList.remove("invalid");
      if (Math.abs(n - computed) < 0.005) state.edits.delete(r.key);
      else state.edits.set(r.key, round2(n));
    }
    updateRow(tr);
    renderCounts();
  }

  function moveFocus(input, delta) {
    const inputs = [...$("gridBody").querySelectorAll("td.final input")];
    const next = inputs[inputs.indexOf(input) + delta];
    if (next) { next.focus(); next.select(); }
  }

  // ---------- review & push

  let review = null;

  function openReview() {
    const all = allDerived();
    const pushable = all.filter((x) => x.c.pushable);
    review = {
      changed: pushable.filter((x) => x.c.changed),
      unchanged: pushable.filter((x) => !x.c.changed),
      excused: all.filter((x) => x.r.canvas.current.excused).length,
      unmatched: state.data.ed_only.length + state.data.canvas_only.length,
      includeUnchanged: false,
      selected: new Set(),
      running: false,
    };
    review.changed.forEach((x) => review.selected.add(x.r.key));
    renderReview();
    $("reviewDialog").showModal();
  }

  const reviewList = () => (review.includeUnchanged ? [...review.changed, ...review.unchanged] : review.changed);
  const reviewChosen = () => reviewList().filter((x) => review.selected.has(x.r.key));

  function gradeText(points, score100, excused) {
    if (excused) return "Excused";
    if (points == null) return "no grade";
    return fmt(score100);
  }

  function renderReview() {
    const d = state.data;
    const a = d.assignment;
    const pp = a.points_possible;
    const list = reviewList();

    const infos = [];
    const warns = [];
    if (Math.abs(pp - 100) > 1e-9) {
      infos.push(`“${esc(a.name)}” is out of ${fmt(pp)} points in Canvas, so scores are sent as percentages (85.5 → ${fmt((85.5 * pp) / 100)} pts).`);
    }
    if (!a.published) warns.push("This assignment is unpublished in Canvas. Canvas may refuse grades until it's published.");
    if (a.post_manually) infos.push("This assignment posts grades manually, so students won't see them until you post them in Canvas.");
    if (a.grading_type === "letter_grade" || a.grading_type === "gpa_scale") infos.push("Canvas will convert each score to a letter grade.");
    if (state.maxOverride != null) infos.push(`Max points is set to ${fmt(state.maxOverride)} (Ed's labels add up to ${fmt(d.lesson.total_max)}).`);

    const tiles = [
      `<div class="tile primary"><b>${review.changed.length}</b><span>will change</span></div>`,
      `<div class="tile"><b>${review.unchanged.length}</b><span>unchanged (${review.includeUnchanged ? "included" : "skipped"})</span></div>`,
      `<div class="tile"><b>${review.unmatched}</b><span>unmatched (skipped)</span></div>`,
    ];
    if (review.excused) tiles.push(`<div class="tile"><b>${review.excused}</b><span>excused (skipped)</span></div>`);

    const rows = list.map((x) => {
      const cur = x.r.canvas.current;
      const checked = review.selected.has(x.r.key);
      const delta = cur.points == null ? null : round2(x.c.final - cur.score_100);
      const deltaCls = delta > 0 ? "up" : delta < 0 ? "down" : "";
      const ptsNote = Math.abs(pp - 100) > 1e-9 ? `<span class="sub">${fmt((x.c.final * pp) / 100)} pts</span>` : "";
      const notes = [x.c.edited && "edited", ...x.r.flags.map((f) => f.label)].filter(Boolean);
      return `<tr class="${checked ? "" : "unchecked"}">
        <td class="col-check"><input type="checkbox" data-key="${esc(x.r.key)}" ${checked ? "checked" : ""} aria-label="Include ${esc(x.r.canvas.name)}"></td>
        <td>${esc(x.r.canvas.name || x.r.ed.name)}${notes.length ? `<span class="sub">${esc(notes.join(" · "))}</span>` : ""}</td>
        <td class="num">${gradeText(cur.points, cur.score_100, cur.excused)}</td>
        <td class="arrow">→</td>
        <td class="num new">${fmt(x.c.final)}${ptsNote}</td>
        <td class="num delta ${deltaCls}">${delta == null ? "" : `${delta > 0 ? "+" : ""}${fmt(delta)}`}</td>
      </tr>`;
    }).join("");

    $("reviewTitle").textContent = "Review changes";
    $("reviewBody").innerHTML = `
      <div class="tiles">${tiles.join("")}</div>
      <div class="info-list" id="reviewNotes">${warns.map((w) => `<div class="warning">${w}</div>`).join("")}${
        infos.map((i) => `<div class="info">${i}</div>`).join("")}</div>
      <div class="list-head">
        <label><input type="checkbox" id="includeUnchanged" ${review.includeUnchanged ? "checked" : ""}> Also re-send unchanged grades</label>
        <div class="links"><button class="btn ghost xs" type="button" data-select="all">Select all</button><button class="btn ghost xs" type="button" data-select="none">None</button></div>
      </div>
      ${list.length ? `<table class="change-table"><thead><tr><th class="col-check"></th><th>Student</th><th class="num">Canvas now</th><th></th><th class="num">New</th><th class="num">Δ</th></tr></thead><tbody>${rows}</tbody></table>`
        : '<div class="empty-msg">Every matched grade already matches Canvas. Tick “Also re-send unchanged grades” to push anyway.</div>'}`;
    renderReviewFoot();
  }

  function renderReviewFoot() {
    const chosen = reviewChosen();
    const n = chosen.length;
    const over = chosen.filter((x) => x.c.final > 100).length;
    const zeros = chosen.filter((x) => x.c.final === 0).length;
    const flagged = chosen.filter((x) => x.r.flags.length).length;
    const extra = [];
    if (flagged) extra.push(`${flagged} flagged`);
    if (zeros) extra.push(`${zeros} at 0`);
    if (over) extra.push(`${over} over 100`);
    // Any change to the selection needs a fresh confirmation.
    $("reviewFoot").innerHTML = `
      <label class="confirm-box"><input type="checkbox" id="confirmPush" ${n ? "" : "disabled"}>
        I've checked ${n === 1 ? "this grade" : `these ${n} grades`}${extra.length ? ` <span class="muted">(${esc(extra.join(", "))})</span>` : ""}</label>
      <span class="spacer"></span>
      <button class="btn ghost" type="button" data-close>Cancel</button>
      <button class="btn primary" type="button" id="pushBtn" disabled>Push ${plural(n, "grade")} to Canvas</button>`;
  }

  async function startPush(items) {
    review.running = true;
    const pushBtn = $("pushBtn");
    if (pushBtn) { pushBtn.disabled = true; pushBtn.textContent = "Backing up…"; }
    let started;
    try {
      started = await api("POST", "/api/push", {
        canvas_course_id: state.ids.canvas_course_id,
        assignment_id: state.ids.assignment_id,
        grades: items.map((x) => ({ user_id: x.r.canvas.user_id, score_100: x.c.final })),
      });
    } catch (e) {
      review.running = false;
      toast(e.message, "error");
      renderReviewFoot();
      return;
    }
    const pp = state.data.assignment.points_possible;
    const lines = items.filter((x) => !started.skipped.some((s) => s.user_id === x.r.canvas.user_id)).map((x) => ({
      user_id: x.r.canvas.user_id,
      name: x.r.canvas.name,
      detail: `${gradeText(x.r.canvas.current.points, x.r.canvas.current.score_100, false)} → ${fmt(x.c.final)}${
        Math.abs(pp - 100) > 1e-9 ? ` (${fmt((x.c.final * pp) / 100)} pts)` : ""}`,
      item: x,
    }));
    $("reviewTitle").textContent = "Pushing to Canvas";
    let job;
    try {
      job = await pollJob(started.job_id, (j) => renderProgress("review", j, lines, started.skipped, "Pushing"));
    } catch (e) {
      toast(`Lost track of the push: ${e.message}. Reload to see what landed.`, "error");
      review.running = false;
      return;
    }
    review.running = false;
    for (const res of job.results) state.pushResults.set(res.user_id, res);
    const failed = job.results.filter((res) => !res.ok);
    const ok = job.results.length - failed.length;
    $("reviewTitle").textContent = failed.length ? "Push finished with errors" : "Push complete";
    renderProgress("review", job, lines, started.skipped, "Pushed");
    const retry = failed.length ? `<button class="btn" type="button" id="retryBtn">Retry ${plural(failed.length, "failed grade")}</button>` : "";
    $("reviewFoot").innerHTML = `<span class="muted">Backup saved. You can undo this with “Revert last push”.</span><span class="spacer"></span>${retry}
      <button class="btn primary" type="button" data-close data-reload>Done</button>`;
    review.retryItems = lines.filter((l) => failed.some((f) => f.user_id === l.user_id)).map((l) => l.item);
    toast(failed.length ? `${ok} grades pushed, ${failed.length} failed.` : `${plural(ok, "grade")} pushed to Canvas.`,
      failed.length ? "warn" : "ok");
  }

  function renderProgress(prefix, job, lines, skipped, verb) {
    const byId = new Map(job.results.map((r) => [r.user_id, r]));
    const failed = job.results.filter((r) => !r.ok).length;
    const pct = job.total ? Math.round((job.done / job.total) * 100) : 0;
    const done = job.status === "done";
    const rows = lines.map((l) => {
      const res = byId.get(l.user_id);
      let icon = '<span class="result-icon pending">…</span>';
      let note = "";
      if (res && !res.ok) { icon = '<span class="result-icon bad">✕</span>'; note = `<span class="err-text">${esc(res.error)}</span>`; }
      else if (res?.warning) { icon = '<span class="result-icon warn">!</span>'; note = `<span class="warn-text">${esc(res.warning)}</span>`; }
      else if (res) icon = '<span class="result-icon ok">✓</span>';
      return `<tr><td class="col-check">${icon}</td><td>${esc(l.name)}${note ? `<span class="sub">${note}</span>` : ""}</td><td class="num">${esc(l.detail)}</td></tr>`;
    }).join("");
    const skippedHTML = skipped?.length ? `<div class="info-list"><div class="warning">Skipped ${plural(skipped.length, "student")}: ${
      esc(skipped.map((s) => `${s.name || `user ${s.user_id}`} (${s.reason})`).join(", "))}</div></div>` : "";
    $(`${prefix}Body`).innerHTML = `
      <div class="progress-label"><span>${done ? `${verb} ${job.done - failed} of ${job.total}` : `${verb} ${job.done} of ${job.total}…`}</span>
        <span>${failed ? `${failed} failed` : ""}</span></div>
      <div class="progress ${done ? (failed ? "has-errors" : "done") : ""}"><div id="${prefix}Bar"></div></div>
      ${skippedHTML}
      <table class="change-table"><tbody>${rows}</tbody></table>`;
    $(`${prefix}Bar`).style.width = `${pct}%`;
    if (!done) {
      $(`${prefix}Foot`).innerHTML = '<span class="muted">Working… keep this window open.</span><span class="spacer"></span><button class="btn" type="button" disabled>Close</button>';
    }
  }

  // ---------- revert

  let revert = null;

  async function refreshBackups() {
    if (!state.ids) return;
    try {
      const { backups } = await api("GET",
        `/api/backups?course_id=${state.ids.canvas_course_id}&assignment_id=${state.ids.assignment_id}`);
      state.backups = backups;
    } catch {
      state.backups = [];
    }
    const last = state.backups.find((b) => b.kind === "push" && !b.reverted_at);
    $("revertBtn").disabled = !last;
    $("revertBtn").title = last
      ? `Put back the ${plural(last.count, "grade")} changed ${when(last.created_at)}`
      : "No push to revert for this assignment";
  }

  async function openRevert() {
    const last = state.backups.find((b) => b.kind === "push" && !b.reverted_at);
    if (!last) return;
    revert = { backup: last, running: false, preview: null };
    $("revertTitle").textContent = "Revert last push";
    $("revertBody").innerHTML = '<div class="empty-msg">Checking current grades in Canvas…</div>';
    $("revertFoot").innerHTML = '<span class="spacer"></span><button class="btn ghost" type="button" data-close>Cancel</button>';
    $("revertDialog").showModal();
    try {
      revert.preview = await api("POST", "/api/revert/preview", { backup_id: last.id });
      renderRevert();
    } catch (e) {
      $("revertBody").innerHTML = `<div class="empty-msg">${esc(e.message)}</div>`;
    }
  }

  function revertText(g) {
    return g.excused ? "Excused" : g.points == null ? "no grade" : fmt(g.score_100);
  }

  function renderRevert() {
    const { backup, rows } = revert.preview;
    const todo = rows.filter((r) => r.differs);
    const drifted = todo.filter((r) => r.changed_since_push).length;
    const body = rows.map((r) => `<tr class="${r.differs ? "" : "unchecked"}">
      <td>${esc(r.name)}${r.changed_since_push ? '<span class="sub warn-text">changed in Canvas since the push</span>' : ""}${
        r.differs ? "" : '<span class="sub">already matches</span>'}</td>
      <td class="num">${revertText(r.current)}</td><td class="arrow">→</td>
      <td class="num new">${revertText(r.restore)}</td></tr>`).join("");
    $("revertBody").innerHTML = `
      <div class="info-list">
        <div class="info">Puts back the Canvas grades saved right before the push on <b>${esc(when(backup.created_at))}</b>${
          backup.profile ? ` (profile ${esc(backup.profile)})` : ""}. A new backup is saved first.</div>
        ${drifted ? `<div class="warning">${plural(drifted, "grade")} changed in Canvas after the push. Reverting will overwrite ${drifted === 1 ? "it" : "them"}.</div>` : ""}
      </div>
      <table class="change-table"><thead><tr><th>Student</th><th class="num">Canvas now</th><th></th><th class="num">Restore to</th></tr></thead><tbody>${body}</tbody></table>`;
    $("revertFoot").innerHTML = todo.length ? `
      <label class="confirm-box"><input type="checkbox" id="confirmRevert"> Restore ${plural(todo.length, "grade")}</label>
      <span class="spacer"></span>
      <button class="btn ghost" type="button" data-close>Cancel</button>
      <button class="btn danger" type="button" id="doRevert" disabled>Revert ${plural(todo.length, "grade")}</button>`
      : '<span class="muted">Canvas already matches the backup.</span><span class="spacer"></span><button class="btn" type="button" data-close>Close</button>';
  }

  async function startRevert() {
    const { backup, rows } = revert.preview;
    revert.running = true;
    $("doRevert").disabled = true;
    let started;
    try {
      started = await api("POST", "/api/revert", { backup_id: backup.id });
    } catch (e) {
      revert.running = false;
      toast(e.message, "error");
      renderRevert();
      return;
    }
    const lines = rows.filter((r) => r.differs).map((r) => ({
      user_id: r.user_id, name: r.name, detail: `${revertText(r.current)} → ${revertText(r.restore)}`,
    }));
    $("revertTitle").textContent = "Reverting";
    let job;
    try {
      job = await pollJob(started.job_id, (j) => renderProgress("revert", j, lines, [], "Restoring"));
    } catch (e) {
      revert.running = false;
      toast(`Lost track of the revert: ${e.message}`, "error");
      return;
    }
    revert.running = false;
    const failed = job.results.filter((r) => !r.ok).length;
    $("revertTitle").textContent = failed ? "Revert finished with errors" : "Revert complete";
    renderProgress("revert", job, lines, [], "Restored");
    $("revertFoot").innerHTML = `<span class="muted">${failed ? "Run Revert again to retry the failed ones." : "Grades restored."}</span>
      <span class="spacer"></span><button class="btn primary" type="button" data-close data-reload>Done</button>`;
    state.pushResults.clear();
    toast(failed ? `Revert: ${failed} failed.` : "Last push reverted.", failed ? "warn" : "ok");
  }

  // ---------- keys & profiles dialog

  let kpSelected = null;
  let keysDirty = false;
  let deleteTimer = null;

  async function openKeys() {
    try { await refreshProfiles(); } catch (e) { toast(e.message, "error"); }
    kpSelected = state.profile || state.profiles[0]?.name || null;
    keysDirty = false;
    renderKeys();
    $("keysDialog").showModal();
    if (!state.profiles.length) showNewProfileForm();
  }

  function renderKeys() {
    const sel = $("kpProfile");
    sel.innerHTML = "";
    state.profiles.forEach((p) => sel.add(new Option(p.name, p.name)));
    sel.disabled = !state.profiles.length;
    const p = state.profiles.find((x) => x.name === kpSelected);
    $("kpBody").hidden = !p;
    $("kpEmpty").hidden = !!p;
    if (!p) return;
    sel.value = p.name;
    for (const card of document.querySelectorAll("#kpBody .key-card")) {
      const kind = card.dataset.kind;
      const form = card.querySelector(".key-edit");
      const input = form.querySelector("input");
      if (kind === "canvas_base_url") {
        input.value = p.canvas_base_url || "";
        continue;
      }
      const masked = p[kind];
      card.querySelector(".key-saved").hidden = !masked;
      form.hidden = !!masked;
      card.querySelector(".mask").textContent = masked || "";
      const result = card.querySelector(".test-result");
      result.textContent = "";
      result.className = "test-result";
      input.value = "";
      form.querySelector('[data-act="cancel"]').hidden = true;
    }
    const link = $("canvasTokenLink");
    link.hidden = !p.canvas_base_url;
    $("canvasTokenHint").hidden = !!p.canvas_base_url;
    if (p.canvas_base_url) link.href = `${p.canvas_base_url}/profile/settings`;
    disarmDelete();
  }

  function showNewProfileForm() {
    $("kpNewForm").hidden = false;
    $("kpNewName").value = "";
    $("kpNewName").focus();
  }

  function replaceProfileInfo(info) {
    const i = state.profiles.findIndex((p) => p.name === info.name);
    if (i >= 0) state.profiles[i] = info;
  }

  async function saveKey(card) {
    const kind = card.dataset.kind;
    const input = card.querySelector(".key-edit input");
    const value = input.value.trim();
    if (!value) { input.focus(); return; }
    const button = card.querySelector('.key-edit button[type="submit"]');
    button.disabled = true;
    try {
      const info = await api("PUT", `/api/profiles/${encodeURIComponent(kpSelected)}`, { [kind]: value });
      input.value = "";  // never keep a pasted token in the page
      replaceProfileInfo(info);
      keysDirty = true;
      renderKeys();
      if (kind === "canvas_base_url") {
        toast(`Canvas address saved: ${info.canvas_base_url}`, "ok");
      } else {
        toast(`${kind === "ed_token" ? "Ed" : "Canvas"} token saved to your Keychain.`, "ok");
        testKey(document.querySelector(`#kpBody .key-card[data-kind="${kind}"]`));
      }
    } catch (e) {
      toast(e.message, "error");
    } finally {
      button.disabled = false;
    }
  }

  async function testKey(card) {
    const result = card.querySelector(".test-result");
    result.className = "test-result";
    result.textContent = "Testing…";
    try {
      const res = await api("POST", `/api/profiles/${encodeURIComponent(kpSelected)}/test`,
        { service: card.dataset.kind === "ed_token" ? "ed" : "canvas" });
      result.className = `test-result ${res.ok ? "ok" : "bad"}`;
      result.textContent = `${res.ok ? "✓" : "✕"} ${res.message}`;
    } catch (e) {
      result.className = "test-result bad";
      result.textContent = `✕ ${e.message}`;
    }
  }

  function disarmDelete() {
    clearTimeout(deleteTimer);
    const btn = $("kpDelete");
    btn.classList.remove("armed");
    btn.textContent = "Delete profile";
  }

  async function deleteProfile() {
    const btn = $("kpDelete");
    if (!btn.classList.contains("armed")) {
      btn.classList.add("armed");
      btn.textContent = `Delete “${kpSelected}”?`;
      deleteTimer = setTimeout(disarmDelete, 4000);
      return;
    }
    disarmDelete();
    const name = kpSelected;
    try {
      await api("DELETE", `/api/profiles/${encodeURIComponent(name)}`);
      toast(`Profile “${name}” and its keys were deleted.`, "ok");
      keysDirty = true;
      await refreshProfiles();
      kpSelected = state.profiles[0]?.name || null;
      if (state.profile === name) await selectProfile(kpSelected);
      renderKeys();
    } catch (e) {
      toast(e.message, "error");
    }
  }

  async function createProfile(e) {
    e.preventDefault();
    const name = $("kpNewName").value.trim();
    if (!name) return;
    try {
      await api("POST", "/api/profiles", { name });
      keysDirty = true;
      await refreshProfiles();
      kpSelected = name;
      $("kpNewForm").hidden = true;
      if (!state.profile) await selectProfile(name);
      renderKeys();
      document.querySelector('#kpBody .key-card[data-kind="ed_token"] input')?.focus();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function onKeysClosed() {
    $("kpNewForm").hidden = true;
    if (!keysDirty) return;
    const before = JSON.stringify(currentProfile());
    try { await refreshProfiles(); } catch { return; }
    if (!state.profile && state.profiles.length) {
      await selectProfile(state.profiles[0].name);
    } else if (JSON.stringify(currentProfile()) !== before || !state.data) {
      await loadPickers();
    }
  }

  // ---------- events

  function bindEvents() {
    $("themeBtn").addEventListener("click", toggleTheme);
    $("keysBtn").addEventListener("click", openKeys);
    $("emptyKeysBtn").addEventListener("click", openKeys);

    $("profileSelect").addEventListener("change", (e) => {
      if ((state.edits.size || st.drafts.size) && !confirm("Switching profiles clears your unsaved edits and drafts. Continue?")) {
        e.target.value = state.profile;
        return;
      }
      selectProfile(e.target.value);
    });

    $("edCourse").addEventListener("change", (e) => { savePick("edCourse", e.target.value); loadEdLessons(e.target.value); });
    $("cvCourse").addEventListener("change", (e) => { savePick("cvCourse", e.target.value); loadAssignments(e.target.value); });
    $("edLesson").addEventListener("change", (e) => { updateLoadButton(); onLessonChanged(e.target.value); });
    $("cvAssignment").addEventListener("change", updateLoadButton);
    $("setupForm").addEventListener("submit", (e) => {
      e.preventDefault();
      if (state.mode === "style") {
        loadStyle(Number($("styleSlide").value));
        return;
      }
      loadReview({
        lesson_id: Number($("edLesson").value),
        canvas_course_id: Number($("cvCourse").value),
        assignment_id: Number($("cvAssignment").value),
      });
    });
    $("reloadBtn").addEventListener("click", () => {
      if (state.mode === "style") { if (st.challengeId) loadStyle(st.challengeId); }
      else if (state.ids) loadReview(state.ids);
    });
    $("changeBtn").addEventListener("click", () => setSetupCollapsed(false));

    document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => {
      state.view = t.dataset.view;
      renderView();
    }));
    document.querySelectorAll(".chip[data-filter]").forEach((c) => c.addEventListener("click", () => {
      state.filter = c.dataset.filter;
      renderFilters();
      renderBody();
    }));
    $("searchInput").addEventListener("input", (e) => { state.search = e.target.value; renderBody(); });
    $("slidesBtn").addEventListener("click", () => {
      state.showSlides = !state.showSlides;
      renderFilters();
      renderHead();
      renderBody();
    });

    $("maxInput").addEventListener("input", (e) => {
      const n = Number(e.target.value);
      if (!Number.isFinite(n) || n <= 0) { e.target.classList.add("invalid"); return; }
      e.target.classList.remove("invalid");
      state.maxOverride = Math.abs(n - state.data.lesson.total_max) < 1e-9 ? null : n;
      renderMax(false);
      renderBody();
      renderUnmatched();
      renderCounts();
    });
    $("maxReset").addEventListener("click", () => {
      state.maxOverride = null;
      $("maxInput").classList.remove("invalid");
      renderMax(true);
      renderBody();
      renderUnmatched();
      renderCounts();
    });

    $("gridHead").addEventListener("click", (e) => {
      const th = e.target.closest("th[data-sort]");
      if (!th) return;
      const key = th.dataset.sort;
      state.sort = { key, dir: state.sort.key === key ? -state.sort.dir : 1 };
      renderHead();
      renderBody();
    });

    const body = $("gridBody");
    body.addEventListener("input", (e) => { if (e.target.matches("td.final input")) onFinalInput(e.target); });
    body.addEventListener("change", (e) => {
      if (!e.target.matches("td.final input")) return;
      const r = state.rowByKey.get(e.target.closest("tr").dataset.key);
      if (e.target.value.trim() === "" || e.target.classList.contains("invalid")) {
        e.target.value = String(derive(r).final);
        e.target.classList.remove("invalid");
      }
    });
    body.addEventListener("focusin", (e) => { if (e.target.matches("td.final input")) e.target.select(); });
    body.addEventListener("keydown", (e) => {
      if (!e.target.matches("td.final input")) return;
      if (e.key === "Enter" || e.key === "ArrowDown") { e.preventDefault(); moveFocus(e.target, 1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); moveFocus(e.target, -1); }
      else if (e.key === "Escape") {
        const tr = e.target.closest("tr");
        state.edits.delete(tr.dataset.key);
        e.target.value = String(derive(state.rowByKey.get(tr.dataset.key)).final);
        updateRow(tr);
        renderCounts();
      }
    });
    body.addEventListener("click", (e) => {
      const btn = e.target.closest(".reset");
      if (!btn) return;
      const tr = btn.closest("tr");
      const r = state.rowByKey.get(tr.dataset.key);
      state.edits.delete(r.key);
      const input = tr.querySelector("td.final input");
      input.value = String(derive(r).final);
      input.classList.remove("invalid");
      updateRow(tr);
      renderCounts();
    });

    // Review dialog
    $("reviewBtn").addEventListener("click", openReview);
    const rd = $("reviewDialog");
    rd.addEventListener("change", (e) => {
      if (e.target.id === "includeUnchanged") {
        review.includeUnchanged = e.target.checked;
        review.unchanged.forEach((x) => (e.target.checked ? review.selected.add(x.r.key) : review.selected.delete(x.r.key)));
        renderReview();
      } else if (e.target.matches("input[data-key]")) {
        if (e.target.checked) review.selected.add(e.target.dataset.key);
        else review.selected.delete(e.target.dataset.key);
        e.target.closest("tr").classList.toggle("unchecked", !e.target.checked);
        renderReviewFoot();
      } else if (e.target.id === "confirmPush") {
        $("pushBtn").disabled = !e.target.checked || reviewChosen().length === 0;
      }
    });
    rd.addEventListener("click", (e) => {
      const sel = e.target.closest("[data-select]");
      if (sel) {
        const all = sel.dataset.select === "all";
        reviewList().forEach((x) => (all ? review.selected.add(x.r.key) : review.selected.delete(x.r.key)));
        renderReview();
        return;
      }
      if (e.target.id === "pushBtn") startPush(reviewChosen());
      else if (e.target.id === "retryBtn") startPush(review.retryItems);
    });

    // Revert dialog
    $("revertBtn").addEventListener("click", openRevert);
    const rv = $("revertDialog");
    rv.addEventListener("change", (e) => {
      if (e.target.id === "confirmRevert") $("doRevert").disabled = !e.target.checked;
    });
    rv.addEventListener("click", (e) => { if (e.target.id === "doRevert") startRevert(); });

    // Shared dialog behaviour: [data-close] buttons; no closing mid-push.
    for (const dlg of [rd, rv, $("keysDialog"), $("styleWriteDialog"), $("styleRevertDialog")]) {
      dlg.addEventListener("click", (e) => {
        const closer = e.target.closest("[data-close]");
        if (!closer || isBusy(dlg)) return;
        dlg.close(closer.hasAttribute("data-reload") ? "reload" : "");
      });
      dlg.addEventListener("cancel", (e) => { if (isBusy(dlg)) e.preventDefault(); });
    }
    const afterChange = () => {
      if (state.ids) loadReview(state.ids);
    };
    rd.addEventListener("close", () => { if (rd.returnValue === "reload") afterChange(); rd.returnValue = ""; });
    rv.addEventListener("close", () => { if (rv.returnValue === "reload") afterChange(); rv.returnValue = ""; });

    // Keys dialog
    const kd = $("keysDialog");
    kd.addEventListener("close", onKeysClosed);
    $("kpProfile").addEventListener("change", (e) => { kpSelected = e.target.value; renderKeys(); });
    $("kpNewBtn").addEventListener("click", showNewProfileForm);
    $("kpNewCancel").addEventListener("click", () => { $("kpNewForm").hidden = true; });
    $("kpNewForm").addEventListener("submit", createProfile);
    $("kpDelete").addEventListener("click", deleteProfile);
    $("kpBody").addEventListener("submit", (e) => {
      e.preventDefault();
      saveKey(e.target.closest(".key-card"));
    });
    $("kpBody").addEventListener("click", (e) => {
      const act = e.target.closest("[data-act]")?.dataset.act;
      if (!act) return;
      const card = e.target.closest(".key-card");
      if (act === "test") testKey(card);
      else if (act === "replace") {
        card.querySelector(".key-saved").hidden = true;
        const form = card.querySelector(".key-edit");
        form.hidden = false;
        form.querySelector('[data-act="cancel"]').hidden = false;
        form.querySelector("input").focus();
      } else if (act === "cancel") renderKeys();
    });

    $("quitBtn").addEventListener("click", quitApp);
    $("reloadPageBtn").addEventListener("click", () => location.reload());
    window.addEventListener("beforeunload", (e) => {
      if (quitting) return;
      if (state.edits.size || st.drafts.size || review?.running || revert?.running || sw?.running || srv?.running) {
        e.preventDefault();
      }
    });
    bindStyleEvents();
  }

  function isBusy(dlg) {
    if (dlg.id === "reviewDialog") return !!review?.running;
    if (dlg.id === "revertDialog") return !!revert?.running;
    if (dlg.id === "styleWriteDialog") return !!sw?.running;
    if (dlg.id === "styleRevertDialog") return !!srv?.running;
    return false;
  }

  // ---------- modes

  function setMode(mode) {
    state.mode = mode === "style" ? "style" : "canvas";
    document.body.dataset.mode = state.mode;
    storage.set("ezgrader.mode", state.mode);
    document.querySelectorAll(".mode").forEach((b) => {
      const active = b.dataset.mode === state.mode;
      b.classList.toggle("active", active);
      b.setAttribute("aria-selected", String(active));
    });
    const lesson = $("edLesson").value;
    if (state.mode === "style" && lesson && st.slidesLesson !== lesson) loadStyleSlides(lesson);
    refreshMainView();
    updateLoadButton();
  }

  function refreshMainView() {
    const styleMode = state.mode === "style";
    const loaded = styleMode ? !!st.data : !!state.data;
    $("emptyState").hidden = loaded;
    $("toolbar").hidden = styleMode || !state.data;
    $("styleView").hidden = !(styleMode && st.data);
    $("actionbar").hidden = !loaded;
    renderView();
    updateEmptyState();
    setSetupCollapsed(loaded);
  }

  // ---------- style grading: Ed rubrics on code slides

  const st = {
    slides: [],
    slidesLesson: null,
    data: null,          // /api/style/challenges/:id
    key: null,
    challengeId: null,
    byId: new Map(),
    items: new Map(),    // rubric item id -> {item, section}
    drafts: new Map(),   // user id -> Set of rubric item ids
    filter: "todo",
    search: "",
    current: null,
    submission: new Map(),
    files: new Map(),    // submission id -> "loading" | {files} | {error}
    fileTab: 0,
    results: new Map(),
    backups: [],
    labels: {},
  };
  let sw = null;   // write dialog
  let srv = null;  // revert dialog

  const sameSet = (a, b) => {
    const x = new Set(a), y = new Set(b);
    return x.size === y.size && [...x].every((v) => y.has(v));
  };
  const signed = (n) => (n > 0 ? `+${fmt(n)}` : fmt(n));
  const shortText = (text, n = 60) => {
    const t = String(text || "").replace(/\s+/g, " ").trim();
    return t.length > n ? `${t.slice(0, n - 1)}…` : t;
  };
  const slideLabel = (s) => `#${s.number} ${String(s.title).replace(/\s*\[[^\]]*\]\s*$/, "")}`;
  const selectionOf = (s) => st.drafts.get(s.user_id) || new Set(s.selected_ids);
  const pointsOf = (ids) => [...ids].reduce((sum, id) => sum + (st.items.get(id)?.item.points || 0), 0);
  const isTodo = (s) => !s.graded && !st.drafts.has(s.user_id) && !!s.lesson_mark_id && !s.error;
  const currentStudent = () => st.byId.get(st.current) || null;
  const currentSubmissionId = (s) => (s ? st.submission.get(s.user_id) ?? s.submissions[0]?.id ?? null : null);
  const orderedItems = () => {
    const r = st.data?.rubric;
    return r ? [...r.sections.flatMap((sec) => sec.items), ...r.loose_items] : [];
  };

  function resetStyle() {
    Object.assign(st, { data: null, key: null, challengeId: null, byId: new Map(), items: new Map(), current: null, backups: [] });
    st.drafts.clear();
    st.results.clear();
    st.submission.clear();
    st.files.clear();
  }

  function resetStyleSlides(placeholder = "Pick a lesson first") {
    st.slides = [];
    st.slidesLesson = null;
    setOptions($("styleSlide"), [], { placeholder });
  }

  function onLessonChanged(lessonId) {
    if (state.mode === "style") loadStyleSlides(lessonId);
    else resetStyleSlides();
  }

  async function loadStyleSlides(lessonId) {
    const sel = $("styleSlide");
    if (!lessonId) {
      resetStyleSlides();
      updateLoadButton();
      return;
    }
    st.slidesLesson = String(lessonId);
    setLoadingSelect(sel);
    try {
      const { slides } = await api("GET", `/api/style/lessons/${encodeURIComponent(lessonId)}/slides`);
      if (st.slidesLesson !== String(lessonId)) return;  // a newer pick won
      st.slides = slides;
      setOptions(sel, slides.map((s) => ({ value: s.challenge_id, label: slideLabel(s) })),
        { placeholder: slides.length ? "Choose a code slide" : "No code slides in this lesson" });
    } catch (e) {
      st.slidesLesson = null;
      setOptions(sel, [], { placeholder: "Couldn't load slides" });
      toast(e.message, "error");
    }
    updateLoadButton();
  }

  async function loadStyle(challengeId) {
    if (!challengeId) return;
    const buttons = [$("loadBtn"), $("reloadBtn")];
    buttons.forEach((b) => b.classList.add("loading"));
    try {
      const data = await api("GET", `/api/style/challenges/${encodeURIComponent(challengeId)}`);
      const key = `${state.profile}:${challengeId}`;
      if (key !== st.key) {
        resetStyle();
        st.filter = "todo";
        st.search = "";
        $("styleSearch").value = "";
        const slide = st.slides.find((s) => s.challenge_id === challengeId);
        st.labels = { lesson: selectedText($("edLesson")), slide: slide ? slideLabel(slide) : data.challenge.title };
      }
      st.data = data;
      st.key = key;
      st.challengeId = challengeId;
      st.byId = new Map(data.students.map((s) => [s.user_id, s]));
      st.items = new Map();
      if (data.rubric) {
        for (const section of data.rubric.sections) for (const item of section.items) st.items.set(item.id, { item, section });
        for (const item of data.rubric.loose_items) st.items.set(item.id, { item, section: null });
      }
      // Drafts that Ed now matches (written, or graded elsewhere the same way) are done.
      for (const [uid, sel] of [...st.drafts]) {
        const s = st.byId.get(uid);
        if (!s || sameSet(sel, s.selected_ids)) st.drafts.delete(uid);
      }
      if (!st.byId.has(st.current)) st.current = (queueStudents()[0] || data.students[0])?.user_id ?? null;
      refreshMainView();
      renderStyle();
      refreshStyleBackups();
    } catch (e) {
      toast(e.message, "error");
    } finally {
      buttons.forEach((b) => b.classList.remove("loading"));
    }
  }

  function statusOf(s) {
    const result = st.results.get(s.user_id);
    if (s.error) return { cls: "bad", text: "Couldn't load" };
    if (!s.lesson_mark_id) return { cls: "muted", text: "No mark record" };
    if (st.drafts.has(s.user_id)) return { cls: "draft", text: `Draft ${signed(pointsOf(st.drafts.get(s.user_id)))}` };
    if (result && !result.ok) return { cls: "bad", text: "Write failed" };
    if (s.graded) return { cls: "ok", text: `Graded ${fmt(s.rubric_mark)}` };
    return { cls: "todo", text: "To grade" };
  }

  function queueStudents() {
    const q = st.search.trim().toLowerCase();
    return st.data.students.filter((s) => {
      if (s.user_id === st.current && !q) return true;  // keep the open student in place
      if (q && !`${s.name} ${s.email}`.toLowerCase().includes(q)) return false;
      if (st.filter === "todo") return isTodo(s);
      if (st.filter === "drafts") return st.drafts.has(s.user_id);
      return true;
    });
  }

  function renderStyle() {
    renderQueue();
    renderDetail();
    renderStyleFooter();
  }

  function renderQueue() {
    const d = st.data;
    const list = queueStudents();
    $("countTodo").textContent = d.students.filter(isTodo).length;
    $("countDrafts").textContent = st.drafts.size;
    $("countAllStudents").textContent = d.students.length;
    document.querySelectorAll(".chip[data-sfilter]").forEach((c) => c.classList.toggle("active", c.dataset.sfilter === st.filter));
    $("queueList").innerHTML = list.length ? list.map((s) => {
      const status = statusOf(s);
      return `<li><button type="button" class="queue-item${s.user_id === st.current ? " active" : ""}" data-user="${s.user_id}">
        <span class="who"><div class="student-name">${esc(s.name || s.email)}</div><div class="student-email">${esc(s.email)}</div></span>
        <span class="badge ${status.cls}">${esc(status.text)}</span></button></li>`;
    }).join("") : `<li class="queue-empty">${st.filter === "todo" ? "Everyone here has a style mark or a draft." : "No students match."}</li>`;
    $("queueSelect").innerHTML = list.map((s) => `<option value="${s.user_id}"${s.user_id === st.current ? " selected" : ""}>${esc(s.name || s.email)} · ${esc(statusOf(s).text)}</option>`).join("");
    $("queueNote").hidden = !d.no_submission;
    $("queueNote").textContent = d.no_submission
      ? `${plural(d.no_submission, "student")} with no submission ${d.no_submission === 1 ? "isn't" : "aren't"} listed.` : "";
    $("queueList").querySelector(".queue-item.active")?.scrollIntoView({ block: "nearest" });
  }

  function renderDetail() {
    const box = $("styleDetail");
    const s = currentStudent();
    if (!s) {
      box.innerHTML = '<div class="code-empty">No students with submissions on this slide yet.</div>';
      return;
    }
    const list = queueStudents();
    const pos = list.findIndex((x) => x.user_id === s.user_id);
    const c = st.data.challenge;
    const sid = currentSubmissionId(s);
    const options = s.submissions.map((sub, i) => `<option value="${sub.id}"${sub.id === sid ? " selected" : ""}>${
      i === 0 ? "Latest" : `#${s.submissions.length - i}`} · ${esc(when(sub.created_at))}${
      sub.total != null ? ` · ${sub.passed}/${sub.total} tests` : ""}</option>`).join("");
    box.innerHTML = `
      <div class="detail-head">
        <div class="who"><div class="student-name">${esc(s.name || s.email)}</div><div class="student-email">${esc(s.email)}</div></div>
        <div class="detail-nav">
          <button class="btn ghost sm" type="button" data-nav="-1" title="Previous student (K)" aria-label="Previous student"${pos <= 0 ? " disabled" : ""}>↑</button>
          <span>${pos >= 0 ? `${pos + 1} of ${list.length}` : ""}</span>
          <button class="btn ghost sm" type="button" data-nav="1" title="Next student (J)" aria-label="Next student"${pos < 0 || pos >= list.length - 1 ? " disabled" : ""}>↓</button>
        </div>
      </div>
      <div class="detail-meta">
        ${s.submissions.length ? `<label>Submission <select id="submissionSelect">${options}</select></label>` : "<span>No submissions</span>"}
        <span>Tests <b>${fmt(s.auto_mark)}</b> / ${fmt(c.auto_points)}</span>
        <span>Style in Ed <b>${s.graded ? fmt(s.rubric_mark) : "—"}</b> / ${fmt(c.rubric_points)}</span>
        ${s.mark_override != null ? `<span class="warn-text">Mark overridden in Ed: ${fmt(s.mark_override)}</span>` : ""}
        ${s.error ? `<span class="err-text">${esc(s.error)}</span>` : ""}
      </div>
      <div class="detail-body">
        <div class="code-box" id="codeBox"></div>
        <div class="rubric-col"><div class="rubric-box" id="rubricBox"></div><div class="draft-bar" id="draftBar"></div></div>
      </div>`;
    renderCode();
    renderRubric();
    if (sid) ensureFiles(sid).then(prefetchNext);
  }

  async function ensureFiles(sid) {
    if (!sid || st.files.has(sid)) return;
    st.files.set(sid, "loading");
    try {
      const { files } = await api("GET", `/api/style/submissions/${encodeURIComponent(sid)}/files`);
      st.files.set(sid, { files });
    } catch (e) {
      st.files.set(sid, { error: e.message });
    }
    if (currentSubmissionId(currentStudent()) === sid) renderCode();
  }

  function prefetchNext() {
    const list = queueStudents();
    const next = list[list.findIndex((x) => x.user_id === st.current) + 1];
    if (next) ensureFiles(currentSubmissionId(next));
  }

  function renderCode() {
    const box = $("codeBox");
    const s = currentStudent();
    if (!box || !s) return;
    const sid = currentSubmissionId(s);
    const entry = sid ? st.files.get(sid) : null;
    if (!sid) {
      box.innerHTML = '<div class="code-empty">This student has no submission.</div>';
      return;
    }
    if (!entry || entry === "loading") {
      box.innerHTML = '<div class="code-empty"><span class="spinner on" aria-hidden="true"></span>Opening the submission on Ed\'s code server…</div>';
      return;
    }
    if (entry.error) {
      box.innerHTML = `<div class="code-empty"><span class="err-text">${esc(entry.error)}</span><button class="btn sm" type="button" data-act="retry-code">Try again</button></div>`;
      return;
    }
    const files = entry.files || [];
    if (!files.length) {
      box.innerHTML = '<div class="code-empty">No files in this submission.</div>';
      return;
    }
    st.fileTab = Math.min(st.fileTab, files.length - 1);
    const f = files[st.fileTab];
    const tabs = `<div class="file-tabs" role="tablist">${files.map((x, i) => `<button type="button" class="file-tab${
      i === st.fileTab ? " active" : ""}" data-tab="${i}" role="tab" aria-selected="${i === st.fileTab}">${esc(x.path)}</button>`).join("")}</div>`;
    const code = f.content == null
      ? `<div class="code-empty err-text">${esc(f.error || "Couldn't read this file.")}</div>`
      : `<pre class="code" tabindex="0" aria-label="${esc(f.path)}"><code>${f.content.replace(/\n$/, "").split("\n")
        .map((line) => `<span class="line">${esc(line) || " "}</span>`).join("")}</code></pre>${
        f.error ? `<div class="code-empty warn-text">${esc(f.error)}</div>` : ""}`;
    box.innerHTML = tabs + code;
  }

  function renderRubric() {
    const box = $("rubricBox");
    const s = currentStudent();
    if (!box || !s) return;
    const r = st.data.rubric;
    if (!r) {
      box.innerHTML = '<p class="rubric-note">This slide has no rubric in Ed, so there\'s nothing to grade here.</p>';
      renderDraftBar(s);
      return;
    }
    const sel = selectionOf(s);
    const inEd = new Set(s.selected_ids);
    let n = 0;
    const itemHTML = (item, section) => {
      n += 1;
      const on = sel.has(item.id);
      return `<button type="button" class="rubric-item${on ? " selected" : ""}" data-item="${item.id}" aria-pressed="${on}"${s.lesson_mark_id ? "" : " disabled"}>
        <span class="pick ${section?.select_one ? "radio" : "check"}" aria-hidden="true"></span>
        <span class="body"><span class="title">${n <= 9 ? `<span class="muted">${n}.</span> ` : ""}${esc(item.title || "(untitled item)")}</span>${
          item.description ? `<span class="desc">${esc(item.description)}</span>` : ""}${
          inEd.has(item.id) ? '<span class="in-ed">✓ selected in Ed</span>' : ""}</span>
        <span class="pts${item.points < 0 ? " neg" : ""}">${signed(item.points)}</span>
      </button>`;
    };
    const parts = r.sections.map((section) => `<div class="rubric-section"><div class="section-head"><h4>${esc(section.title || "Rubric")}</h4>${
      section.select_one ? "<span>pick one</span>" : ""}</div>${section.items.map((i) => itemHTML(i, section)).join("")}</div>`);
    if (r.loose_items.length) {
      parts.push(`<div class="rubric-section"><div class="section-head"><h4>${r.sections.length ? "Other" : "Rubric"}</h4></div>${
        r.loose_items.map((i) => itemHTML(i, null)).join("")}</div>`);
    }
    const note = s.lesson_mark_id ? "" : '<p class="rubric-note">Ed has no marking record for this student yet, so a style mark can\'t be saved.</p>';
    box.innerHTML = note + parts.join("");
    renderDraftBar(s);
  }

  function renderDraftBar(s) {
    const bar = $("draftBar");
    if (!bar) return;
    bar.hidden = !st.data.rubric || !s.lesson_mark_id;
    if (bar.hidden) return;
    const draft = st.drafts.get(s.user_id);
    const sel = selectionOf(s);
    bar.innerHTML = `
      <span>${draft ? "Draft" : "Selected"}: <b>${sel.size ? signed(pointsOf(sel)) : "nothing"}</b></span>
      <span>Ed now: <b>${s.graded ? fmt(s.rubric_mark) : "not graded"}</b></span>
      <span class="spacer"></span>
      ${draft ? '<button class="btn ghost sm" type="button" data-act="reset-draft">Reset to Ed</button>' : ""}
      <button class="btn sm" type="button" data-act="next">Next student ↓</button>
      <span class="kbd-hint">J/K move · 1–9 pick</span>`;
  }

  function showStudent(uid) {
    if (!st.byId.has(uid)) return;
    st.current = uid;
    st.fileTab = 0;
    renderQueue();
    renderDetail();
  }

  function moveStudent(delta) {
    const list = queueStudents();
    const next = list[list.findIndex((x) => x.user_id === st.current) + delta];
    if (next) showStudent(next.user_id);
  }

  function toggleItem(itemId) {
    const s = currentStudent();
    if (!s || !s.lesson_mark_id || !st.items.has(itemId)) return;
    const sel = new Set(selectionOf(s));
    const { section } = st.items.get(itemId);
    if (sel.has(itemId)) {
      sel.delete(itemId);
    } else {
      if (section?.select_one) section.items.forEach((i) => sel.delete(i.id));
      sel.add(itemId);
    }
    if (sameSet(sel, s.selected_ids)) st.drafts.delete(s.user_id);
    else st.drafts.set(s.user_id, sel);
    renderRubric();
    renderQueue();
    renderStyleFooter();
  }

  function draftRows() {
    return [...st.drafts].map(([uid, sel]) => ({ s: st.byId.get(uid), sel }))
      .filter((r) => r.s && r.s.lesson_mark_id && !sameSet(r.sel, r.s.selected_ids));
  }

  function renderStyleFooter() {
    if (!st.data) return;
    const drafts = draftRows().length;
    const todo = st.data.students.filter(isTodo).length;
    const graded = st.data.students.filter((s) => s.graded).length;
    $("styleSummary").innerHTML = `<b>${drafts}</b> draft${drafts === 1 ? "" : "s"} · <b>${todo}</b> to grade · <b>${graded}</b> graded in Ed`;
    const btn = $("styleWriteBtn");
    btn.disabled = !drafts;
    btn.textContent = drafts ? `Review ${plural(drafts, "draft")}` : "Review drafts";
  }

  async function refreshStyleBackups() {
    if (!st.challengeId) return;
    try {
      const { backups } = await api("GET", `/api/backups?assignment_id=${st.challengeId}&kind=style`);
      st.backups = backups;
    } catch {
      st.backups = [];
    }
    const last = st.backups.find((b) => !b.reverted_at);
    $("styleRevertBtn").disabled = !last;
    $("styleRevertBtn").title = last
      ? `Put back the rubric selections from before the write ${when(last.created_at)}`
      : "No style write to revert on this slide";
  }

  function itemsLabel(ids) {
    const list = [...(ids || [])];
    if (!list.length) return "nothing selected";
    return list.map((id) => {
      const item = st.items.get(id)?.item;
      return item ? `${shortText(item.title, 38)} (${signed(item.points)})` : `item ${id}`;
    }).join(", ");
  }

  // ---------- style: write to Ed

  function openStyleWrite() {
    const rows = draftRows();
    if (!rows.length) return;
    sw = { rows, selected: new Set(rows.map((r) => r.s.user_id)), running: false, retry: [] };
    renderStyleWrite();
    $("styleWriteDialog").showModal();
  }

  function renderStyleWrite() {
    $("styleWriteTitle").textContent = "Write style grades to Ed";
    const rows = sw.rows.map(({ s, sel }) => {
      const checked = sw.selected.has(s.user_id);
      return `<tr class="${checked ? "" : "unchecked"}">
        <td class="col-check"><input type="checkbox" data-user="${s.user_id}" ${checked ? "checked" : ""} aria-label="Include ${esc(s.name)}"></td>
        <td>${esc(s.name || s.email)}<span class="sub">${esc(s.email)}</span></td>
        <td>${esc(itemsLabel(s.selected_ids))}<span class="sub">${s.graded ? `Ed: ${fmt(s.rubric_mark)}` : "Ed: not graded"}</span></td>
        <td class="arrow">→</td>
        <td class="new">${esc(itemsLabel(sel))}<span class="sub">${signed(pointsOf(sel))} pts</span></td>
      </tr>`;
    }).join("");
    $("styleWriteBody").innerHTML = `
      <div class="info-list"><div class="info">Selects these rubric items on each student's marking record in Ed, the same as ticking them in Ed's marking panel. Ed's current selections are backed up first, and anyone graded in Ed since you loaded this slide is skipped.</div></div>
      <div class="list-head"><span class="muted">${plural(sw.rows.length, "draft")}</span>
        <div class="links"><button class="btn ghost xs" type="button" data-select="all">Select all</button><button class="btn ghost xs" type="button" data-select="none">None</button></div></div>
      <table class="change-table"><thead><tr><th class="col-check"></th><th>Student</th><th>In Ed now</th><th></th><th>New</th></tr></thead><tbody>${rows}</tbody></table>`;
    renderStyleWriteFoot();
  }

  function renderStyleWriteFoot() {
    const n = sw.rows.filter((r) => sw.selected.has(r.s.user_id)).length;
    $("styleWriteFoot").innerHTML = `
      <label class="confirm-box"><input type="checkbox" id="confirmStyleWrite" ${n ? "" : "disabled"}> I've checked ${n === 1 ? "this grade" : `these ${n} grades`}</label>
      <span class="spacer"></span>
      <button class="btn ghost" type="button" data-close>Cancel</button>
      <button class="btn primary" type="button" id="doStyleWrite" disabled>Write ${plural(n, "grade")} to Ed</button>`;
  }

  async function startStyleWrite(rows) {
    sw.running = true;
    const btn = $("doStyleWrite");
    if (btn) { btn.disabled = true; btn.textContent = "Backing up…"; }
    let started;
    try {
      started = await api("POST", "/api/style/write", {
        challenge_id: st.challengeId,
        grades: rows.map(({ s, sel }) => ({ user_id: s.user_id, lesson_mark_id: s.lesson_mark_id, select: [...sel], expected: s.selected_ids })),
      });
    } catch (e) {
      sw.running = false;
      toast(e.message, "error");
      renderStyleWriteFoot();
      return;
    }
    const skipped = new Set(started.skipped.map((x) => x.user_id));
    const lines = rows.filter((r) => !skipped.has(r.s.user_id)).map((r) => ({
      user_id: r.s.user_id,
      name: r.s.name || r.s.email,
      detail: `${r.s.graded ? fmt(r.s.rubric_mark) : "—"} → ${signed(pointsOf(r.sel))}`,
      row: r,
    }));
    $("styleWriteTitle").textContent = "Writing to Ed";
    let job;
    try {
      job = await pollJob(started.job_id, (j) => renderProgress("styleWrite", j, lines, started.skipped, "Writing"));
    } catch (e) {
      sw.running = false;
      toast(`Lost track of the write: ${e.message}. Reload to see what landed.`, "error");
      return;
    }
    sw.running = false;
    for (const res of job.results) st.results.set(res.user_id, res);
    const failed = job.results.filter((r) => !r.ok);
    $("styleWriteTitle").textContent = failed.length ? "Write finished with errors" : "Written to Ed";
    renderProgress("styleWrite", job, lines, started.skipped, "Wrote");
    sw.retry = lines.filter((l) => failed.some((f) => f.user_id === l.user_id)).map((l) => l.row);
    $("styleWriteFoot").innerHTML = `<span class="muted">Backup saved. “Revert last write” puts Ed's previous selections back.</span><span class="spacer"></span>
      ${failed.length ? `<button class="btn" type="button" id="retryStyleWrite">Retry ${plural(failed.length, "failed grade")}</button>` : ""}
      <button class="btn primary" type="button" data-close data-reload>Done</button>`;
    toast(failed.length ? `${job.results.length - failed.length} written, ${failed.length} failed.`
      : `${plural(job.results.length, "style grade")} written to Ed.`, failed.length ? "warn" : "ok");
  }

  // ---------- style: revert

  async function openStyleRevert() {
    const last = st.backups.find((b) => !b.reverted_at);
    if (!last) return;
    srv = { backup: last, running: false, preview: null };
    $("styleRevertTitle").textContent = "Revert last style write";
    $("styleRevertBody").innerHTML = '<div class="empty-msg">Checking Ed\'s current selections…</div>';
    $("styleRevertFoot").innerHTML = '<span class="spacer"></span><button class="btn ghost" type="button" data-close>Cancel</button>';
    $("styleRevertDialog").showModal();
    try {
      srv.preview = await api("POST", "/api/style/revert/preview", { backup_id: last.id });
      renderStyleRevert();
    } catch (e) {
      $("styleRevertBody").innerHTML = `<div class="empty-msg">${esc(e.message)}</div>`;
    }
  }

  function renderStyleRevert() {
    const { backup, rows } = srv.preview;
    const todo = rows.filter((r) => r.differs);
    const drifted = todo.filter((r) => r.changed_since).length;
    $("styleRevertBody").innerHTML = `
      <div class="info-list"><div class="info">Puts back the rubric selections Ed had right before the write on <b>${esc(when(backup.created_at))}</b>. A new backup is saved first.</div>
        ${drifted ? `<div class="warning">${plural(drifted, "student")} changed in Ed after that write. Reverting will overwrite ${drifted === 1 ? "it" : "them"}.</div>` : ""}</div>
      <table class="change-table"><thead><tr><th>Student</th><th>In Ed now</th><th></th><th>Restore to</th></tr></thead><tbody>${rows.map((r) => `<tr class="${r.differs ? "" : "unchecked"}">
        <td>${esc(r.name)}${r.changed_since ? '<span class="sub warn-text">changed in Ed since the write</span>' : ""}${r.differs ? "" : '<span class="sub">already matches</span>'}</td>
        <td>${esc(itemsLabel(r.current))}</td><td class="arrow">→</td><td class="new">${esc(itemsLabel(r.restore))}</td></tr>`).join("")}</tbody></table>`;
    $("styleRevertFoot").innerHTML = todo.length ? `
      <label class="confirm-box"><input type="checkbox" id="confirmStyleRevert"> Restore ${plural(todo.length, "student")}</label>
      <span class="spacer"></span>
      <button class="btn ghost" type="button" data-close>Cancel</button>
      <button class="btn danger" type="button" id="doStyleRevert" disabled>Revert ${plural(todo.length, "student")}</button>`
      : '<span class="muted">Ed already matches the backup.</span><span class="spacer"></span><button class="btn" type="button" data-close>Close</button>';
  }

  async function startStyleRevert() {
    const { backup, rows } = srv.preview;
    srv.running = true;
    $("doStyleRevert").disabled = true;
    let started;
    try {
      started = await api("POST", "/api/style/revert", { backup_id: backup.id });
    } catch (e) {
      srv.running = false;
      toast(e.message, "error");
      renderStyleRevert();
      return;
    }
    const lines = rows.filter((r) => r.differs).map((r) => ({
      user_id: r.user_id, name: r.name, detail: `${shortText(itemsLabel(r.current), 30)} → ${shortText(itemsLabel(r.restore), 30)}`,
    }));
    $("styleRevertTitle").textContent = "Reverting";
    let job;
    try {
      job = await pollJob(started.job_id, (j) => renderProgress("styleRevert", j, lines, [], "Restoring"));
    } catch (e) {
      srv.running = false;
      toast(`Lost track of the revert: ${e.message}`, "error");
      return;
    }
    srv.running = false;
    const failed = job.results.filter((r) => !r.ok).length;
    $("styleRevertTitle").textContent = failed ? "Revert finished with errors" : "Revert complete";
    renderProgress("styleRevert", job, lines, [], "Restored");
    $("styleRevertFoot").innerHTML = `<span class="muted">${failed ? "Run Revert again to retry the failed ones." : "Selections restored."}</span>
      <span class="spacer"></span><button class="btn primary" type="button" data-close data-reload>Done</button>`;
    st.results.clear();
    toast(failed ? `Revert: ${failed} failed.` : "Last style write reverted.", failed ? "warn" : "ok");
  }

  function bindStyleEvents() {
    document.querySelectorAll(".mode").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode)));
    $("styleSlide").addEventListener("change", updateLoadButton);
    document.querySelectorAll(".chip[data-sfilter]").forEach((c) => c.addEventListener("click", () => {
      st.filter = c.dataset.sfilter;
      renderQueue();
      renderDetail();
    }));
    $("styleSearch").addEventListener("input", (e) => { st.search = e.target.value; renderQueue(); });
    $("queueList").addEventListener("click", (e) => {
      const row = e.target.closest("[data-user]");
      if (row) showStudent(Number(row.dataset.user));
    });
    $("queueSelect").addEventListener("change", (e) => showStudent(Number(e.target.value)));

    const detail = $("styleDetail");
    detail.addEventListener("click", (e) => {
      const item = e.target.closest("[data-item]");
      if (item) { toggleItem(Number(item.dataset.item)); return; }
      const nav = e.target.closest("[data-nav]");
      if (nav) { moveStudent(Number(nav.dataset.nav)); return; }
      const tab = e.target.closest("[data-tab]");
      if (tab) { st.fileTab = Number(tab.dataset.tab); renderCode(); return; }
      const act = e.target.closest("[data-act]")?.dataset.act;
      if (act === "next") moveStudent(1);
      else if (act === "reset-draft") {
        st.drafts.delete(st.current);
        renderRubric();
        renderQueue();
        renderStyleFooter();
      } else if (act === "retry-code") {
        const sid = currentSubmissionId(currentStudent());
        st.files.delete(sid);
        renderCode();
        ensureFiles(sid);
      }
    });
    detail.addEventListener("change", (e) => {
      if (e.target.id !== "submissionSelect") return;
      const sid = Number(e.target.value);
      st.submission.set(st.current, sid);
      st.fileTab = 0;
      renderCode();
      ensureFiles(sid);
    });
    document.addEventListener("keydown", (e) => {
      if (state.mode !== "style" || !st.data || e.metaKey || e.ctrlKey || e.altKey) return;
      if (document.querySelector("dialog[open]")) return;
      if (e.target instanceof Element && e.target.closest("input, select, textarea")) return;
      const key = e.key.toLowerCase();
      if (key === "j") { e.preventDefault(); moveStudent(1); }
      else if (key === "k") { e.preventDefault(); moveStudent(-1); }
      else if (/^[1-9]$/.test(key)) {
        const item = orderedItems()[Number(key) - 1];
        if (item) { e.preventDefault(); toggleItem(item.id); }
      }
    });

    $("styleWriteBtn").addEventListener("click", openStyleWrite);
    const wd = $("styleWriteDialog");
    wd.addEventListener("change", (e) => {
      if (e.target.matches("input[data-user]")) {
        const uid = Number(e.target.dataset.user);
        if (e.target.checked) sw.selected.add(uid);
        else sw.selected.delete(uid);
        e.target.closest("tr").classList.toggle("unchecked", !e.target.checked);
        renderStyleWriteFoot();
      } else if (e.target.id === "confirmStyleWrite") {
        $("doStyleWrite").disabled = !e.target.checked;
      }
    });
    wd.addEventListener("click", (e) => {
      const pick = e.target.closest("[data-select]");
      if (pick) {
        const all = pick.dataset.select === "all";
        sw.rows.forEach((r) => (all ? sw.selected.add(r.s.user_id) : sw.selected.delete(r.s.user_id)));
        renderStyleWrite();
        return;
      }
      if (e.target.id === "doStyleWrite") startStyleWrite(sw.rows.filter((r) => sw.selected.has(r.s.user_id)));
      else if (e.target.id === "retryStyleWrite") startStyleWrite(sw.retry);
    });
    $("styleRevertBtn").addEventListener("click", openStyleRevert);
    const rvd = $("styleRevertDialog");
    rvd.addEventListener("change", (e) => {
      if (e.target.id === "confirmStyleRevert") $("doStyleRevert").disabled = !e.target.checked;
    });
    rvd.addEventListener("click", (e) => { if (e.target.id === "doStyleRevert") startStyleRevert(); });
    for (const dlg of [wd, rvd]) {
      dlg.addEventListener("close", () => {
        if (dlg.returnValue === "reload" && st.challengeId) loadStyle(st.challengeId);
        dlg.returnValue = "";
      });
    }
  }

  // ---------- app lifecycle: the packaged app quits by itself once no page checks in

  let quitting = false;

  function showStopped(text) {
    $("stoppedText").textContent = text;
    $("stoppedBanner").hidden = false;
  }

  async function heartbeat() {
    if (quitting) return;
    try {
      const res = await fetch("/api/ping", { method: "POST", headers: { "X-EzGrader-Session": SESSION }, cache: "no-store" });
      if (res.status === 403) showStopped("EzGrader was restarted. Reload this page to keep working.");
      else $("stoppedBanner").hidden = true;
    } catch {
      showStopped("EzGrader has stopped. Open it again, then reload this page.");
    }
  }

  function startHeartbeat() {
    heartbeat();
    setInterval(heartbeat, 20000);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) heartbeat(); });
  }

  async function quitApp() {
    if ((state.edits.size || st.drafts.size) && !confirm("Quit EzGrader? Your unsaved edits and drafts will be lost.")) return;
    try {
      await api("POST", "/api/quit");
    } catch (e) {
      toast(e.message, "error");
      return;
    }
    quitting = true;
    document.body.innerHTML = '<main class="quit-screen"><h1>EzGrader has quit</h1>'
      + "<p>You can close this tab. Open EzGrader again whenever you need it.</p></main>";
  }

  // ---------- start

  async function init() {
    applyTheme(storage.get(THEME_KEY));
    bindEvents();
    setMode(storage.get("ezgrader.mode") || "canvas");
    startHeartbeat();
    try {
      await refreshProfiles();
    } catch (e) {
      toast(e.message, "error");
      return;
    }
    const saved = storage.get("ezgrader.profile");
    const initial = state.profiles.find((p) => p.name === saved)?.name || state.profiles[0]?.name || null;
    if (initial) {
      await selectProfile(initial);
    } else {
      updateEmptyState();
      setOptions($("edCourse"), [], { placeholder: "No profile yet" });
      setOptions($("cvCourse"), [], { placeholder: "No profile yet" });
      openKeys();
    }
  }

  init();
})();
