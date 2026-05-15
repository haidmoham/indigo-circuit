-- Gym Leader per archetype: the highest-scoring specialist for each deck.
-- Score = sum of season-weighted placement scores for tournaments played with that archetype,
-- scaled by log(tournament_count) to penalise tiny samples.
-- Minimum 3 tournaments with the archetype to qualify.
-- archetype_rank = 1 is the current Gym Leader.

with base as (
    select
        pth.player_username,
        pth.player_name,
        pth.deck_id,
        pth.deck_name,
        pth.tournament_id,
        pth.tournament_date,
        pth.normalized_placement,
        pth.player_count,
        -- season derived from tournament date (season year starts in September)
        case
            when month(pth.tournament_date) >= 9
                then year(pth.tournament_date) + 1
            else year(pth.tournament_date)
        end                                                     as season_year,
        pth.wins,
        pth.losses,
        pth.ties
    from {{ ref('player_tournament_history') }} pth
    where pth.deck_name is not null
        and pth.normalized_placement is not null
),

season_weighted as (
    select
        *,
        case season_year
            when 2026 then 1.0
            when 2025 then 0.5
            else 0.25
        end                                                     as season_weight,
        -- placement contribution: top finish in a big field scores high
        (1.0 - normalized_placement)
            * ln(greatest(player_count / 8.0, 1.0))
            * case season_year
                when 2026 then 1.0
                when 2025 then 0.5
                else 0.25
              end                                               as placement_score
    from base
),

arch_stats as (
    select
        player_username,
        player_name,
        deck_id,
        deck_name,
        count(*)                                                as tournament_count,
        sum(placement_score)                                    as raw_score,
        round(avg(normalized_placement), 4)                     as avg_normalized_placement,
        round(
            sum(wins)::float / nullif(sum(wins) + sum(losses), 0), 3
        )                                                       as win_rate,
        max(tournament_date)                                    as last_played
    from season_weighted
    group by player_username, player_name, deck_id, deck_name
    having count(*) >= 3
),

scored as (
    select
        *,
        round(raw_score * ln(1.0 + tournament_count), 4)       as gym_leader_score
    from arch_stats
),

ranked as (
    select
        *,
        row_number() over (
            partition by deck_name
            order by gym_leader_score desc
        )                                                       as archetype_rank
    from scored
)

select
    deck_name                               as archetype,
    deck_id,
    player_username,
    player_name,
    archetype_rank,
    archetype_rank = 1                      as is_gym_leader,
    tournament_count,
    round(win_rate * 100, 1)               as win_rate_pct,
    round(avg_normalized_placement * 100, 1) as avg_placement_pct,
    gym_leader_score,
    last_played
from ranked
where archetype_rank <= 10
order by deck_name, archetype_rank
