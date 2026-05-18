-- Rolling 52-week rankings. Every result in the window counts in full.
-- No best-N cap — recency decay naturally devalues older events without
-- needing an artificial ceiling. Half-life = 6 months.
--
-- Rules:
--   1. Rolling 52-week window — results older than 1 year drop off entirely.
--   2. All tiers count — Worlds, ICs, Regionals, Specials (tier_weight
--      already encodes prestige; no separate cap needed).
--   3. Recency decay — placement_score × exp(-1.386 × days_ago / 365).
--      A result from 6 months ago is worth 50% of an identical result today.
-- Champion = rank 1, Elite Four = ranks 2-5.

with windowed as (
    select *
    from {{ ref('major_player_history') }}
    where tournament_date >= current_date - INTERVAL '52 weeks'
),

-- Best archetype per player by decayed placement_score — reflects current specialization
top_arch as (
    select
        player_key,
        deck_name   as top_deck_name,
        deck_sprite as top_deck_sprite
    from (
        select
            lower(player_name)                                               as player_key,
            deck_name,
            max(deck_sprite)                                                 as deck_sprite,
            sum(placement_score
                * exp(-1.386 * datediff('day', tournament_date, current_date()) / 365.0)
            )                                                                as arch_score,
            row_number() over (
                partition by lower(player_name)
                order by sum(placement_score
                    * exp(-1.386 * datediff('day', tournament_date, current_date()) / 365.0)
                ) desc
            )                                                                as rn
        from windowed
        where deck_name is not null
        group by lower(player_name), deck_name
    )
    where rn = 1
),

-- Most recent player_id per player (canonical Limitless account link)
recent_ids as (
    select player_key, player_id
    from (
        select
            lower(player_name) as player_key,
            player_id,
            row_number() over (
                partition by lower(player_name)
                order by tournament_date desc
            ) as rn
        from {{ ref('major_player_history') }}
        where player_id is not null
    )
    where rn = 1
),

scored as (
    select
        lower(player_name)                                      as player_key,
        max(player_name)                                        as player_name,
        max(country)                                            as country,
        count(distinct tournament_id)                           as majors_counted,
        -- Decayed ATP score: exp(-1.386 × days/365) → 6-month half-life
        sum(
            placement_score
            * exp(-1.386 * datediff('day', tournament_date, current_date()) / 365.0)
        )                                                       as atp_score,
        round(avg(normalized_placement), 4)                     as avg_normalized_placement,
        round(
            sum(wins)::float / nullif(sum(wins) + sum(losses), 0), 3
        )                                                       as win_rate,
        min("placing")                                          as best_placing,
        sum(case when "placing" <= 8  then 1 else 0 end)        as top8s,
        sum(case when "placing" <= 16 then 1 else 0 end)        as top16s,
        sum(case when "placing" <= 8 and tier = 'worlds'                    then 1 else 0 end) as worlds_top8s,
        sum(case when "placing" <= 8 and tier = 'international'             then 1 else 0 end) as ic_top8s,
        sum(case when "placing" <= 8 and tier in ('regional', 'special')    then 1 else 0 end) as regional_top8s,
        max(tournament_date)                                    as last_played,
        sum(case when tier = 'worlds'        then placement_score * exp(-1.386 * datediff('day', tournament_date, current_date()) / 365.0) else 0 end) as worlds_score,
        sum(case when tier = 'international' then placement_score * exp(-1.386 * datediff('day', tournament_date, current_date()) / 365.0) else 0 end) as ic_score,
        sum(case when tier in ('regional', 'special') then placement_score * exp(-1.386 * datediff('day', tournament_date, current_date()) / 365.0) else 0 end) as regional_score
    from windowed
    group by lower(player_name)
    having count(distinct tournament_id) >= 2
),

ranked_with_arch as (
    select
        s.*,
        ta.top_deck_name,
        ta.top_deck_sprite,
        ri.player_id
    from scored s
    left join top_arch ta   on ta.player_key = s.player_key
    left join recent_ids ri on ri.player_key = s.player_key
),

final as (
    select
        *,
        row_number() over (order by atp_score desc) as rank
    from ranked_with_arch
)

select
    rank,
    player_name,
    player_id,
    country,
    majors_counted,
    round(win_rate * 100, 1)                 as win_rate_pct,
    best_placing,
    top8s,
    top16s,
    worlds_top8s,
    ic_top8s,
    regional_top8s,
    round(atp_score, 1)                      as atp_score,
    round(avg_normalized_placement * 100, 1) as avg_placement_pct,
    last_played,
    round(worlds_score, 1)                   as worlds_score,
    round(ic_score, 1)                       as ic_score,
    round(regional_score, 1)                 as regional_score,
    top_deck_name,
    top_deck_sprite,
    case
        when rank = 1             then 'Champion'
        when rank between 2 and 5 then 'Elite Four'
        else null
    end                                      as title
from final
order by rank
