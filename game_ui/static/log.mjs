/* ============================================================ GlobeRunners UI — game log (turn -> stopover -> both players' lines) */
"use strict";

import { $ } from "./utils.mjs";
import { CARDPOOL, cardImg } from "./cards.mjs";
import { game } from "./game.mjs";
import { makeStaticCard } from "./zones.mjs";
import { showCardHover, hideCardHover, showCardModal } from "./modals.mjs";
import { playChainAnim } from "./chainanim.mjs";

// readable label for a card's effect (uses the card pool: effect + effect_number)
function effectLabel(id) {
  const row = CARDPOOL[id];
  if (!row || !row.effect) return null;
  const n = (row.effect_number != null) ? (Math.abs(parseInt(row.effect_number, 10)) || 0) : 0;
  switch (row.effect) {
    case 'advancing': return `advancing +${n}`;
    case 'backward': return `recoil ${n}`;
    case 'advancing_oppo': return `opponent +${n}`;
    case 'backward_oppo': return `opponent −${n}`;
    case 'draw': return `draw ${n}`;
    case 'draw_oppo': return `opponent draws ${n}`;
    case 'discard': return `discard ${n}`;
    case 'discard_oppo': return `opponent discards ${n}`;
    case 'ramp': return `ramp ${n}`;
    case 'ramp_oppo': return `opponent +${n} mana`;
    case 'taxation': return `taxation ${n}`;
    case 'taxation_oppo': return `opponent −${n} mana`;
    case 'pet_trap': return 'trap — drop token here';
    default: return row.effect;
  }
}

// "stopover_4" (1st slot of the turn) -> "stopover 1" (the UI's numbering 1..5)
function stopoverLabel(sv) {
  const m = /^stopover_(\d+)/.exec(sv || '');
  if (!m) return String(sv || 'stopover');
  const num = parseInt(m[1], 10);
  return `stopover ${((5 - (num % 5)) % 5) || 5}`;
}

// mini card (hover = 3x preview, click = zoom modal)
function makeLogCardEl(id) {
  const el = document.createElement('span');
  el.className = 'log-card';
  el.title = (CARDPOOL[id] && CARDPOOL[id].name) || id;
  el.innerHTML = `<img src="${cardImg(id)}" alt="" loading="lazy" onerror="this.onerror=null;this.src='/placeholder.svg'">`;
  el.addEventListener('mouseenter', () => showCardHover(id));
  el.addEventListener('mouseleave', hideCardHover);
  el.addEventListener('click', () => showCardModal(id));
  return el;
}

function makeTag(cls, text) {
  const t = document.createElement('span');
  t.className = 'log-tag ' + cls;
  t.textContent = text;
  return t;
}

// one player's line: [order] [name] [pos] [cards] [condition tag] [effect] [negatives] -> [final pos]
function buildLogEntryEl(e) {
  const div = document.createElement('div');
  div.className = 'log-entry';
  const flow = document.createElement('div');
  flow.className = 'log-flow';

  const ord = document.createElement('span');
  ord.className = 'log-ord';
  ord.textContent = e.order === 1 ? '1st' : (e.order === 2 ? '2nd' : '');
  const name = document.createElement('span');
  name.className = 'log-name';
  name.textContent = e.player;
  flow.append(ord, name);

  const p0 = document.createElement('span');
  p0.className = 'log-pos';
  p0.textContent = `pos ${e.pos_before}`;
  flow.append(p0);

  for (const id of (e.cards || [])) flow.append(makeLogCardEl(id));

  const negatives = e.negatives || [];
  const blocked = negatives.some((s) => /blocked/.test(s));

  if (e.mode === 'defend') {
    // defend line: no condition is ever evaluated -> shield tag instead
    flow.append(makeTag('shield', `⛨ shield ${e.shield ?? 0}`));
  } else {
    // condition tag: green = met, red = not met, gray = no condition / not evaluated
    if (e.condition_met === true) flow.append(makeTag('met', '✓ condition'));
    else if (e.condition_met === false) flow.append(makeTag('unmet', '✗ condition'));
    else if (!blocked) flow.append(makeTag('shield', 'no condition'));
    // effect tag (dimmed when the effect never fired: blocked or canceled)
    const eff = effectLabel((e.cards || [])[0]);
    if (eff) {
      const dim = /blocked|canceled/.test(negatives.join(' '));
      flow.append(makeTag('effect' + (dim ? ' dim' : ''), eff));
    }
    for (const s of negatives) flow.append(makeTag('neg', s));
  }

  const pf = document.createElement('span');
  pf.className = 'log-pos final';
  pf.textContent = `→ ${e.pos_after != null ? e.pos_after : e.pos_before}`;
  flow.append(pf);

  div.append(flow);
  for (const note of (e.notes || [])) {
    const n = document.createElement('div');
    n.className = 'log-note';
    n.textContent = note;
    div.append(n);
  }
  return div;
}

