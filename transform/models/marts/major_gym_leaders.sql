-- Gym Leader per archetype, majors only.
-- Gym Leader = rank 1 specialist for a given deck at major events.
-- Only the 8 most-played archetypes in the rolling 52-week season qualify
-- (mirrors the 8 Gym Badges in core Pokemon lore).
-- Minimum 2 majors with the archetype within the season to earn the title.

with windowed as (
    -- rolling 52-week window — same as seasonal_rankings
    select *
    from {{ ref('major_player_history') }}
    where tournament_date >= current_date - INTERVAL '52 weeks'
      and deck_name is not null
      and normalized_placement is not null
),

-- Top 8 archetypes by prestige score this season.
-- A tournament win earns 20 pts so even a niche deck that won an event
-- (e.g. Zoroark) outranks volume-only decks with no wins.
-- Top-8 finishes add 3 pts each; every appearance adds 1 pt.
top_archetypes as (
    select deck_name
    from windowed
    group by deck_name
    order by
        sum(case when "placing" = 1  then 40 else 0 end)   -- tournament wins
      + sum(case when "placing" <= 8 then  3 else 0 end)   -- top cut depth
      + count(*)                                          -- meta presence
    desc
    limit 8
),

arch_stats as (
    select
        lower(player_name)                                      as player_key,
        max(player_name)                                        as player_name,
        deck_name,
        max(deck_id)                                            as deck_id,
        max(deck_sprite)                                        as deck_sprite,
        count(*)                                                as tournament_count,
        sum(placement_score)                                    as raw_score,
        round(avg(normalized_placement), 4)                     as avg_normalized_placement,
        round(
            sum(wins)::float / nullif(sum(wins) + sum(losses), 0), 3
        )                                                       as win_rate,
        max(tournament_date)                                    as last_played,
        max_by(player_id, tournament_date)                      as player_id
    from windowed
    where deck_name in (select deck_name from top_archetypes)
    group by lower(player_name), deck_name
    having count(*) >= 2
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
    deck_name                                as archetype,
    deck_id,
    deck_sprite,
    player_name,
    player_id,
    archetype_rank,
    archetype_rank = 1                       as is_gym_leader,
    tournament_count,
    round(win_rate * 100, 1)                as win_rate_pct,
    round(avg_normalized_placement * 100, 1) as avg_placement_pct,
    gym_leader_score,
    last_played
from ranked
where archetype_rank <= 10
order by deck_name, archetype_rank
