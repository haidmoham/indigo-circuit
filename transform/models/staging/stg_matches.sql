select
    tournament_id,
    round,
    phase,
    table_number,
    match_id,
    player1,
    player2,
    winner,
    case
        when winner = player1  then player1
        when winner = player2  then player2
        else null
    end                              as winner_username,
    winner = '0'                     as is_tie,
    winner = '-1'                    as is_double_loss,
    _loaded_at
from {{ source('ptcg_raw', 'matches') }}
where player1 is not null
  and player2 is not null
