with matches as (
    select * from {{ ref('stg_matches') }}
    where not is_double_loss
),

-- expand to one row per player perspective
player_pov as (
    select
        tournament_id,
        round,
        player1    as player_username,
        player2    as opponent_username,
        case
            when winner_username = player1 then 'win'
            when is_tie                    then 'tie'
            when winner_username = player2 then 'loss'
        end        as result
    from matches

    union all

    select
        tournament_id,
        round,
        player2    as player_username,
        player1    as opponent_username,
        case
            when winner_username = player2 then 'win'
            when is_tie                    then 'tie'
            when winner_username = player1 then 'loss'
        end        as result
    from matches
)

select
    player_username,
    opponent_username,
    count(*)                                                              as matches_played,
    count(case when result = 'win'  then 1 end)                         as wins,
    count(case when result = 'loss' then 1 end)                         as losses,
    count(case when result = 'tie'  then 1 end)                         as ties,
    round(
        count(case when result = 'win' then 1 end)::float
        / nullif(count(*), 0), 3
    )                                                                     as win_rate
from player_pov
where result is not null
group by player_username, opponent_username
