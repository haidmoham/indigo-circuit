with history as (
    select * from {{ ref('player_tournament_history') }}
),

glicko as (
    select * from {{ ref('stg_glicko_ratings') }}
)

select
    player_username,
    -- keep the most recent display name
    max_by(player_name, tournament_date)                                        as player_name,
    max_by(country, tournament_date)                                            as country,

    count(*)                                                                    as tournaments_entered,
    sum(wins)                                                                   as total_wins,
    sum(losses)                                                                 as total_losses,
    sum(ties)                                                                   as total_ties,
    round(
        sum(wins)::float / nullif(sum(wins) + sum(losses), 0), 3
    )                                                                           as win_rate,

    round(avg("placing"), 1)                                                    as avg_placement,
    min("placing")                                                              as best_placement,
    max(tournament_date)                                                        as last_played,
    min(tournament_date)                                                        as first_played,

    count(case when "placing" <= 8 then 1 end)                                 as top8_finishes,
    count(case when normalized_placement <= 0.10 then 1 end)                   as top_cut_count,
    round(
        count(case when normalized_placement <= 0.10 then 1 end)::float
        / nullif(count(*), 0), 3
    )                                                                           as top_cut_rate,

    -- consistency: 1 - stddev of normalized placements; null if only 1 tournament
    case
        when count(*) > 1
        then round(1 - stddev(normalized_placement), 3)
        else null
    end                                                                         as consistency_score

from history
group by player_username
