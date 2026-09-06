import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const template = readFileSync(new URL('../dashboard/templates/league.html', import.meta.url), 'utf8');
const leagueScript = template.split('{% block scripts %}')[1].split('{% endblock %}')[0]
  .replace(/load\(\);\s*$/, '');
const honors = JSON.parse(readFileSync(new URL('../dashboard/static/championship-honors.json', import.meta.url)));
const champion = { PLAYER_NAME: 'Andrew Hedrick', ATP_SCORE: 370.3, MAJORS_COUNTED: 14 };

function context(extra = {}) {
  return vm.createContext({ getSprite: () => null, countryDisplay: value => value, ...extra });
}

test('world and League titles remain independent when their holders differ', () => {
  const ctx = context();
  vm.runInContext(leagueScript, ctx);
  const combined = ctx.renderChampionships(champion, honors.worldChampion);
  assert.equal((combined.match(/<article /g) || []).length, 1);
  assert.match(combined, /double-champion/);
  assert.match(combined, /2026 world champion/);

  const separate = ctx.renderChampionships({ ...champion, PLAYER_NAME: 'Another Player' }, honors.worldChampion);
  const articles = separate.match(/<article[\s\S]*?<\/article>/g);
  assert.equal(articles.length, 2);
  assert.match(articles[0], /world-honor[\s\S]*Andrew Hedrick/);
  assert.doesNotMatch(articles[0], /Another Player/);
  assert.match(articles[1], /league-champion[\s\S]*Another Player/);
  assert.doesNotMatch(articles[1], /world champion/);
  assert.match(ctx.renderChampionships(null, honors.worldChampion), /world-honor/);
});

test('an unavailable honors record cannot hide the live League', async () => {
  const elements = new Map();
  const ctx = context({
    document: { getElementById(id) {
      if (!elements.has(id)) elements.set(id, { style: {}, innerHTML: '' });
      return elements.get(id);
    } },
    fetch: async url => {
      if (url.includes('championship-honors')) throw new Error('unavailable');
      return { json: async () => url.includes('leaderboard') ? [champion] : [] };
    },
  });
  vm.runInContext(leagueScript, ctx);
  await ctx.load();
  assert.match(elements.get('champion-section').innerHTML, /league-champion/);
  assert.doesNotMatch(elements.get('champion-section').innerHTML, /world champion/);
  assert.equal(elements.get('board').style.display, '');
});

test('ghost woods is the default, while explicit scene choices persist', () => {
  const script = readFileSync(new URL('../dashboard/static/atmosphere.js', import.meta.url), 'utf8');
  for (const [stored, expected] of [[null, 'night'], ['invalid', 'night'], ['garden', 'garden'], ['tide', 'tide']]) {
    const root = { dataset: {} };
    vm.runInNewContext(script, {
      document: { documentElement: root, hidden: false, addEventListener() {} },
      window: { matchMedia: () => ({ matches: true }) },
      localStorage: { getItem: key => key === 'indigo-circuit-atmosphere' ? stored : null },
    });
    assert.equal(root.dataset.atmosphere, expected);
    assert.equal(root.dataset.motion, 'paused');
  }
});
