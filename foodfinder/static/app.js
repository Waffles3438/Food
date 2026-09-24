(() => {
  const $ = (selector, parent = document) => parent.querySelector(selector);
  const $$ = (selector, parent = document) => [...parent.querySelectorAll(selector)];
  const state = { view: "upcoming", clubs: [], refreshTimer: null, toastTimer: null, clubFilter: "", clubRefreshRun: "" };

  async function api(path, options = {}) {
    const response = await fetch(path, {
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
    });
    if (!response.ok) {
      let message = `Request failed (${response.status})`;
      try { message = (await response.json()).detail || message; } catch (_) {}
      throw new Error(message);
    }
    return response.status === 204 ? null : response.json();
  }

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function badge(label, style = "") {
    return node("span", `badge ${style}`.trim(), label);
  }

  function showToast(message) {
    const toast = $("#toast");
    toast.textContent = message;
    toast.classList.add("visible");
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => toast.classList.remove("visible"), 2800);
  }

  async function loadAll() {
    await Promise.allSettled([loadEvents(), loadClubs(), loadStatus()]);
  }

  function eventURL() {
    const query = new URLSearchParams({ view: state.view });
    const club = $("#club-filter").value;
    const from = $("#from-filter").value;
    const to = $("#to-filter").value;
    if (club) query.set("club", club);
    if (from) query.set("from_date", from);
    if (to) query.set("to_date", to);
    if ($("#free-entry").checked) query.set("free_entry", "true");
    if ($("#unrestricted-entry").checked) query.set("unrestricted_entry", "true");
    return `/api/events?${query}`;
  }

  async function loadEvents() {
    const list = $("#events");
    list.replaceChildren(Object.assign(node("div", "loading-card"), { textContent: "Loading events…" }));
    try {
      const events = await api(eventURL());
      list.replaceChildren();
      $("#event-count").textContent = `${events.length} ${events.length === 1 ? "event" : "events"}`;
      if (state.view === "upcoming") $("#stat-upcoming").textContent = events.length;
      if (!events.length) {
        const empty = node("div", "empty-state");
        empty.append(node("div", "empty-icon", state.view === "review" ? "✦" : "🥡"));
        const title = state.view === "review" ? "You’re all caught up." : state.view === "today" ? "No untimed events for today." : "No food events found yet.";
        empty.append(node("strong", "", title));
        empty.append(node("span", "", state.view === "upcoming" ? "Start a scan, or check back after clubs post their next event." : "Try another tab or broaden your filters."));
        list.append(empty);
      } else {
        events.forEach((event) => list.append(renderEvent(event)));
      }
    } catch (error) {
      list.replaceChildren(node("div", "empty-state", `Could not load events: ${error.message}`));
    }
  }

  function formatDate(value) {
    if (!value) return { mon: "TBD", day: "—", full: "Date needs review" };
    const date = new Date(`${value}T12:00:00`);
    return {
      mon: new Intl.DateTimeFormat("en-CA", { month: "short", timeZone: "America/Toronto" }).format(date),
      day: new Intl.DateTimeFormat("en-CA", { day: "2-digit", timeZone: "America/Toronto" }).format(date),
      full: new Intl.DateTimeFormat("en-CA", { weekday: "short", month: "short", day: "numeric", year: "numeric", timeZone: "America/Toronto" }).format(date),
    };
  }

  function formatTime(value) {
    if (!value) return "";
    const [hourText, minute] = value.split(":");
    const hour = Number(hourText);
    return `${hour % 12 || 12}:${minute} ${hour >= 12 ? "PM" : "AM"}`;
  }

  function makeSourceLink(source, index) {
    const label = source.kind === "story" ? `@${source.username} · Story${source.excerpt ? "" : ""}` : `@${source.username} · Instagram`;
    const link = node("a", "", label || `Source ${index + 1}`);
    link.href = source.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    const expires = source.expires_at ? new Date(source.expires_at) : null;
    if (source.kind === "story" && (!expires || expires < new Date())) {
      link.title = "Story links expire; saved evidence may still be available.";
    } else if (source.kind === "story" && expires) {
      link.title = `Story expires ${expires.toLocaleString()}`;
    }
    return link;
  }

  function renderEvent(event) {
    const card = node("article", "event-card");
    const top = node("div", "event-top");
    const d = formatDate(event.event_date);
    const dateTile = node("div", `date-tile ${event.event_date ? "" : "unknown"}`);
    dateTile.append(node("span", "", d.mon), node("strong", "", d.day));
    top.append(dateTile);

    const main = node("div", "event-main");
    main.append(node("p", "club-label", (event.club_names || event.username || "U of T club").split(",").map((v) => v.trim()).filter(Boolean).join(" · ")));
    main.append(node("h3", "event-title", event.title || "Club event"));
    const description = node("div", "event-desc");
    if (event.event_date) description.append(node("span", "", `◷ ${d.full}${event.event_time ? ` · ${formatTime(event.event_time)}` : ""}`));
    if (event.location) description.append(node("span", "", `⌖ ${event.location}`));
    if (event.food) description.append(node("span", "", `✳ ${event.food}`));
    if (!event.food && event.food_confidence === "low") description.append(node("span", "", "Food offer needs confirmation"));
    main.append(description);
    const badges = node("div", "event-badges");
    if (event.food && event.food_confidence !== "low") badges.append(badge("Complimentary food", "badge-food"));
    else if (event.food) badges.append(badge("Check food details", "badge-review"));
    if (event.entry_cost === "Free entry") badges.append(badge("Free entry", "badge-food"));
    else if (event.entry_cost) badges.append(badge(`Entry: ${event.entry_cost}`, "badge-paid"));
    else badges.append(badge("Entry cost not listed", "badge-saved"));
    if (event.eligibility) badges.append(badge(event.eligibility, "badge-restricted"));
    if (event.rsvp && !event.restrictions.includes("RSVP")) badges.append(badge(event.rsvp, "badge-rsvp"));
    if (event.source_kind === "story") badges.append(badge("Instagram Story", "badge-story"));
    if (event.review_reason) badges.append(badge("Needs a look", "badge-review"));
    main.append(badges);
    top.append(main);

    const actions = node("div", "event-actions");
    const edit = node("button", "icon-button", "✎");
    edit.type = "button";
    edit.title = "Correct event details";
    edit.setAttribute("aria-label", `Correct ${event.title}`);
    edit.addEventListener("click", () => toggleEditor(card, event));
    actions.append(edit);
    if (state.view === "review") {
      const hide = node("button", "icon-button", "×");
      hide.type = "button";
      hide.title = "Dismiss from this list";
      hide.setAttribute("aria-label", "Dismiss event");
      hide.addEventListener("click", async () => {
        try { await api(`/api/events/${event.id}`, { method: "PATCH", body: JSON.stringify({ status: "dismissed" }) }); showToast("Event dismissed."); loadAll(); }
        catch (error) { showToast(error.message); }
      });
      actions.append(hide);
    }
    top.append(actions);
    card.append(top);

    if (event.review_reason) card.append(node("p", "source-caption", event.review_reason));
    if (event.source_kind === "story") {
      const img = document.createElement("img");
      img.className = "evidence-image";
      img.src = `/api/events/${encodeURIComponent(event.id)}/evidence`;
      img.alt = `Saved Instagram Story evidence for ${event.title}`;
      img.loading = "lazy";
      img.addEventListener("error", () => img.remove(), { once: true });
      card.append(img);
    }
    const sources = event.supporting_sources || [];
    if (sources.length) {
      const sourceRow = node("div", "source-links");
      sourceRow.append(node("span", "muted", sources.length > 1 ? "Announcements:" : "Source:"));
      sources.slice(0, 8).forEach((source, index) => sourceRow.append(makeSourceLink(source, index)));
      if (sources.length > 8) sourceRow.append(node("span", "muted", `+${sources.length - 8} more`));
      card.append(sourceRow);
      const excerpt = sources.find((source) => source.excerpt)?.excerpt;
      if (excerpt) card.append(node("p", "source-caption", excerpt));
    }
    return card;
  }

  function toggleEditor(card, event) {
    const existing = $(".event-editor", card);
    if (existing) { existing.remove(); return; }
    const form = node("form", "event-editor");
    const fields = [
      ["title", "Event title", event.title || "", "text"],
      ["event_date", "Date", event.event_date || "", "date"],
      ["event_time", "Time", event.event_time || "", "time"],
      ["location", "Location", event.location || "", "text"],
      ["food", "Complimentary food", event.food || "", "text"],
      ["entry_cost", "Entry info (Free entry or e.g. $8 tickets)", event.entry_cost || "", "text"],
      ["restrictions", "Eligibility / restrictions", event.restrictions || "", "text"],
      ["eligibility", "Who may attend", event.eligibility || "", "text"],
      ["rsvp", "RSVP", event.rsvp || "", "text"],
    ];
    fields.forEach(([name, label, value, type]) => {
      const wrapper = node("label");
      wrapper.append(node("span", "", label));
      const input = document.createElement("input");
      input.name = name; input.type = type; input.value = value;
      wrapper.append(input); form.append(wrapper);
    });
    const actions = node("div", "editor-actions");
    const cancel = node("button", "button button-light", "Cancel"); cancel.type = "button";
    cancel.addEventListener("click", () => form.remove());
    const save = node("button", "button button-green", "Save correction"); save.type = "submit";
    actions.append(cancel, save); form.append(actions);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const patch = Object.fromEntries(new FormData(form).entries());
      patch.status = "upcoming"; patch.review_reason = "";
      try { await api(`/api/events/${event.id}`, { method: "PATCH", body: JSON.stringify(patch) }); showToast("Correction saved."); loadAll(); }
      catch (error) { showToast(error.message); }
    });
    card.append(form);
  }

  async function loadClubs() {
    try {
      state.clubs = await api("/api/clubs");
      const select = $("#club-filter");
      const previous = select.value;
      select.replaceChildren(new Option("All clubs", ""));
      state.clubs.filter((club) => club.active).forEach((club) => select.add(new Option(club.name, club.id)));
      select.value = previous;
      const search = $("#club-search").value.toLocaleLowerCase();
      const visible = state.clubs.filter((club) => !search || `${club.name} ${club.instagram_username}`.toLocaleLowerCase().includes(search));
      const container = $("#clubs");
      container.replaceChildren();
      visible.forEach((club) => container.append(renderClub(club)));
      $("#club-directory-count").textContent = `${state.clubs.length} club listings`;
      $("#stat-clubs").textContent = state.clubs.filter((club) => club.instagram_username && club.active).length;
    } catch (error) { showToast(`Could not load clubs: ${error.message}`); }
  }

  function renderClub(club) {
    const row = node("div", "club-row");
    row.append(node("span", "club-name", club.name));
    const handle = node("span", `club-handle ${club.instagram_username ? "" : "missing"}`, club.instagram_username ? `@${club.instagram_username}` : "Instagram account missing");
    row.append(handle);
    const status = club.instagram_username ? `${club.check_status === "never" ? "Not checked yet" : (club.check_message || club.check_status)}${club.last_checked ? ` · ${new Date(club.last_checked).toLocaleString()}` : ""}` : "Add the club’s Instagram handle to include it in scans.";
    const stat = node("span", "club-status", "");
    const dot = node("i", `status-dot ${club.check_status || "never"}`); stat.append(dot, document.createTextNode(status)); row.append(stat);
    const edit = node("button", "icon-button", "✎"); edit.type = "button"; edit.title = "Edit club / Instagram handle";
    edit.setAttribute("aria-label", `Edit ${club.name}`);
    edit.addEventListener("click", () => openClubForm(club));
    const toggle = node("button", "button button-light club-toggle", club.active ? "Pause" : "Include");
    toggle.type = "button";
    toggle.title = club.active ? "Pause Instagram scans for this club" : "Include this club in Instagram scans";
    toggle.addEventListener("click", async () => {
      try { await api(`/api/clubs/${club.id}`, { method: "PATCH", body: JSON.stringify({ active: !club.active }) }); showToast(club.active ? "Club paused." : "Club included in scans."); await loadClubs(); }
      catch (error) { showToast(error.message); }
    });
    const actions = node("div", "club-actions"); actions.append(edit, toggle); row.append(actions);
    if (!club.active) row.classList.add("muted");
    return row;
  }

  async function loadStatus() {
    try {
      const progress = await api("/api/scan");
      const run = progress.run;
      if (run?.kind === "full" && run.id && run.message !== "Discovering clubs" && state.clubRefreshRun !== run.id) {
        state.clubRefreshRun = run.id;
        await loadClubs();
      }
      const indicator = $("#sync-indicator");
      indicator.classList.toggle("busy", progress.running);
      indicator.lastElementChild.textContent = progress.running ? "Scan in progress" : run?.finished_at ? `Updated ${new Date(run.finished_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}` : "Ready to scan";
      const total = Object.values(progress.accounts || {}).reduce((sum, value) => sum + value, 0);
      const checked = (progress.accounts?.ok || 0) + (progress.accounts?.partial || 0);
      $("#stat-checked").textContent = total ? `${checked} / ${total}` : "0";
      const review = await api("/api/events?view=review");
      $("#stat-review").textContent = review.length;
      $("#tab-review").textContent = review.length;
      const today = await api("/api/events?view=today");
      $("#tab-today").textContent = today.length;
      $("#tab-upcoming").textContent = $("#stat-upcoming").textContent || "0";
      const missing = progress.missing_handles || 0;
      let message = run?.message || "No scan has run yet. Start one to discover clubs and check Instagram.";
      if (progress.running && run) message = `${run.message || "Scanning"} · ${run.accounts_checked || 0} of ${run.accounts_total || 0} accounts checked · ${run.posts_seen || 0} posts read.`;
      if (!progress.running && run?.status) message += ` ${run.accounts_checked || 0} accounts checked, ${run.posts_seen || 0} posts read, ${run.events_found || 0} events identified.`;
      if (missing) message += ` ${missing} club listings still need an Instagram handle.`;
      if (progress.accounts?.error) message += ` ${progress.accounts.error} accounts need attention.`;
      $("#scan-message").textContent = message;
    } catch (_) { $("#scan-message").textContent = "The local scan status is not available yet."; }
  }

  function openClubForm(club = null) {
    const form = $("#club-form");
    form.classList.remove("hidden");
    $("#club-edit-id").value = club?.id || "";
    $("#club-name").value = club?.name || "";
    $("#club-username").value = club?.instagram_username || "";
    $("#club-website").value = club?.website || "";
    $("#club-name").focus();
  }

  function init() {
    $$(".tab").forEach((tab) => tab.addEventListener("click", () => {
      state.view = tab.dataset.view;
      $$(".tab").forEach((item) => item.classList.toggle("active", item === tab));
      loadEvents();
    }));
    $("#filters").addEventListener("input", loadEvents);
    $("#filters").addEventListener("reset", () => setTimeout(loadEvents, 0));
    $("#scan-now").addEventListener("click", async (event) => {
      event.currentTarget.disabled = true;
      try { const result = await api("/api/scan", { method: "POST", body: "{}" }); showToast(result.message); await loadStatus(); }
      catch (error) { showToast(error.message); }
      finally { setTimeout(() => { event.currentTarget.disabled = false; }, 1200); }
    });
    $("#refresh-clubs").addEventListener("click", async (event) => {
      event.currentTarget.disabled = true;
      try { const result = await api("/api/scan", { method: "POST", body: JSON.stringify({ discover: true }) }); showToast(result.message); }
      catch (error) { showToast(error.message); }
      finally { event.currentTarget.disabled = false; }
    });
    $("#add-club").addEventListener("click", () => openClubForm());
    $("#cancel-club").addEventListener("click", () => $("#club-form").classList.add("hidden"));
    $("#club-search").addEventListener("input", loadClubs);
    $("#club-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const id = $("#club-edit-id").value;
      const body = JSON.stringify({ name: $("#club-name").value, instagram_username: $("#club-username").value, website: $("#club-website").value });
      try {
        await api(id ? `/api/clubs/${id}` : "/api/clubs", { method: id ? "PATCH" : "POST", body });
        $("#club-form").classList.add("hidden"); showToast("Club saved."); await loadClubs();
      } catch (error) { showToast(error.message); }
    });
    loadAll();
    state.refreshTimer = setInterval(loadStatus, 5000);
  }
  document.addEventListener("DOMContentLoaded", init, { once: true });
})();
