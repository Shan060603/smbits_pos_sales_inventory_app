(function () {
  function initInactivityLogout(timeoutSeconds) {
    var timeoutMs = Math.max(1, Number(timeoutSeconds || 30)) * 1000;
    var timerId = null;

    function triggerLogout() {
      window.location.assign("/logout");
    }

    function resetTimer() {
      if (timerId) {
        clearTimeout(timerId);
      }
      timerId = setTimeout(triggerLogout, timeoutMs);
    }

    var events = ["mousemove", "mousedown", "keydown", "touchstart", "scroll", "click"];
    events.forEach(function (eventName) {
      window.addEventListener(eventName, resetTimer, { passive: true });
    });

    resetTimer();
  }

  window.initInactivityLogout = initInactivityLogout;
})();
