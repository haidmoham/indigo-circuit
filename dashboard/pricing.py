"""
Deck prices + budget swaps.

Budget swap logic for the most expensive cards in an archetype:
  1. Same card, cheaper printing (reprint) — strictly equivalent for play:
     Trainers/Energy by name, Pokémon only when card text matches (func_key).
  2. Different, cheaper Trainer/Energy whose *shrunk* win-rate effect in this
     archetype is statistically indistinguishable from the expensive card's:
     the 80% credible intervals of the two effects overlap. That is weak
     evidence ("we can't tell them apart"), never proof they are equivalent,
     and the UI says so. Pokémon are never substituted (evolution lines /
     attacks make cross-card swaps meaningless).

Effects reuse the empirical-Bayes machinery in ev.py: Beta prior centred on
the archetype's baseline WR, posterior delta = E[WR|with] - E[WR|without].
"""
from __future__ import annotations

import os
from dataclasses import dataclass, asdict
from typing import Optional

from .ev import _card_matchup_ev, MIN_SCOREABLE_MATCHES

MARTS = os.environ.get("DBT_MARTS_SCHEMA", "dbt_dev_marts")
Z80 = 1.2816


@dataclass
class Effect:
    card_name: str
    delta: float
    std: float
    n_with: int
    n_without: int

    @property
    def lo(self) -> float:
        return self.delta - Z80 * self.std

    @property
    def hi(self) -> float:
        return self.delta + Z80 * self.std

    @property
    def scoreable(self) -> bool:
        return min(self.n_with, self.n_without) >= MIN_SCOREABLE_MATCHES


def pooled_effect(card_name: str, w_with: int, l_with: int, w_without: int, l_without: int,
                  baseline_wr: Optional[float]) -> Effect:
    ev = _card_matchup_ev(card_name, "*", None, w_with, l_with, w_without, l_without,
                          baseline_wr=baseline_wr)
    return Effect(card_name, ev.delta, ev.delta_std, ev.n_with, ev.n_without)


def intervals_overlap(a: Effect, b: Effect) -> bool:
    return a.lo <= b.hi and b.lo <= a.hi


def overlap_fraction(a: Effect, b: Effect) -> float:
    """Length of the intersection over the length of the narrower interval (0..1)."""
    inter = min(a.hi, b.hi) - max(a.lo, b.lo)
    if inter <= 0:
        return 0.0
    narrower = min(a.hi - a.lo, b.hi - b.lo) or 1e-9
    return min(1.0, inter / narrower)


def list_cost(lines: list[dict], price_of) -> dict:
    """Pure helper mirroring deck_list_prices: lines = [{name, set, number, count}],
    price_of(line) -> float | None. Unpriced copies are counted, never guessed."""
    total = priced = 0
    cost = 0.0
    for ln in lines:
        c = int(ln.get("count") or 0)
        total += c
        p = price_of(ln)
        if p is None:
            continue
        priced += c
        cost += c * float(p)
    return {"cost_usd": round(cost, 2), "total_cards": total, "priced_cards": priced,
            "unpriced_cards": total - priced, "is_lower_bound": priced < total}


def median_iqr(values: list[float]) -> Optional[dict]:
    v = sorted(x for x in values if x is not None)
    if not v:
        return None

    def q(p):
        k = (len(v) - 1) * p
        f = int(k)
        c = min(f + 1, len(v) - 1)
        return v[f] + (v[c] - v[f]) * (k - f)
    return {"median": round(q(0.5), 2), "p25": round(q(0.25), 2), "p75": round(q(0.75), 2), "n": len(v)}


