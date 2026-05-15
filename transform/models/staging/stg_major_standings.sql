select
    labs_tournament_id  as tournament_id,
    player_id,
    player_name,
    country,
    placing,
    wins,
    losses,
    ties,
    deck_id,
    deck_name,
    deck_sprite,
    _loaded_at
from {{ source('ptcg_raw', 'major_standings') }}
where placing is not null
