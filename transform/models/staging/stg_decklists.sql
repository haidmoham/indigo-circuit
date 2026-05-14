select
    tournament_id,
    player_username,
    card_category,
    card_name,
    card_set,
    card_number,
    card_count,
    _loaded_at
from {{ source('ptcg_raw', 'decklists') }}
where card_name is not null
