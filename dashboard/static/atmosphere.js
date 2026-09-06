/* Scene preferences are separate from scouting filters and never fetch data. */
(() => {
  'use strict';
  const root = document.documentElement;
  const scenes = ['garden', 'night', 'tide'];
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)');
  const read = key => { try { return localStorage.getItem(key); } catch (_) { return null; } };
  const save = (key, value) => { try { localStorage.setItem(key, value); } catch (_) { /* Preferences remain usable when storage is disabled. */ } };
  const storedScene = read('indigo-circuit-atmosphere');
  root.dataset.atmosphere = scenes.includes(storedScene) ? storedScene : 'night';
  // A reduced-motion system preference starts still, even after a prior visit.
  let moving = !reduced.matches && read('indigo-circuit-motion') !== 'paused';
  root.dataset.motion = moving ? 'running' : 'paused';
  root.dataset.documentHidden = String(document.hidden);

  document.addEventListener('DOMContentLoaded', () => {
    const buttons = document.querySelectorAll('[data-scene-choice]');
    const motion = document.getElementById('atmosphere-motion');
    const label = document.getElementById('atmosphere-motion-label');
    function reflect() {
      root.dataset.motion = moving ? 'running' : 'paused';
      buttons.forEach(button => button.setAttribute('aria-pressed', String(button.dataset.sceneChoice === root.dataset.atmosphere)));
      // aria-pressed reports whether decorative motion is enabled.
      motion.setAttribute('aria-pressed', String(moving));
      motion.setAttribute('aria-label', moving ? 'pause atmosphere motion' : 'play atmosphere motion');
      label.textContent = moving ? 'pause motion' : 'play motion';
    }
    buttons.forEach(button => button.addEventListener('click', () => {
      root.dataset.atmosphere = button.dataset.sceneChoice;
      save('indigo-circuit-atmosphere', root.dataset.atmosphere);
      reflect();
    }));
    motion.addEventListener('click', () => {
      moving = !moving;
      save('indigo-circuit-motion', moving ? 'running' : 'paused');
      reflect();
    });
    reduced.addEventListener('change', event => {
      if (event.matches) { moving = false; reflect(); }
    });
    reflect();
  });
  document.addEventListener('visibilitychange', () => {
    root.dataset.documentHidden = String(document.hidden);
  });
})();
