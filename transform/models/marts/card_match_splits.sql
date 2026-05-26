-- Pre-computes per-round win/loss splits by card presence for Bayesian EV.
-- One row per (deck_id, card_name, opponent_deck_id).
-- Powers the EV Lab: w_with/l_with = matches where player ran the card,
-- w_without/l_without = matches where they ran the same archetype without it.
with player_matches as (
    -- Normalize: one row per player per round (not one row per match)
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

-- Distinct (tournament, player, card) — one row per card per player list
player_cards as (
    select distinct tournament_id, player_username, card_name
    from {{ source('ptcg_raw', 'decklists') }}
),

-- Cards worth tracking: not universal (100%), not fringe (<2%), enough data
card_universe as (
    select deck_id, card_name
    from {{ ref('card_archetype_stats') }}
    where total_lists    >= 30
      and inclusion_rate <  0.99
      and inclusion_rate >  0.02
),

-- Expand each match by every trackable card for that archetype.
-- has_card = 1 if THIS player ran the card in this tournament, else 0.
match_card as (
    select
        mwd.my_deck_id   as deck_id,
        mwd.opp_deck_id  as opponent_deck_id,
        cu.card_name,
        mwd.won,
        (pc.card_name is not null)::int as has_card
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
    sum(case when has_card = 1 and won = 1 then 1 else 0 end) as w_with,
    sum(case when has_card = 1 and won = 0 then 1 else 0 end) as l_with,
    sum(case when has_card = 0 and won = 1 then 1 else 0 end) as w_without,
    sum(case when has_card = 0 and won = 0 then 1 else 0 end) as l_without
from match_card
group by deck_id, card_name, opponent_deck_id
having w_with + l_with >= 5
   and w_without + l_without >= 5
