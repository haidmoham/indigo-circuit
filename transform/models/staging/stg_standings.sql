-- Recompute wins/losses/ties from match results where available.
-- The Limitless API standings feed records winner='-1' rounds (byes,
-- intentional draws, double-losses) as regular losses, inflating loss
-- counts. We override the API values with counts derived from stg_matches
-- and fall back to the raw API values only when no match data exists for
-- that tournament.

with raw_standings as (
    select * from {{ source('ptcg_raw', 'standings') }}
),

matches as (
    select * from {{ ref('stg_matches') }}
),

-- Unpivot matches so each player gets one row per match
match_players as (
    select
        tournament_id,
        player1                                     as player_username,
        (winner_username = player1)::int            as won,
        (winner_username = player2)::int            as lost,
        (is_tie or is_double_loss)::int             as tied
    from matches
    union all
    select
        tournament_id,
        player2                                     as player_username,
        (winner_username = player2)::int            as won,
        (winner_username = player1)::int            as lost,
        (is_tie or is_double_loss)::int             as tied
    from matches
),

match_stats as (
    select
        tournament_id,
        player_username,
        sum(won)                                    as computed_wins,
        sum(lost)                                   as computed_losses,
        sum(tied)                                   as computed_ties
    from match_players
    group by tournament_id, player_username
)

select
    s.tournament_id,
    s.player_username,
    s.player_name,
    s.country,
    s.placing,
    coalesce(ms.computed_wins,   s.wins)            as wins,
    coalesce(ms.computed_losses, s.losses)          as losses,
    coalesce(ms.computed_ties,   s.ties)            as ties,
    s.deck_id,
    s.deck_name,
    s.deck_icons,
    s.drop_round,
    s._loaded_at
from raw_standings s
left join match_stats ms
  on  s.tournament_id   = ms.tournament_id
  and s.player_username = ms.player_username