function buildLogTurnEl(t, open) {
  const det = document.createElement('details');
  det.className = 'log-turn';
  det.open = !!open;
  const sum = document.createElement('summary');
  sum.textContent = `Turn ${t.turn}`;
  det.append(sum);
  for (const sv of (t.stopovers || [])) {
    const sd = document.createElement('details');
    sd.className = 'log-sv';
    sd.open = true;
    const ss = document.createElement('summary');
    ss.textContent = stopoverLabel(sv.stopover);
    sd.append(ss);
    for (const e of (sv.entries || [])) sd.append(buildLogEntryEl(e));
    det.append(sd);
  }
  return det;
}

// The log panel is pinned to the RIGHT edge of the row (absolute, see CSS) while the
// board + trip-chain are centered — measure the free space between them and cap the
// panel's width so the centered group is never overlapped, whatever the window width.
function fitLogWidth() {
  const panel = $('#log-panel');
  if (!panel || panel.classList.contains('hidden')) return;
  const row = $('#board-wrap');
  const rr = row && row.getBoundingClientRect();
  if (!rr || !rr.width) return;
  let groupRight = 0;
  for (const el of [$('#board-stage'), $('#stopovers')]) {
    if (!el) continue;
    const b = el.getBoundingClientRect();
    if (b.width) groupRight = Math.max(groupRight, b.right);
  }
  // rr.right is the border-box edge; the panel (absolute, right:0) sits inside the
  // 18px right padding -> reserve that padding + a 20px visual gap before the panel.
  const avail = rr.right - 18 - 20 - groupRight;
  panel.style.maxWidth = Math.max(170, Math.min(avail, 340)) + 'px';
}
window.addEventListener('resize', () => fitLogWidth());

// incremental: only newly-resolved turns are appended (existing collapsed state is
// preserved across the 2.5s polling re-renders); on the first render (or a rejoin)
// every turn appears, all collapsed except the latest one
export function renderLog(st) {
  const log = (st && st.log) || [];
  const panel = $('#log-panel');
  if (!log.length) { panel.classList.add('hidden'); return; }
  panel.classList.remove('hidden');
  const body = $('#log-body');
  const start = game.logTurnsRendered;
  let added = false;
  while (game.logTurnsRendered < log.length) {
    body.append(buildLogTurnEl(log[game.logTurnsRendered], true));
    game.logTurnsRendered++;
    added = true;
  }
  fitLogWidth();   // cap the (right-pinned) panel to the free space left of the centered board + trip-chain
  if (start === 0 && game.logTurnsRendered > 1) {
    // first render with several turns: collapse all but the latest
    body.querySelectorAll(':scope > .log-turn').forEach((d, i) => {
      if (i < game.logTurnsRendered - 1) d.open = false;
    });
  }
  // auto-reveal the latest turn only when it's new — otherwise keep the
  // user's scroll position (the panel scrolls internally, see #board-wrap)
  if (added) body.scrollTop = body.scrollHeight;
  // trip-chain animation: a turn that just resolved (a GENUINELY new log turn,
  // not a rejoin/first render) -> pulse the slots in the exact resolution order
  if (added && start > 0) playChainAnim(log[log.length - 1]);
}
