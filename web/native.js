/* The native bridge exists only in the trusted loopback WKWebView. */
(() => {
  const bridge = window.webkit?.messageHandlers?.missionControl;
  if (!bridge) return;
  document.documentElement.classList.add('native-macos');
  const nav = document.querySelector('#deck-nav');
  const button = document.createElement('button');
  button.innerHTML = '<svg class="i"><use href="#i-term"/></svg><span>Runtime</span><b class="nav-count">Mac</b>';
  button.id = 'native-runtime-button';
  button.title = 'Services, processes, background operation and macOS permissions';
  button.addEventListener('click', () => bridge.postMessage({action: 'runtime'}));
  (nav || document.querySelector('aside'))?.append(button);
})();
