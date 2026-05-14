select
    tournament_id,
    player_username,
    player_name,
    country,
    placing,
    wins,
    losses,
    ties,
    deck_id,
    deck_name,
    deck_icons,
    drop_round,
    _loaded_at
from {{ source('ptcg_raw', 'standings') }}
