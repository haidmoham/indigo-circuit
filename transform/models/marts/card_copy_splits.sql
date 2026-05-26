-- Per-round win/loss splits by copy count for Bayesian EV copy modeling.
-- One row per (deck_id, card_name, opponent_deck_id, copy_count).
-- copy_count = 0 means player ran the same archetype without the card.
with player_matches as (
    select
        tournament_id,
        player1                        as player_username,
        player2                        as opponent_username,
        (winner = player1)::int        as won
    from {{ source('ptcg_raw', 'matches') }}
    where winner  is not null
      and player1 is not null
      and player2 is not null
    union all
    select
        tournament_id,
        player2,
        player1,
        (winner = player2)::int
    from {{ source('ptcg_raw', 'matches') }}
    where winner  is not null
      and player1 is not null
      and player2 is not null
),

standings as (
    select tournament_id, player_username, deck_id
    from {{ source('ptcg_raw', 'standings') }}
    where deck_id is not null
),

match_with_decks as (
    select
        pm.tournament_id,
        pm.player_username,
        pm.won,
        s_me.deck_id   as my_deck_id,
        s_opp.deck_id  as opp_deck_id
    from player_matches pm
    join standings s_me
      on pm.tournament_id   = s_me.tournament_id
     and pm.player_username = s_me.player_username
    join standings s_opp
      on pm.tournament_id     = s_opp.tournament_id
     and pm.opponent_username = s_opp.player_username
),

player_cards as (
    select tournament_id, player_username, card_name, max(card_count) as card_count
    from {{ source('ptcg_raw', 'decklists') }}
    where card_count is not null
    group by tournament_id, player_username, card_name
),

card_universe as (
    select deck_id, card_name
    from {{ ref('card_archetype_stats') }}
    where total_lists    >= 30
      and inclusion_rate <  0.99
      and inclusion_rate >  0.02
),

match_card as (
    select
        mwd.my_deck_id   as deck_id,
        mwd.opp_deck_id  as opponent_deck_id,
        cu.card_name,
        mwd.won,
        coalesce(pc.card_count, 0) as copy_count
    from match_with_decks mwd
    join card_universe cu
      on cu.deck_id = mwd.my_deck_id
    left join player_cards pc
      on pc.tournament_id    = mwd.tournament_id
     and pc.player_username  = mwd.player_username
     and pc.card_name        = cu.card_name
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
