/* ==========================================================================
   108 Tools — regex-worker.js
   Used by tools/regex-tester.html only. It runs one pattern over one text,
   away from the page.

   Why a separate thread: a pattern such as (a+)+$ against text that nearly
   matches takes effectively forever, and JavaScript cannot interrupt a
   regular expression once it has started. Run on the page, it froze the
   whole tab for half a minute. Run here, the page stays usable, and after
   its time limit the page ends this worker and starts a fresh one.
   ========================================================================== */

/* ---- START: running one pattern and sending back what it found ----
   The same loop the page used to run itself, including the lastIndex nudge:
   a pattern that can match nothing returns a zero-length match and leaves
   lastIndex where it was, so the next call would find the same nothing in
   the same place, forever. A match goes back as plain data (its place, its
   text, its groups), because the page cannot receive the match object. */
self.onmessage = function (event) {
  var job = event.data;
  var re;
  try {
    re = new RegExp(job.source, job.flags);
  } catch (error) {
    self.postMessage({ id: job.id, error: error.message });
    return;
  }

  var started = performance.now();
  var found = [];
  var m;
  if (!re.global) {
    m = re.exec(job.subject);
    if (m) { found.push(m); }
  } else {
    re.lastIndex = 0;
    while ((m = re.exec(job.subject)) !== null) {
      found.push(m);
      if (m.index === re.lastIndex) { re.lastIndex += 1; }
      if (found.length >= job.max) { break; }
    }
  }
  var took = performance.now() - started;

  self.postMessage({
    id: job.id,
    took: took,
    found: found.map(function (one) {
      return {
        index: one.index,
        parts: Array.prototype.slice.call(one),
        named: one.groups ? Object.assign({}, one.groups) : null
      };
    })
  });
};
/* ---- END: running one pattern and sending back what it found ---- */
