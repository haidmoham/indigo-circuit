-- ATP-style rolling rankings from majors only.
-- Rules mirroring ATP tour:
--   1. Rolling 52-week window — results older than 1 year drop off naturally.
--   2. Best-N cap — all ICs + Worlds always count (only 2-3 per season);
--      best 10 regional/special results (prevents pure volume grinding).
--   3. No binary season weight — recency is handled by the rolling window.
-- Champion = rank 1, Elite Four = ranks 2-5.

with windowed as (
    -- restrict to last 52 weeks
    select *
    from {{ ref('major_player_history') }}
    where tournament_date >= dateadd('week', -52, current_date())
),

-- Best archetype per player by total placement_score in the window
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
            sum(placement_score)                                             as arch_score,
            row_number() over (
                partition by lower(player_name)
                order by sum(placement_score) desc
            )                                                                as rn
        from windowed
        where deck_name is not null
        group by lower(player_name), deck_name
    )
    where rn = 1
),

-- ICs and Worlds always count in full
prestige as (
    select
        lower(player_name)  as player_key,
        tournament_id,
        placement_score,
        placing,
        wins, losses,
        tier,
        normalized_placement,
        tournament_date
    from windowed
    where tier in ('worlds', 'international')
),

-- Regionals and specials: rank per player, keep best 10
regional_ranked as (
    select
        lower(player_name)  as player_key,
        tournament_id,
        placement_score,
        placing,
        wins, losses,
        tier,
        normalized_placement,
        tournament_date,
        row_number() over (
            partition by lower(player_name)
            order by placement_score desc
        )                   as result_rank
    from windowed
    where tier in ('regional', 'special')
),

regional_capped as (
    select * from regional_ranked where result_rank <= 10
),

combined as (
    select player_key, tournament_id, placement_score, placing,
           wins, losses, tier, normalized_placement, tournament_date
    from prestige
    union all
    select player_key, tournament_id, placement_score, placing,
           wins, losses, tier, normalized_placement, tournament_date
    from regional_capped
),

scored as (
    select
        player_key,
        -- restore display name from original table
        max(mph.player_name)                                    as player_name,
        max(mph.country)                                        as country,
        count(distinct c.tournament_id)                         as majors_counted,
        sum(c.placement_score)                                  as atp_score,
        round(avg(c.normalized_placement), 4)                   as avg_normalized_placement,
        round(
            sum(c.wins)::float / nullif(sum(c.wins) + sum(c.losses), 0), 3
        )                                                       as win_rate,
        min(c.placing)                                          as best_placing,
        sum(case when c.placing <= 8  then 1 else 0 end)        as top8s,
        sum(case when c.placing <= 16 then 1 else 0 end)        as top16s,
        -- tier-split top 8s for card display
        sum(case when c.placing <= 8 and c.tier = 'worlds'                    then 1 else 0 end) as worlds_top8s,
        sum(case when c.placing <= 8 and c.tier = 'international'             then 1 else 0 end) as ic_top8s,
        sum(case when c.placing <= 8 and c.tier in ('regional', 'special')    then 1 else 0 end) as regional_top8s,
        max(c.tournament_date)                                  as last_played,
        sum(case when c.tier = 'worlds'        then c.placement_score else 0 end) as worlds_score,
        sum(case when c.tier = 'international' then c.placement_score else 0 end) as ic_score,
        sum(case when c.tier in ('regional', 'special') then c.placement_score else 0 end) as regional_score
    from combined c
    join {{ ref('major_player_history') }} mph
      on lower(mph.player_name) = c.player_key
      and mph.tournament_id = c.tournament_id
    group by player_key
    having count(distinct c.tournament_id) >= 2
),

ranked_with_arch as (
    select
        s.*,
        ta.top_deck_name,
        ta.top_deck_sprite
    from scored s
    left join top_arch ta on ta.player_key = s.player_key
),

final as (
    select
        *,
        round(atp_score, 1)                                     as atp_score_rounded,
        row_number() over (order by atp_score desc)             as rank
    from ranked_with_arch
)

select
    rank,
    player_name,
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
    case rank
        when 1 then 'Champion'
        when 2 then 'Elite 1'
        when 3 then 'Elite 2'
        when 4 then 'Elite 3'
        when 5 then 'Elite 4'
        else null
    end                                      as title
from final
order by rank
