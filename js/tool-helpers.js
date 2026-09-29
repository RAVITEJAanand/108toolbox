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

/* ---- START: building a ZIP file, with no library ----
   Two tools need to hand back many files at once - the favicon set and the
   image splitter - and a browser will not let a page trigger a dozen
   downloads in a row without asking the visitor about each.

   This writes the simplest legal ZIP: every file stored as it is, nothing
   compressed. That is exactly right for PNG and JPEG, which are compressed
   already and do not shrink further, and it keeps this to a page of code
   instead of a library.

   files: [{ name: "a.png", bytes: Uint8Array }]. Returns a Blob.
   Names are written as UTF-8 and flagged so, or every non-English file name
   would come out as mojibake in the visitor's unzip program. */
var ZIP_CRC_TABLE = null;

function zipCrc32(bytes) {
  if (!ZIP_CRC_TABLE) {
    ZIP_CRC_TABLE = new Uint32Array(256);
    for (var n = 0; n < 256; n++) {
      var c = n;
      for (var k = 0; k < 8; k++) { c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1); }
      ZIP_CRC_TABLE[n] = c >>> 0;
    }
  }
  var crc = 0xFFFFFFFF;
  for (var i = 0; i < bytes.length; i++) {
    crc = ZIP_CRC_TABLE[(crc ^ bytes[i]) & 0xFF] ^ (crc >>> 8);
  }
  return (crc ^ 0xFFFFFFFF) >>> 0;
}

function buildZip(files) {
  var encoder = new TextEncoder();
  var now = new Date();
  /* MS-DOS date and time: two-second resolution, years counted from 1980. */
  var dosDate = ((now.getFullYear() - 1980) << 9) | ((now.getMonth() + 1) << 5) | now.getDate();
  var dosTime = (now.getHours() << 11) | (now.getMinutes() << 5) | (now.getSeconds() >> 1);

  var parts = [];
  var central = [];
  var offset = 0;

  files.forEach(function (file) {
    var name = encoder.encode(file.name);
    var crc = zipCrc32(file.bytes);
    var size = file.bytes.length;

    var local = new DataView(new ArrayBuffer(30));
    local.setUint32(0, 0x04034B50, true);
    local.setUint16(4, 20, true);
    local.setUint16(6, 0x0800, true);          /* bit 11: the name is UTF-8 */
    local.setUint16(8, 0, true);               /* method 0: stored */
    local.setUint16(10, dosTime, true);
    local.setUint16(12, dosDate, true);
    local.setUint32(14, crc, true);
    local.setUint32(18, size, true);
    local.setUint32(22, size, true);
    local.setUint16(26, name.length, true);
    local.setUint16(28, 0, true);

    var entry = new DataView(new ArrayBuffer(46));
    entry.setUint32(0, 0x02014B50, true);
    entry.setUint16(4, 20, true);
    entry.setUint16(6, 20, true);
    entry.setUint16(8, 0x0800, true);
    entry.setUint16(10, 0, true);
    entry.setUint16(12, dosTime, true);
    entry.setUint16(14, dosDate, true);
    entry.setUint32(16, crc, true);
    entry.setUint32(20, size, true);
    entry.setUint32(24, size, true);
    entry.setUint16(28, name.length, true);
    entry.setUint32(42, offset, true);          /* where its local header sits */

    parts.push(local.buffer, name, file.bytes);
    central.push(entry.buffer, name);
    offset += 30 + name.length + size;
  });

  var centralSize = 0;
  central.forEach(function (piece) { centralSize += piece.byteLength || piece.length; });

  var end = new DataView(new ArrayBuffer(22));
  end.setUint32(0, 0x06054B50, true);
  end.setUint16(8, files.length, true);
  end.setUint16(10, files.length, true);
  end.setUint32(12, centralSize, true);
  end.setUint32(16, offset, true);

  return new Blob(parts.concat(central, [end.buffer]), { type: "application/zip" });
}
/* ---- END: building a ZIP file, with no library ---- */
