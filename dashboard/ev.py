"""
Deck EV calculator — Bayesian win-rate estimation and meta-weighted EV.

Design doc (see conversation 2026-05-26):
  - Each win rate modelled as Beta(α, β); conjugate prior for Bernoulli.
  - Prior calibrated per archetype-matchup from aggregate historical WR
    (empirical Bayes); falls back to Beta(15, 15) when no aggregate exists.
  - Card EV = posterior_mean(with_card) - posterior_mean(without_card),
    shrunk automatically toward 0 when sample sizes are small.
  - Meta-weighted list EV = archetype_baseline_wr + Σ card_ev_deltas,
    weighted by opponent archetype meta share.
  - Variance propagated through all steps → credible intervals surfaced in UI.

NOT YET WIRED UP — implement once majors match data is populated.
Entry point for the optimizer feature will be `list_ev(decklist, source)`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# scipy is an optional dep until this module is active; guard the import
try:
    from scipy.stats import beta as beta_dist
    _SCIPY = True
except ImportError:
    _SCIPY = False


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

#: Phantom match count for the default prior — equivalent to 30 matches at 50%.
#: Increase to be more conservative with sparse cards; decrease to trust data faster.
DEFAULT_PRIOR_STRENGTH: int = 30

#: Minimum real matches (with OR without card) required before the delta is
#: considered scoreable. Below this the credible interval is too wide to act on.
MIN_SCOREABLE_MATCHES: int = 20


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class PosteriorWR:
    """Posterior win-rate estimate for one side of a card split."""
    wins:   int
    losses: int
    prior_a: float
    prior_b: float

    @property
    def a(self) -> float:
        return self.prior_a + self.wins

    @property
    def b(self) -> float:
        return self.prior_b + self.losses

    @property
    def mean(self) -> float:
        return self.a / (self.a + self.b)

    @property
    def variance(self) -> float:
        n = self.a + self.b
        return (self.a * self.b) / (n ** 2 * (n + 1))

    def credible_interval(self, mass: float = 0.90) -> tuple[float, float]:
        """Return (lo, hi) highest-density interval."""
        if not _SCIPY:
            raise RuntimeError("scipy required for credible intervals")
        lo = (1 - mass) / 2
        hi = 1 - lo
        return beta_dist.ppf(lo, self.a, self.b), beta_dist.ppf(hi, self.a, self.b)


@dataclass
class CardMatchupEV:
    """EV of a single card against a single opponent archetype."""
    card_name:        str
    opponent_deck_id: str
    delta:            float          # posterior_mean(with) - posterior_mean(without)
    delta_variance:   float
    n_with:           int
    n_without:        int
    scoreable:        bool           # False when sample too small to trust

    @property
    def ci_halfwidth(self) -> float:
        return self.delta_variance ** 0.5 * 1.645  # ~90% interval half-width


@dataclass
class CardEV:
    """Meta-weighted EV of a single card across all opponent archetypes."""
    card_name:       str
    meta_weighted_ev: float
    meta_weighted_variance: float
    per_matchup:     list[CardMatchupEV] = field(default_factory=list)
    scoreable:       bool = True     # False if too few scoreable matchups

    @property
    def ci_halfwidth(self) -> float:
        return self.meta_weighted_variance ** 0.5 * 1.645


@dataclass
class ListEV:
    """Meta-weighted EV for a full submitted decklist."""
    archetype_id:       str
    baseline_wr:        float        # archetype historical meta-weighted WR
    list_ev:            float        # baseline + Σ card deltas
    list_ev_variance:   float
    per_card:           list[CardEV] = field(default_factory=list)
    unscored_cards:     list[str]    = field(default_factory=list)  # no data

    @property
    def ci_halfwidth(self) -> float:
        return self.list_ev_variance ** 0.5 * 1.645


# ---------------------------------------------------------------------------
# Core math
# ---------------------------------------------------------------------------

def make_prior(baseline_wr: Optional[float] = None,
               strength: int = DEFAULT_PRIOR_STRENGTH) -> tuple[float, float]:
    """
    Return (alpha, beta) for the Beta prior.

    If baseline_wr is provided (archetype's known aggregate WR vs this opponent),
    the prior mean is set to that value (empirical Bayes).
    Otherwise falls back to 0.5.
    """
    mu = baseline_wr if baseline_wr is not None else 0.5
    mu = max(0.05, min(0.95, mu))   # clamp away from degenerate extremes
    alpha = mu * strength
    beta  = (1 - mu) * strength
    return alpha, beta


def posterior_wr(wins: int, losses: int,
                 prior_a: float, prior_b: float) -> PosteriorWR:
    """Construct a posterior win-rate estimate from observed data + prior."""
    return PosteriorWR(wins=wins, losses=losses,
                       prior_a=prior_a, prior_b=prior_b)


def card_matchup_ev(card_name: str,
                    opponent_deck_id: str,
                    w_with: int, l_with: int,
                    w_without: int, l_without: int,
                    baseline_wr: Optional[float] = None) -> CardMatchupEV:
    """
    Compute the posterior EV delta for one card vs one opponent archetype.

    Both the with-card and without-card win rates get the same prior so the
    delta shrinks toward 0 as sample sizes decrease.
    """
    prior_a, prior_b = make_prior(baseline_wr)

    p_with    = posterior_wr(w_with,    l_with,    prior_a, prior_b)
    p_without = posterior_wr(w_without, l_without, prior_a, prior_b)

    delta          = p_with.mean - p_without.mean
    delta_variance = p_with.variance + p_without.variance   # independence approx
    n              = min(w_with + l_with, w_without + l_without)
    scoreable      = n >= MIN_SCOREABLE_MATCHES

    return CardMatchupEV(
        card_name=card_name,
        opponent_deck_id=opponent_deck_id,
        delta=delta,
        delta_variance=delta_variance,
        n_with=w_with + l_with,
        n_without=w_without + l_without,
        scoreable=scoreable,
    )


def meta_weighted_card_ev(card_name: str,
                           matchups: list[CardMatchupEV],
                           meta_shares: dict[str, float]) -> CardEV:
    """
    Collapse per-matchup deltas into a single meta-weighted EV for a card.

    meta_shares: {deck_id: share} where shares sum to ~1.0.
    Only scoreable matchups contribute; unscored matchups are skipped
    (conservative — treats unknown matchups as zero delta).
    """
    weighted_ev  = 0.0
    weighted_var = 0.0
    scored_weight = 0.0

    for m in matchups:
        share = meta_shares.get(m.opponent_deck_id, 0.0)
        if not m.scoreable or share == 0.0:
            continue
        weighted_ev  += m.delta   * share
        weighted_var += m.delta_variance * share ** 2
        scored_weight += share

    # If we only scored a fraction of the meta, flag it
    scoreable = scored_weight >= 0.5   # at least half the meta is represented

    return CardEV(
        card_name=card_name,
        meta_weighted_ev=weighted_ev,
        meta_weighted_variance=weighted_var,
        per_matchup=matchups,
        scoreable=scoreable,
    )


# ---------------------------------------------------------------------------
# List-level EV  (main entry point — stubbed, not yet wired to DB)
# ---------------------------------------------------------------------------

def list_ev(decklist: list[dict],
            archetype_id: str,
            source: str = "majors") -> ListEV:
    """
    Compute meta-weighted EV for a submitted decklist.

    Args:
        decklist:     parsed card list, each entry {"card_name": str, "card_count": int, ...}
        archetype_id: detected or user-confirmed archetype (e.g. "dragapult-ex")
        source:       "online" | "majors" | "both" — which match data to draw from

    Returns:
        ListEV with baseline WR, total list EV, per-card breakdown, and unscored cards.

    TODO (implement when majors data lands):
      1. Load archetype baseline WR vs each opponent from DB
      2. Load meta shares from major_standings (or online standings for source=online)
      3. For each non-CORE card in decklist, query match splits (w_with/l_with etc.)
         from major_matches + major_decklists JOIN
      4. Call card_matchup_ev() per card per opponent
      5. Call meta_weighted_card_ev() per card
      6. Sum card EVs over non-CORE slots → list_ev
      7. CORE cards contribute zero delta by definition (run in every list)
    """
    raise NotImplementedError(
        "list_ev() is stubbed — implement once major_matches data is populated. "
        "See dashboard/ev.py docstring for full design."
    )


def detect_archetype(decklist: list[dict],
                     known_archetypes: list[dict]) -> Optional[str]:
    """
    Score the submitted list against known archetypes by CORE card overlap.
    Returns the best-matching archetype_id, or None if confidence is too low.

    TODO: implement using card_archetype_stats inclusion_rate >= 0.75 as CORE definition.
    known_archetypes: list of {deck_id, core_cards: [card_name, ...]}
    """
    raise NotImplementedError("detect_archetype() stubbed")


def parse_ptcglive(raw: str) -> list[dict]:
    """
    Parse a PTCG Live export string into a card list.

    Expected format:
        Pokémon: 12
        4 Dreepy TWM 158
        ...
        Trainer: 38
        ...
        Energy: 10
        ...

    Returns list of {"card_category", "card_name", "card_set", "card_count"}.

    TODO: wire up — parser logic already exists in ingest/labs.py (_parse_card_text).
    Refactor shared logic to a common module (ingest/cards.py) so both
    labs.py and ev.py can import it without circular deps.
    """
    raise NotImplementedError("parse_ptcglive() stubbed")
