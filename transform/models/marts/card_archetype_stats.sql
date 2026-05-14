-- For each archetype: how often is each card included, and in what count?
-- Powers the "tech card" view on the scouting report and meta pages.
with decklists as (
    select * from {{ ref('stg_decklists') }}
),

standings as (
    select tournament_id, player_username, deck_id
    from {{ ref('stg_standings') }}
    where deck_id is not null
),

joined as (
    select
        s.deck_id,
        d.card_category,
        d.card_name,
        d.card_set,
        d.card_count,
        d.tournament_id || '|' || d.player_username as list_key
    from decklists d
    join standings s
      on d.tournament_id = s.tournament_id
     and d.player_username = s.player_username
),

archetype_totals as (
    select deck_id, count(distinct list_key) as total_lists
    from joined
    group by deck_id
)

select
    j.deck_id,
    j.card_category,
    j.card_name,
    j.card_set,
    count(distinct j.list_key)                                            as lists_with_card,
    a.total_lists,
    round(count(distinct j.list_key)::float / a.total_lists, 3)          as inclusion_rate,
    round(avg(j.card_count), 2)                                           as avg_count_when_included,
    -- "tech" = appears in fewer than 30% of lists for this archetype
    count(distinct j.list_key)::float / a.total_lists < 0.30             as is_tech_card
from joined j
join archetype_totals a on j.deck_id = a.deck_id
group by j.deck_id, j.card_category, j.card_name, j.card_set, a.total_lists
