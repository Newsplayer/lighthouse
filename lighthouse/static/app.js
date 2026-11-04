// While a fetch or Claude run is going, poll /status and reload once it finishes.
(function () {
  var el = document.getElementById("status");
  if (!el || el.dataset.running !== "1") return;
  var timer = setInterval(function () {
    fetch("/status").then(function (r) { return r.json(); }).then(function (s) {
      if (s.fetch.running) {
        el.textContent = s.fetch.phase === "index" ? "Updating the job index. The list updates when it finishes."
          : "Fetching boards (" + s.fetch.done + "/" + s.fetch.total + "). The list updates when it finishes.";
      } else if (s.claude.running) {
        el.textContent = "Claude: " + s.claude.message;
      } else {
        clearInterval(timer);
        location.reload();
      }
    }).catch(function () { clearInterval(timer); });
  }, 3000);
})();
