# Design Notes

## Elite 4 + Champion mechanic

Seasonal ranking where rank 1 holds the title of **Champion** and ranks 2–5 are the **Elite 4**.
Titles shift in real time as tournaments resolve. Community engagement hook.

### Scoring formula (sports-inspired, implement in order)

---

#### 1. ATP-style seasonal points
*Powers the Champion/Elite 4 ladder. Pure SQL in dbt — implement first.*

```
points = base_points[round_reached] × field_size_multiplier × event_tier_bonus
```

- **field_size_multiplier**: `log(field_size) / log(baseline_field)`
  Logarithmic scaling — a 1800-player Regional is worth more than a 50-player online event,
  but not 36× more. Choose `baseline_field = 200` as a reasonable mid-tier event.

- **event_tier_bonus** (mirrors ATP Grand Slam → Masters → 500 → 250):
  | Tier | Bonus |
  |---|---|
  | Worlds | 3.0 |
  | International Championship | 2.0 |
  | Regional Championship | 1.5 |
  | Special Event | 1.1 |
  | Online / Limitless Showdown | 1.0 |

- **Best N of season counted**, not cumulative — prevents padding via small-event grinding.
  N = TBD, but ~8–10 is reasonable for a full season.

- **Narrative hook**: TPCi's CP system ignores field size entirely. This formula is a
  demonstrably better version of their own system — good story for the community.

**dbt model**: `marts/seasonal_rankings.sql` + `marts/title_history.sql` (snapshots rank 1–5
  whenever the top changes after each ingest run).

---

#### 2. Glicko-2 rating
*True skill estimate with confidence interval. Python script → Snowflake table → dbt reads it. Implement second.*

Glicko-2 extends ELO with:
- **Rating deviation (RD)**: how confident we are in the rating. High RD = few games played,
  wide confidence band. Low RD = many games, tight band.
- **Volatility (σ)**: how erratic the player's results are. High σ = streaky; low σ = consistent.

Community-facing display: "Estimated Skill Range: 1840–1920" — immediately legible to anyone
familiar with chess ratings.

**Implementation**: `ingest/glicko.py` — processes `stg_matches` chronologically, outputs a
`RAW.PTCG.GLICKO_RATINGS` table. dbt staging model reads it in.

Reference: http://www.glicko.net/glicko/glicko2.pdf (the original Glicko-2 paper, 6 pages, very readable)

---

#### 3. WAR-equivalent placement score
*"Above replacement" metric. Pure dbt — implement third.*

Instead of raw placement, show **placement vs. expected placement given field size**.
A player who consistently finishes top 15% across all field sizes — small events and
large Regionals alike — is more impressive than one who dominates small events but
struggles when the field is deep.

```sql
-- sketch
placement_percentile = 1 - (placing / player_count)
above_replacement    = placement_percentile - avg(placement_percentile) over (partition by tournament_id)
player_war           = avg(above_replacement) over (partition by player_username)
```

Novel to the PTCG community — good differentiator from existing tools.

**dbt model**: column added to `marts/players.sql` once foundation data is validated.
