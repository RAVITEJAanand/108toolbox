/* ==========================================================================
   108 Tools — tool-helpers.js
   Small functions that MORE THAN ONE tool needs.

   Rule of thumb: write a helper the second time you need it, not the first.
   ========================================================================== */

/* Show a small dark popup at the bottom of the screen, e.g. "Copied!" */
function showToast(message) {
  let el = document.querySelector(".toast");
  if (!el) {
    el = document.createElement("div");
    el.className = "toast";
    el.setAttribute("role", "status");     /* screen readers announce it */
    document.body.appendChild(el);
  }
  el.textContent = message;
  el.classList.add("is-visible");
  clearTimeout(el._timer);
  el._timer = setTimeout(function () { el.classList.remove("is-visible"); }, 1800);
}

/* Copy any string to the clipboard.
   navigator.clipboard needs HTTPS (or localhost), so we keep a fallback. */
function copyText(text) {
  if (!text) { showToast("Nothing to copy"); return; }

  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(
      function () { showToast("Copied!"); },
      function () { showToast("Copy failed"); }
    );
    return;
  }

  /* Old-browser fallback: a hidden textarea + execCommand */
  const temp = document.createElement("textarea");
  temp.value = text;
  temp.style.position = "fixed";
  temp.style.left = "-9999px";
  document.body.appendChild(temp);
  temp.select();
  try { document.execCommand("copy"); showToast("Copied!"); }
  catch (e) { showToast("Copy failed"); }
  document.body.removeChild(temp);
}

/* Trigger a download of a Blob (used by the image tools and JSON formatter) */
function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  /* Give the browser a moment, then release the memory */
  setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
}

/* Download a plain string as a text file */
function downloadText(text, filename, mime) {
  downloadBlob(new Blob([text], { type: mime || "text/plain" }), filename);
}

/* 1536000 -> "1.5 MB" */
function formatBytes(bytes) {
  if (bytes === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const i = Math.floor(Math.log(bytes) / Math.log(1024));
  const value = bytes / Math.pow(1024, i);
  return (value >= 10 || i === 0 ? Math.round(value) : value.toFixed(1)) + " " + units[i];
}

/* 1234567.891 -> "12,34,567.89" in India, "1,234,567.89" elsewhere.
   Intl does the local formatting for us — no manual comma logic. */
function formatNumber(value, decimals) {
  if (!isFinite(value)) return "—";
  return value.toLocaleString(undefined, {
    minimumFractionDigits: decimals || 0,
    maximumFractionDigits: decimals === undefined ? 0 : decimals
  });
}

/* Make a string safe to put inside HTML.

   Needed wherever a message is built as markup — because it carries a
   <strong> or a <br> — and part of that message came from the visitor.

   This was not hypothetical. json-formatter printed the browser's own
   JSON.parse error into an innerHTML message, and V8 quotes a piece of the
   input back inside that error. Pasting `<iframe onload=...>` therefore put
   a real iframe on the page and its handler ran. Eighteen characters.

   The rule that follows: if it is going into innerHTML and it did not come
   from this repository, it goes through here first. */
function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
