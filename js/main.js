/* ==========================================================================
   108 Tools — main.js
   Runs on index.html and tools.html. Needs tools-data.js loaded first.

   Three jobs:
     1. renderGrid()   — paint tool cards into a container
     2. wireSearch()   — filter those cards as the visitor types
     3. wireBurger()   — open/close the mobile menu

   Nothing here knows about any specific tool. That is on purpose: this file
   should still work untouched when there are 108 tools.
   ========================================================================== */

/* Where are we? Tool pages sit one folder deep, so links need "../".
   Every other page is at the root. */
const AT_ROOT = !window.location.pathname.includes("/tools/");
const BASE = AT_ROOT ? "" : "../";

/* --------------------------------------------------------------------------
   Build the HTML for one card.
   The whole card is a single <a> so the entire rectangle is clickable
   and reachable with the Tab key.
   -------------------------------------------------------------------------- */
function cardHTML(tool) {
  return (
    /* data-cat is what gives the card its category colour. style.css maps
       each category to an accent; without this attribute the card falls
       back to the brand indigo, which still looks fine. */
    '<a class="card" data-cat="' + tool.category + '"' +
       ' href="' + BASE + 'tools/' + tool.slug + '.html">' +
      '<span class="card__icon" aria-hidden="true">' + tool.icon + '</span>' +
      '<h3 class="card__title">' + tool.name + '</h3>' +
      '<p class="card__desc">' + tool.desc + '</p>' +
      '<span class="badge">' + tool.category + '</span>' +
    '</a>'
  );
}

/* --------------------------------------------------------------------------
   Paint a list of tools into a container element.
   -------------------------------------------------------------------------- */
function renderGrid(containerId, list) {
  const box = document.getElementById(containerId);
  if (!box) return;                     // page does not have this grid — fine

  if (list.length === 0) {
    box.innerHTML = '<p class="empty">No tool matches that. Try "word", "image" or "json".</p>';
    return;
  }
  box.innerHTML = list.map(cardHTML).join("");
}

/* --------------------------------------------------------------------------
   Does this tool match what the visitor typed?
   We check the name, the description, the category and the keywords, so
   someone typing "how old am i" still finds the Age Calculator.
   -------------------------------------------------------------------------- */
function matches(tool, query) {
  if (!query) return true;
  const haystack = [
    tool.name,
    tool.desc,
    tool.category,
    tool.keywords.join(" ")
  ].join(" ").toLowerCase();
  return haystack.indexOf(query) !== -1;
}

/* --------------------------------------------------------------------------
   Wire up the search box + the category chips together.
   Both filters apply at the same time.
   -------------------------------------------------------------------------- */
function wireSearch(inputId, chipsId, gridId) {
  const input = document.getElementById(inputId);
  const chips = document.getElementById(chipsId);
  let activeCategory = "All";

  function apply() {
    const query = input ? input.value.trim().toLowerCase() : "";
    const list = TOOLS.filter(function (tool) {
      const catOk = activeCategory === "All" || tool.category === activeCategory;
      return catOk && matches(tool, query);
    });
    renderGrid(gridId, list);
  }

  if (input) {
    input.addEventListener("input", apply);
  }

  if (chips) {
    /* Build the chips from the CATEGORIES list so you never hand-edit them.

       A category with nothing in it yet gets no chip. CATEGORIES is allowed
       to run ahead of what is actually built - it is the plan, not the
       inventory - and a chip that opens an empty grid is worse than no chip,
       because the visitor assumes the site is broken rather than unfinished.
       The chip appears by itself the day the first tool in that category is
       registered. */
    const filled = CATEGORIES.filter(function (cat) {
      return TOOLS.some(function (t) { return t.category === cat; });
    });

    chips.innerHTML = ["All"].concat(filled).map(function (cat, i) {
      return '<button class="chip' + (i === 0 ? " is-active" : "") +
             '" type="button" data-cat="' + cat + '">' + cat + '</button>';
    }).join("");

    chips.addEventListener("click", function (event) {
      const button = event.target.closest(".chip");
      if (!button) return;
      activeCategory = button.dataset.cat;
      /* Move the active class */
      chips.querySelectorAll(".chip").forEach(function (c) {
        c.classList.toggle("is-active", c === button);
      });
      apply();
    });
  }

  apply();   // first paint
}

/* --------------------------------------------------------------------------
   Light / dark theme toggle

   Three possible states:
     - no choice saved  -> follow the operating system (the CSS does this)
     - "light" saved    -> always light, even on a dark machine
     - "dark"  saved    -> always dark, even on a light machine

   The choice is written to localStorage, so it is remembered on the next
   visit. It lives only in that browser, which is exactly right for a per-
   person preference.
   -------------------------------------------------------------------------- */
