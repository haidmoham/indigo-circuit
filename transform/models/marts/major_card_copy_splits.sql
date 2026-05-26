-- Per-round win/loss splits by copy count for major tournaments (Regionals/ICs/Worlds).
-- Mirrors card_copy_splits but uses raw.major_* tables.
with player_matches as (
    select
        labs_tournament_id  as tournament_id,
        player_id,
        (result = 'W')::int as won,
        opponent_deck_id    as opp_deck_id
    from {{ source('ptcg_raw', 'major_matches') }}
    where result in ('W', 'L')
      and opponent_deck_id is not null
),

standings as (
    select labs_tournament_id as tournament_id, player_id, deck_id
    from {{ source('ptcg_raw', 'major_standings') }}
    where deck_id is not null
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
    select labs_tournament_id as tournament_id, player_id, card_name,
           max(card_count) as card_count
    from {{ source('ptcg_raw', 'major_decklists') }}
    where card_count is not null
    group by labs_tournament_id, player_id, card_name
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
        coalesce(pc.card_count, 0) as copy_count
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
    copy_count,
    sum(case when won = 1 then 1 else 0 end) as w,
    sum(case when won = 0 then 1 else 0 end) as l
from match_card
group by deck_id, card_name, opponent_deck_id, copy_count
having w + l >= 3
