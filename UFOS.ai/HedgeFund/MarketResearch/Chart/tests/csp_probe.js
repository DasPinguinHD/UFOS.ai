/* TEST ONLY — loaded by run_tests.js as an external 'self' script to check that the page CSP
   blocks eval-like code for page scripts (DevTools-evaluated code is exempt from CSP). */
(function () {
  'use strict';
  var r = {};
  try { r.eval = String(window['ev' + 'al']('1+1')); } catch (e) { r.eval = 'blocked'; }
  try { r.fn = String(new Function('return 2')()); } catch (e) { r.fn = 'blocked'; }
  window.__probe = r;
})();
