-- players + Glicko-2 ratings joined.
-- Dashboard should query this model, not players directly.
select
    p.*,
    g.rating                as glicko_rating,
    g.rating_deviation      as glicko_rd,
    g.volatility            as glicko_volatility,
    g.rating_low            as glicko_rating_low,
    g.rating_high           as glicko_rating_high
from {{ ref('players') }} p
left join {{ ref('stg_glicko_ratings') }} g
  on p.player_username = g.player_username
