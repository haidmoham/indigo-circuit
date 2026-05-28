-- Per-round win/loss splits for major tournaments (Regionals/ICs/Worlds).
-- Mirrors card_match_splits but uses raw.major_* tables.
-- major_matches stores per-player rows: result (W/L) + opponent_deck_id already resolved.
-- Filtered to the current rotation window (tournaments on/after most recent April 1).
with rotation_start as (
    select case
        when extract(month from current_date) >= 4
        then make_date(extract(year from current_date)::int, 4, 1)
        else make_date(extract(year from current_date)::int - 1, 4, 1)
    end as cutoff
),

current_tournaments as (
    select mt.labs_id
    from {{ source('ptcg_raw', 'major_tournaments') }} mt
    cross join rotation_start rs
    where mt.tournament_date >= rs.cutoff
),

player_matches as (
    select
        labs_tournament_id  as tournament_id,
        player_id,
        (result = 'W')::int as won,
        opponent_deck_id    as opp_deck_id
    from {{ source('ptcg_raw', 'major_matches') }}
    where result in ('W', 'L')
      and opponent_deck_id is not null
      and labs_tournament_id in (select labs_id from current_tournaments)
),

standings as (
    select labs_tournament_id as tournament_id, player_id, deck_id
    from {{ source('ptcg_raw', 'major_standings') }}
    where deck_id is not null
      and labs_tournament_id in (select labs_id from current_tournaments)
),

match_with_decks as (
    select
        pm.tournament_id,
        pm.player_id,
        pm.won,
        s.deck_id      as my_deck_id,
        pm.opp_deck_id
    from player_matches pm
    join standings s
      on pm.tournament_id = s.tournament_id
     and pm.player_id     = s.player_id
),

player_cards as (
    select distinct labs_tournament_id as tournament_id, player_id, card_name
    from {{ source('ptcg_raw', 'major_decklists') }}
    where labs_tournament_id in (select labs_id from current_tournaments)
),

card_universe as (
    select deck_id, card_name
    from {{ ref('major_card_archetype_stats') }}
    where total_lists    >= 10
      and inclusion_rate <  0.99
      and inclusion_rate >  0.02
),

match_card as (
    select
        mwd.my_deck_id  as deck_id,
        mwd.opp_deck_id as opponent_deck_id,
        cu.card_name,
        mwd.won,
        (pc.card_name is not null)::int as has_card
    from match_with_decks mwd
    join card_universe cu
      on cu.deck_id = mwd.my_deck_id
    left join player_cards pc
      on pc.tournament_id = mwd.tournament_id
     and pc.player_id     = mwd.player_id
     and pc.card_name     = cu.card_name
)

select
    deck_id,
    card_name,
    opponent_deck_id,
    sum(case when has_card = 1 and won = 1 then 1 else 0 end) as w_with,
    sum(case when has_card = 1 and won = 0 then 1 else 0 end) as l_with,
    sum(case when has_card = 0 and won = 1 then 1 else 0 end) as w_without,
    sum(case when has_card = 0 and won = 0 then 1 else 0 end) as l_without
from match_card
group by deck_id, card_name, opponent_deck_id
having w_with + l_with >= 3
   and w_without + l_without >= 3