function currentTheme() {
  /* An explicit choice wins; otherwise ask the operating system. */
  var stamped = document.documentElement.getAttribute("data-theme");
  if (stamped === "light" || stamped === "dark") return stamped;
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark" : "light";
}

function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  try { localStorage.setItem("theme", theme); }
  catch (e) { /* private browsing: it just will not be remembered */ }

  var button = document.querySelector(".theme-toggle");
  if (button) {
    button.setAttribute("aria-label",
      theme === "dark" ? "Switch to light theme" : "Switch to dark theme");
  }
}

function wireThemeToggle() {
  var button = document.querySelector(".theme-toggle");
  if (!button) return;

  /* Label it correctly on load, without changing anything */
  button.setAttribute("aria-label",
    currentTheme() === "dark" ? "Switch to light theme" : "Switch to dark theme");

  button.addEventListener("click", function () {
    applyTheme(currentTheme() === "dark" ? "light" : "dark");
  });
}

/* --------------------------------------------------------------------------
   Mobile menu toggle
   -------------------------------------------------------------------------- */
function wireBurger() {
  const burger = document.querySelector(".nav__burger");
  const panel = document.querySelector(".nav__mobile");
  if (!burger || !panel) return;

  burger.addEventListener("click", function () {
    const open = panel.classList.toggle("is-open");
    burger.setAttribute("aria-expanded", open ? "true" : "false");
  });
}

/* --------------------------------------------------------------------------
   "Related tools" strip at the bottom of a tool page.
   Shows up to 3 other tools from the same category.
   -------------------------------------------------------------------------- */
function renderRelated(containerId, currentSlug) {
  const me = TOOLS.find(function (t) { return t.slug === currentSlug; });
  if (!me) return;

  let list = TOOLS.filter(function (t) {
    return t.slug !== currentSlug && t.category === me.category;
  });
  /* If the category is thin, top up with anything else */
  if (list.length < 3) {
    const extra = TOOLS.filter(function (t) {
      return t.slug !== currentSlug && list.indexOf(t) === -1;
    });
    list = list.concat(extra);
  }
  renderGrid(containerId, list.slice(0, 3));
}

/* --------------------------------------------------------------------------
   Tool counts

   "15 tools live" used to be typed by hand into three separate pages, and it
   went stale every single time a batch shipped - twice it was wrong on the
   live site while the grid below it showed the real number. The registry is
   the source of truth for everything else on this site, so it is the source
   of truth for the count as well.

   Any element carrying data-tool-count is filled in automatically:
     data-tool-count="live"       how many tools exist right now
     data-tool-count="remaining"  how many of the 108 are still to come
   -------------------------------------------------------------------------- */
const TOOL_TARGET = 108;

function renderCounts() {
  const nodes = document.querySelectorAll("[data-tool-count]");
  Array.prototype.forEach.call(nodes, function (el) {
    const which = el.getAttribute("data-tool-count");
    el.textContent = which === "remaining"
      ? TOOL_TARGET - TOOLS.length
      : TOOLS.length;
  });
}

/* --------------------------------------------------------------------------
   Go
   -------------------------------------------------------------------------- */
document.addEventListener("DOMContentLoaded", function () {
  wireThemeToggle();
  wireBurger();
  renderCounts();

  /* Homepage: only the popular tools */
  if (document.getElementById("popularGrid")) {
    renderGrid("popularGrid", TOOLS.filter(function (t) { return t.popular; }));
  }

  /* Homepage hero search sends you to tools.html carrying the query */
  const heroSearch = document.getElementById("heroSearch");
  if (heroSearch && !document.getElementById("allGrid")) {
    heroSearch.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && heroSearch.value.trim()) {
        window.location.href = "tools.html?q=" + encodeURIComponent(heroSearch.value.trim());
      }
    });
    /* Live-filter the popular grid too, so typing does something immediately */
    heroSearch.addEventListener("input", function () {
      const q = heroSearch.value.trim().toLowerCase();
      const base = q ? TOOLS : TOOLS.filter(function (t) { return t.popular; });
      renderGrid("popularGrid", base.filter(function (t) { return matches(t, q); }));
    });
  }

  /* Tools page: full grid + search + chips */
  if (document.getElementById("allGrid")) {
    const fromUrl = new URLSearchParams(window.location.search).get("q");
    const box = document.getElementById("toolSearch");
    if (fromUrl && box) box.value = fromUrl;
    wireSearch("toolSearch", "chips", "allGrid");
  }

  /* Tool page: related strip. The page sets window.CURRENT_TOOL. */
  if (window.CURRENT_TOOL) {
    renderRelated("relatedGrid", window.CURRENT_TOOL);
  }
});
