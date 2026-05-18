with history as (
    select * from {{ ref('player_tournament_history') }}
    where deck_id is not null
)

select
    player_username,
    deck_id,
    deck_name,
    count(*)                                                              as times_played,
    min(tournament_date)                                                  as first_played,
    max(tournament_date)                                                  as last_played,
    round(avg("placing"), 1)                                                as avg_placement,
    round(
        sum(wins)::float / nullif(sum(wins) + sum(losses), 0), 3
    )                                                                     as win_rate,
    -- loyalty: share of this player's total tournaments on this archetype
    round(
        count(*)::float / sum(count(*)) over (partition by player_username), 3
    )                                                                     as archetype_loyalty
from history
group by player_username, deck_id, deck_name
