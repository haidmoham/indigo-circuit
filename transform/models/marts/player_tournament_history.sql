with standings as (
    select * from {{ ref('stg_standings') }}
),

tournaments as (
    select * from {{ ref('stg_tournaments') }}
)

select
    s.player_username,
    s.player_name,
    s.country,
    s.tournament_id,
    t.tournament_name,
    t.tournament_date,
    t.format,
    t.player_count,
    s.placing,
    round(s.placing::float / nullif(t.player_count, 0), 4) as normalized_placement,
    s.wins,
    s.losses,
    s.ties,
    s.deck_id,
    s.deck_name,
    s.drop_round,
    s.drop_round is not null                                as did_drop
from standings s
left join tournaments t on s.tournament_id = t.tournament_id
where t.tournament_date >= current_date - INTERVAL '52 weeks'
