select
    id                               as tournament_id,
    game,
    format,
    name                             as tournament_name,
    tournament_date::timestamp_tz    as tournament_date,
    player_count,
    _loaded_at
from {{ source('ptcg_raw', 'tournaments') }}
where game = 'PTCG'
