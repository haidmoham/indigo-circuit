-- One row per player per major tournament.
-- Derives player_count from standings so normalized_placement is comparable to online events.

with standings as (
    select * from {{ ref('stg_major_standings') }}
),

tournaments as (
    select * from {{ ref('stg_major_tournaments') }}
),

with_count as (
    select
        s.*,
        count(*) over (partition by s.tournament_id) as player_count
    from standings s
)

select
    w.player_name,
    w.player_id,
    w.country,
    w.tournament_id,
    t.tournament_name,
    t.tournament_date,
    t.tier,
    t.season,
    t.tier_bonus,
    t.season_weight,
    t.location,
    w.placing,
    w.player_count,
    round(w.placing::float / nullif(w.player_count, 0), 4)  as normalized_placement,
    w.wins,
    w.losses,
    w.ties,
    w.deck_id,
    w.deck_name,
    w.deck_sprite,
    -- Tier bonus — overrides raw table values to enforce prestige hierarchy:
    --   Worlds wins are legacy-defining, IC wins are season-defining,
    --   Regional wins are the baseline achievement worth chasing.
    --   Special events are participation-tier relative to these.
    case t.tier
        when 'worlds'        then 10.0
        when 'international' then  6.0
        when 'regional'      then  3.0
        when 'special'       then  1.2
        else                        1.0
    end                                                      as tier_weight,

    -- Placement score: convex curve that rewards deep runs over win rate.
    -- sqrt(player_count / placing) makes 1st worth 3× 8th and 8th worth 2× 32nd,
    -- so consistent top finishes dominate over going 12-3 and losing in Top 32.
    sqrt(greatest(w.player_count::float / nullif(w.placing, 0), 1.0))
        * case t.tier
            when 'worlds'        then 10.0
            when 'international' then  6.0
            when 'regional'      then  3.0
            when 'special'       then  1.2
            else                        1.0
          end                                               as placement_score
from with_count w
join tournaments t on w.tournament_id = t.tournament_id