def suggest_swaps(cards: list[dict], effects: dict[str, Effect], top_n: int = 6,
                  max_subs: int = 3, min_saving: float = 0.5) -> list[dict]:
    """cards: rows from archetype_card_prices (lowercase keys).
    Returns one entry per expensive card with reprint + substitute suggestions."""
    priced = [c for c in cards if c.get("median_listed_usd") is not None]
    expensive = sorted(priced, key=lambda c: -(c.get("expected_cost_usd") or 0))[:top_n]
    out = []
    for e in expensive:
        price = float(e["median_listed_usd"])
        entry = {"card_name": e["card_name"], "card_category": e["card_category"],
                 "median_listed_usd": price, "avg_copies": e.get("avg_copies"),
                 "inclusion_rate": e.get("inclusion_rate"),
                 "expected_cost_usd": e.get("expected_cost_usd"),
                 "reprint": None, "substitutes": [], "effect": None, "note": None}
        ch = e.get("cheapest_equiv_usd")
        if ch is not None and price - float(ch) >= min_saving and e.get("cheapest_card_key") != e.get("typical_card_key"):
            entry["reprint"] = {"card_key": e.get("cheapest_card_key"), "variant": e.get("cheapest_variant"),
                                "price_usd": float(ch), "saves_per_copy": round(price - float(ch), 2)}
        eff = effects.get(e["card_name"])
        if eff:
            entry["effect"] = _effect_dict(eff)
        if e["card_category"] == "pokemon":
            entry["note"] = "pokémon are only swapped for identical reprints"
        elif eff is None:
            entry["note"] = "too few matches with and without this card to estimate its effect"
        elif not eff.scoreable:
            entry["note"] = (f"only {min(eff.n_with, eff.n_without)} matches on one side "
                             f"({eff.n_with} with / {eff.n_without} without) — too few to compare "
                             f"substitutes honestly")
        else:
            cands = []
            for c in priced:
                if c["card_name"] == e["card_name"] or c["card_category"] != e["card_category"]:
                    continue
                cp = c.get("cheapest_equiv_usd")
                cp = float(cp) if cp is not None else float(c["median_listed_usd"])
                if price - cp < min_saving:
                    continue
                ce = effects.get(c["card_name"])
                if not ce or not ce.scoreable or not intervals_overlap(eff, ce):
                    continue
                cands.append({"card_name": c["card_name"], "price_usd": round(cp, 2),
                              "saves_per_copy": round(price - cp, 2),
                              "overlap": round(overlap_fraction(eff, ce), 2),
                              "effect": _effect_dict(ce)})
            cands.sort(key=lambda x: (-x["overlap"], -x["saves_per_copy"]))
            entry["substitutes"] = cands[:max_subs]
        out.append(entry)
    return out


def _effect_dict(e: Effect) -> dict:
    return {"delta": round(e.delta, 4), "lo": round(e.lo, 4), "hi": round(e.hi, 4),
            "n_with": e.n_with, "n_without": e.n_without, "scoreable": e.scoreable}


# ---------------------------------------------------------------------------
# DB-backed helpers (used by app.py)
# ---------------------------------------------------------------------------

def load_effects(query, deck_id: str, source: str = "online") -> dict[str, Effect]:
    table = f"{MARTS}.major_card_match_splits" if source == "majors" else f"{MARTS}.card_match_splits"
    rows = query(f"""
        SELECT card_name, SUM(w_with) w_with, SUM(l_with) l_with,
               SUM(w_without) w_without, SUM(l_without) l_without
        FROM {table} WHERE deck_id = ? GROUP BY card_name""", (deck_id,))
    if not rows:
        return {}
    # Baseline: every split row partitions the same deck matches, so the card
    # with the most total matches gives the closest-to-complete archetype WR.
    best = max(rows, key=lambda r: (r["W_WITH"] + r["L_WITH"] + r["W_WITHOUT"] + r["L_WITHOUT"]))
    n = best["W_WITH"] + best["L_WITH"] + best["W_WITHOUT"] + best["L_WITHOUT"]
    baseline = (best["W_WITH"] + best["W_WITHOUT"]) / n if n else None
    return {r["CARD_NAME"]: pooled_effect(r["CARD_NAME"], int(r["W_WITH"]), int(r["L_WITH"]),
                                           int(r["W_WITHOUT"]), int(r["L_WITHOUT"]), baseline)
            for r in rows}


def lower_keys(rows):
    return [{k.lower(): v for k, v in r.items()} for r in rows]


__all__ = ["Effect", "pooled_effect", "intervals_overlap", "overlap_fraction", "list_cost",
           "median_iqr", "suggest_swaps", "load_effects", "lower_keys", "asdict"]
