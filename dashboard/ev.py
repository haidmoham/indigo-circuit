"""
Deck EV calculator — Bayesian win-rate estimation and meta-weighted EV.

Math (see design doc 2026-05-26):
  WR ~ Beta(α, β)  — conjugate prior for Bernoulli win rate.
  Prior calibrated per archetype-matchup from aggregate historical WR
  (empirical Bayes); falls back to Beta(15,15) when no aggregate exists.
  Card EV = E[WR|with_card] - E[WR|without_card], shrunk toward 0 automatically.
  List EV = archetype_baseline_WR + Σ card_ev_deltas, meta-share weighted.
  Variance propagated end-to-end → credible intervals in the UI.

Entry point: compute_list_ev(raw_list, conn, source)
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Optional

DEFAULT_PRIOR_STRENGTH: int = 30   # phantom match count; raise = more conservative
MIN_SCOREABLE_MATCHES:  int = 20   # min real matches on either side to trust a delta
CORE_THRESHOLD:         float = 0.75  # inclusion_rate >= this → CORE (skip for EV)
MIN_ARCHETYPE_LISTS:    int = 50   # min lists for archetype to be in meta shares

MARTS = os.environ.get("DBT_MARTS_SCHEMA", "dbt_dev_marts")


def _tables(source: str) -> dict:
    """Return table names for a given source: 'online' | 'majors' | 'both'."""
    if source == "majors":
        return {
            "stats":     f"{MARTS}.major_card_archetype_stats",
            "splits":    f"{MARTS}.major_card_match_splits",
            "standings": "raw.major_standings",
        }
    # 'online' and 'both' start from online tables; 'both' adds major tables via UNION
    return {
        "stats":     f"{MARTS}.card_archetype_stats",
        "splits":    f"{MARTS}.card_match_splits",
        "standings": "raw.standings",
    }


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class PosteriorWR:
    a: float  # alpha = prior_a + wins
    b: float  # beta  = prior_b + losses

    @property
    def mean(self) -> float:
        return self.a / (self.a + self.b)

    @property
    def variance(self) -> float:
        n = self.a + self.b
        return (self.a * self.b) / (n ** 2 * (n + 1))


@dataclass
class CardMatchupEV:
    card_name:        str
    opponent_deck_id: str
    opponent_deck_name: Optional[str]
    delta:            float
    delta_std:        float    # sqrt of variance — used for CI display
    n_with:           int
    n_without:        int
    wr_with:          float
    wr_without:       float
    scoreable:        bool


@dataclass
class CardEV:
    card_name:        str
    meta_ev:          float    # meta-weighted delta
    meta_ev_std:      float
    inclusion_rate:   float    # from card_archetype_stats
    is_core:          bool
    per_matchup:      list[CardMatchupEV] = field(default_factory=list)
    scoreable:        bool = True


@dataclass
class ListEV:
    archetype_id:     str
    archetype_name:   Optional[str]
    baseline_wr:      float    # archetype's meta-weighted historical WR
    list_ev:          float    # baseline + Σ tech card deltas
    list_ev_std:      float
    per_card:         list[CardEV] = field(default_factory=list)
    unscored_cards:   list[str]   = field(default_factory=list)
    meta_coverage:    float = 0.0   # fraction of meta that had scoreable data


# ---------------------------------------------------------------------------
# Core Bayesian math
# ---------------------------------------------------------------------------

def _make_prior(baseline_wr: Optional[float],
                strength: int = DEFAULT_PRIOR_STRENGTH) -> tuple[float, float]:
    mu = max(0.05, min(0.95, baseline_wr if baseline_wr is not None else 0.5))
    return mu * strength, (1 - mu) * strength


def _posterior(wins: int, losses: int,
               prior_a: float, prior_b: float) -> PosteriorWR:
    return PosteriorWR(a=prior_a + wins, b=prior_b + losses)


def _card_matchup_ev(card_name: str, opp_id: str, opp_name: Optional[str],
                     w_with: int, l_with: int,
                     w_without: int, l_without: int,
                     baseline_wr: Optional[float] = None) -> CardMatchupEV:
    pa, pb = _make_prior(baseline_wr)
    p_w = _posterior(w_with,    l_with,    pa, pb)
    p_o = _posterior(w_without, l_without, pa, pb)
    delta    = p_w.mean - p_o.mean
    delta_var = p_w.variance + p_o.variance
    n = min(w_with + l_with, w_without + l_without)
    return CardMatchupEV(
        card_name=card_name,
        opponent_deck_id=opp_id,
        opponent_deck_name=opp_name,
        delta=delta,
        delta_std=delta_var ** 0.5,
        n_with=w_with + l_with,
        n_without=w_without + l_without,
        wr_with=p_w.mean,
        wr_without=p_o.mean,
        scoreable=(n >= MIN_SCOREABLE_MATCHES),
    )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_ptcglive(raw: str) -> list[dict]:
    """
    Parse a PTCG Live export string.

    Accepts:
        Pokémon: 12
        4 Dreepy TWM 158
        ...
        Trainer: 38
        ...
        Energy: 10
        ...

    Returns list of {"card_category", "card_name", "card_set", "card_count"}.
    """
    cards = []
    current_category: Optional[str] = None
    set_num_re = re.compile(r'\s+([A-Z]{2,6})\s+(\d+[A-Z]?)\s*$')

    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        lower = line.lower()
        if lower.startswith("pokémon") or lower.startswith("pokemon"):
            current_category = "pokemon"
            continue
        if lower.startswith("trainer"):
            current_category = "trainer"
            continue
        if lower.startswith("energy"):
            current_category = "energy"
            continue
        if not current_category:
            continue

        # "4 Dreepy TWM 158" or "4 Dreepy"
        m = re.match(r'^(\d+)\s+(.+)$', line)
        if not m:
            continue

        count     = int(m.group(1))
        name_part = m.group(2).strip()

        # Strip trailing set + number if present
        card_set = card_number = None
        sm = set_num_re.search(name_part)
        if sm:
            card_set    = sm.group(1)
            card_number = sm.group(2)
            name_part   = name_part[:sm.start()].strip()

        if name_part:
            cards.append({
                "card_category": current_category,
                "card_name":     name_part,
                "card_set":      card_set,
                "card_count":    count,
            })

    return cards


# ---------------------------------------------------------------------------
# Archetype detection
# ---------------------------------------------------------------------------

def detect_archetype(decklist: list[dict], conn,
                      source: str = "online") -> Optional[tuple[str, str, float]]:
    """
    Score submitted list against all archetypes by Pokemon CORE card overlap.
    Uses only pokemon-category CORE cards so trainer variation doesn't tank confidence.
    Returns (deck_id, deck_name, confidence_0_to_1) or None.
    """
    t = _tables(source)

    submitted_pokemon = {
        c["card_name"] for c in decklist
        if c.get("card_category") in ("pokemon", None)
    }
    if not submitted_pokemon:
        submitted_pokemon = {c["card_name"] for c in decklist}

    if source == "both":
        rows = conn.execute(f"""
            SELECT deck_id, card_name FROM {MARTS}.card_archetype_stats
            WHERE inclusion_rate >= ? AND card_category = 'pokemon'
            UNION
            SELECT deck_id, card_name FROM {MARTS}.major_card_archetype_stats
            WHERE inclusion_rate >= ? AND card_category = 'pokemon'
        """, (CORE_THRESHOLD, CORE_THRESHOLD)).fetchall()
    else:
        rows = conn.execute(f"""
            SELECT deck_id, card_name FROM {t['stats']}
            WHERE inclusion_rate >= ? AND card_category = 'pokemon'
        """, (CORE_THRESHOLD,)).fetchall()

    cores: dict[str, set] = {}
    for deck_id, card_name in rows:
        cores.setdefault(deck_id, set()).add(card_name)

    best_id = best_score = None
    for deck_id, core_cards in cores.items():
        if not core_cards:
            continue
        overlap = len(submitted_pokemon & core_cards) / len(core_cards)
        if best_score is None or overlap > best_score:
            best_id, best_score = deck_id, overlap

    if best_id is None or best_score < 0.4:
        return None

    if source == "both":
        name_row = conn.execute("""
            SELECT deck_name FROM raw.standings
            WHERE deck_id = ? AND deck_name IS NOT NULL LIMIT 1
        """, (best_id,)).fetchone()
        if not name_row:
            name_row = conn.execute("""
                SELECT deck_name FROM raw.major_standings
                WHERE deck_id = ? AND deck_name IS NOT NULL LIMIT 1
            """, (best_id,)).fetchone()
    else:
        name_row = conn.execute(f"""
            SELECT deck_name FROM {t['standings']}
            WHERE deck_id = ? AND deck_name IS NOT NULL LIMIT 1
        """, (best_id,)).fetchone()
    deck_name = name_row[0] if name_row else best_id

    return best_id, deck_name, round(best_score, 3)


# ---------------------------------------------------------------------------
# Meta shares
# ---------------------------------------------------------------------------

def _meta_shares(conn, source: str = "online") -> dict[str, float]:
    """Fraction of tournament lists per archetype for the given source."""
    if source == "both":
        rows = conn.execute("""
            SELECT deck_id, COUNT(*) as n
            FROM (
                SELECT deck_id FROM raw.standings WHERE deck_id IS NOT NULL
                UNION ALL
                SELECT deck_id FROM raw.major_standings WHERE deck_id IS NOT NULL
            ) sub
            GROUP BY deck_id
            HAVING COUNT(*) >= ?
        """, (MIN_ARCHETYPE_LISTS,)).fetchall()
    else:
        t = _tables(source)
        rows = conn.execute(f"""
            SELECT deck_id, COUNT(*) as n
            FROM {t['standings']}
            WHERE deck_id IS NOT NULL
            GROUP BY deck_id
            HAVING COUNT(*) >= ?
        """, (MIN_ARCHETYPE_LISTS,)).fetchall()

    total = sum(r[1] for r in rows)
    if total == 0:
        return {}
    return {r[0]: r[1] / total for r in rows}


def _deck_names(conn, source: str = "online") -> dict[str, str]:
    """deck_id → deck_name lookup."""
    if source == "both":
        rows = conn.execute("""
            SELECT deck_id, deck_name FROM raw.standings
            WHERE deck_id IS NOT NULL AND deck_name IS NOT NULL
            UNION
            SELECT deck_id, deck_name FROM raw.major_standings
            WHERE deck_id IS NOT NULL AND deck_name IS NOT NULL
        """).fetchall()
    else:
        t = _tables(source)
        rows = conn.execute(f"""
            SELECT DISTINCT deck_id, deck_name FROM {t['standings']}
            WHERE deck_id IS NOT NULL AND deck_name IS NOT NULL
        """).fetchall()
    return {r[0]: r[1] for r in rows}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def compute_list_ev(raw_list: str, conn,
                    archetype_id: Optional[str] = None,
                    source: str = "online") -> dict:
    """
    Full pipeline: parse → detect → query splits → Bayesian EV → return.

    Returns a JSON-serialisable dict with:
      archetype_id, archetype_name, confidence,
      baseline_wr, list_ev, list_ev_std,
      cards: [{card_name, meta_ev, meta_ev_std, inclusion_rate, is_core,
               scoreable, per_matchup: [{opp, delta, std, wr_with, wr_without,
               n_with, n_without, scoreable}]}],
      unscored_cards, meta_coverage, error (if any)
    """
    # 1. Parse
    decklist = parse_ptcglive(raw_list)
    if not decklist:
        return {"error": "Could not parse decklist — paste PTCG Live export format"}

    t = _tables(source)

    # 2. Detect or accept provided archetype
    confidence = 1.0
    arch_name  = archetype_id
    if archetype_id:
        if source == "both":
            row = conn.execute("""
                SELECT deck_name FROM raw.standings
                WHERE deck_id = ? AND deck_name IS NOT NULL LIMIT 1
            """, (archetype_id,)).fetchone()
            if not row:
                row = conn.execute("""
                    SELECT deck_name FROM raw.major_standings
                    WHERE deck_id = ? AND deck_name IS NOT NULL LIMIT 1
                """, (archetype_id,)).fetchone()
        else:
            row = conn.execute(f"""
                SELECT deck_name FROM {t['standings']}
                WHERE deck_id = ? AND deck_name IS NOT NULL LIMIT 1
            """, (archetype_id,)).fetchone()
        arch_name = row[0] if row else archetype_id
    else:
        detected = detect_archetype(decklist, conn, source=source)
        if not detected:
            return {"error": "Could not identify archetype — try selecting manually"}
        archetype_id, arch_name, confidence = detected

    # 3. Load meta shares + deck names
    meta_shares = _meta_shares(conn, source=source)
    deck_names  = _deck_names(conn, source=source)

    # 4. Load inclusion rates + avg copies for this archetype
    if source == "both":
        inclusion_rows = conn.execute(f"""
            SELECT card_name,
                   AVG(inclusion_rate)          as inclusion_rate,
                   AVG(avg_count_when_included) as avg_copies
            FROM (
                SELECT card_name, inclusion_rate, avg_count_when_included
                FROM {MARTS}.card_archetype_stats WHERE deck_id = ?
                UNION ALL
                SELECT card_name, inclusion_rate, avg_count_when_included
                FROM {MARTS}.major_card_archetype_stats WHERE deck_id = ?
            ) sub
            GROUP BY card_name
        """, (archetype_id, archetype_id)).fetchall()
    else:
        inclusion_rows = conn.execute(f"""
            SELECT card_name, inclusion_rate, avg_count_when_included
            FROM {t['stats']}
            WHERE deck_id = ?
        """, (archetype_id,)).fetchall()
    inclusion  = {r[0]: r[1] for r in inclusion_rows}
    avg_copies = {r[0]: r[2] for r in inclusion_rows}

    # submitted copy counts keyed by card name (last wins for dupes, which can't happen)
    submitted_counts = {c["card_name"]: c["card_count"] for c in decklist}

    # 5. Archetype baseline WR (meta-weighted, no card filter)
    submitted_cards = list({c["card_name"] for c in decklist})
    if not submitted_cards:
        return {"error": "No cards found in decklist"}

    placeholders = ",".join("?" * len(submitted_cards))

    if source == "both":
        baseline_rows = conn.execute(f"""
            SELECT opponent_deck_id,
                   SUM(w_with + w_without)::float /
                   NULLIF(SUM(w_with + l_with + w_without + l_without), 0) as agg_wr
            FROM (
                SELECT opponent_deck_id, w_with, l_with, w_without, l_without
                FROM {MARTS}.card_match_splits WHERE deck_id = ?
                UNION ALL
                SELECT opponent_deck_id, w_with, l_with, w_without, l_without
                FROM {MARTS}.major_card_match_splits WHERE deck_id = ?
            ) sub
            GROUP BY opponent_deck_id
        """, (archetype_id, archetype_id)).fetchall()
    else:
        baseline_rows = conn.execute(f"""
            SELECT opponent_deck_id,
                   SUM(w_with + w_without)::float /
                   NULLIF(SUM(w_with + l_with + w_without + l_without), 0) as agg_wr
            FROM {t['splits']}
            WHERE deck_id = ?
            GROUP BY opponent_deck_id
        """, (archetype_id,)).fetchall()
    agg_wr_by_opp = {r[0]: r[1] for r in baseline_rows}

    baseline_wr = sum(
        agg_wr_by_opp.get(opp, 0.5) * share
        for opp, share in meta_shares.items()
    ) or 0.5

    # 6. Load splits for all cards in submitted list
    if source == "both":
        split_rows = conn.execute(f"""
            SELECT card_name, opponent_deck_id,
                   SUM(w_with) as w_with, SUM(l_with) as l_with,
                   SUM(w_without) as w_without, SUM(l_without) as l_without
            FROM (
                SELECT card_name, opponent_deck_id, w_with, l_with, w_without, l_without
                FROM {MARTS}.card_match_splits
                WHERE deck_id = ? AND card_name IN ({placeholders})
                UNION ALL
                SELECT card_name, opponent_deck_id, w_with, l_with, w_without, l_without
                FROM {MARTS}.major_card_match_splits
                WHERE deck_id = ? AND card_name IN ({placeholders})
            ) sub
            GROUP BY card_name, opponent_deck_id
        """, [archetype_id] + submitted_cards + [archetype_id] + submitted_cards).fetchall()
    else:
        split_rows = conn.execute(f"""
            SELECT card_name, opponent_deck_id,
                   w_with, l_with, w_without, l_without
            FROM {t['splits']}
            WHERE deck_id = ?
              AND card_name IN ({placeholders})
        """, [archetype_id] + submitted_cards).fetchall()

    # Index splits by (card_name, opp_deck_id)
    splits: dict[tuple, tuple] = {}
    for row in split_rows:
        splits[(row[0], row[1])] = row[2:]   # w_with, l_with, w_without, l_without

    # 7. Compute per-card EV
    card_evs: list[CardEV] = []
    unscored: list[str]    = []
    total_ev = total_var   = 0.0
    scored_meta_weight     = 0.0

    for card_name in submitted_cards:
        rate      = inclusion.get(card_name)
        is_core   = rate is not None and rate >= CORE_THRESHOLD

        matchups: list[CardMatchupEV] = []
        card_meta_ev = card_meta_var = 0.0
        card_scored_weight = 0.0

        for opp_id, share in meta_shares.items():
            key = (card_name, opp_id)
            if key not in splits:
                continue
            w_with, l_with, w_without, l_without = splits[key]
            baseline = agg_wr_by_opp.get(opp_id)
            mu = _card_matchup_ev(
                card_name, opp_id, deck_names.get(opp_id),
                w_with, l_with, w_without, l_without,
                baseline_wr=baseline,
            )
            matchups.append(mu)
            if mu.scoreable:
                card_meta_ev  += mu.delta   * share
                card_meta_var += (mu.delta_std ** 2) * (share ** 2)
                card_scored_weight += share

        if not matchups or card_scored_weight < 0.3:
            if not is_core:
                unscored.append(card_name)
            # CORE cards silently skipped — they contribute zero delta by definition
            continue

        ev = CardEV(
            card_name=card_name,
            meta_ev=card_meta_ev,
            meta_ev_std=card_meta_var ** 0.5,
            inclusion_rate=rate if rate is not None else 0.0,
            is_core=is_core,
            per_matchup=sorted(matchups, key=lambda m: -abs(m.delta)),
            scoreable=card_scored_weight >= 0.5,
        )
        card_evs.append(ev)

        if not is_core:
            total_ev  += card_meta_ev
            total_var += card_meta_var
            scored_meta_weight = max(scored_meta_weight, card_scored_weight)

    # Sort cards: biggest absolute meta EV first, unscored CORE last
    card_evs.sort(key=lambda c: (c.is_core, -abs(c.meta_ev)))

    # 8. Serialise
    opponent_archetypes = sorted(
        [
            {"id": opp, "name": deck_names.get(opp, opp), "share": round(share, 4)}
            for opp, share in meta_shares.items()
        ],
        key=lambda x: -x["share"],
    )

    return {
        "archetype_id":   archetype_id,
        "archetype_name": arch_name,
        "confidence":     confidence,
        "baseline_wr":    round(baseline_wr, 4),
        "list_ev":        round(baseline_wr + total_ev, 4),
        "list_ev_std":    round(total_var ** 0.5, 4),
        "meta_coverage":  round(scored_meta_weight, 3),
        "unscored_cards": unscored,
        "opponent_archetypes": opponent_archetypes,
        "cards": [
            {
                "card_name":       c.card_name,
                "meta_ev":         round(c.meta_ev, 4),
                "meta_ev_std":     round(c.meta_ev_std, 4),
                "inclusion_rate":  round(c.inclusion_rate, 3),
                "submitted_count": submitted_counts.get(c.card_name, 1),
                "avg_copies":      round(avg_copies.get(c.card_name) or 0, 1),
                "is_core":         c.is_core,
                "scoreable":       c.scoreable,
                "per_matchup": [
                    {
                        "opponent_deck_id":   m.opponent_deck_id,
                        "opponent_deck_name": m.opponent_deck_name,
                        "delta":      round(m.delta, 4),
                        "delta_std":  round(m.delta_std, 4),
                        "wr_with":    round(m.wr_with, 4),
                        "wr_without": round(m.wr_without, 4),
                        "n_with":     m.n_with,
                        "n_without":  m.n_without,
                        "scoreable":  m.scoreable,
                    }
                    for m in c.per_matchup
                ],
            }
            for c in card_evs
        ],
    }
