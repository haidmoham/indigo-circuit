select
    labs_id             as tournament_id,
    name                as tournament_name,
    tournament_date,
    location,
    tier,
    season,
    tier_bonus,
    season_weight,
    _loaded_at
from {{ source('ptcg_raw', 'major_tournaments') }}
