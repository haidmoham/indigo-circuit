select
    player_username,
    rating,
    rating_deviation,
    volatility,
    rating_low,
    rating_high,
    games_played,
    _loaded_at
from {{ source('ptcg_raw', 'glicko_ratings') }}
