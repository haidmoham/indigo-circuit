-- Card inclusion rates computed from major tournament (Regionals/ICs/Worlds) decklists.
-- Same structure as card_archetype_stats but uses raw.major_decklists + raw.major_standings.
-- Filtered to the current rotation window: tournaments on/after the most recent April 1,
-- so rotated archetypes (Charizard, Gardevoir, etc.) are excluded automatically each year.
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

decklists as (
    select * from {{ source('ptcg_raw', 'major_decklists') }}
    where labs_tournament_id in (select labs_id from current_tournaments)
),

standings as (
    select labs_tournament_id, player_id, deck_id
    from {{ source('ptcg_raw', 'major_standings') }}
    where deck_id is not null
      and labs_tournament_id in (select labs_id from current_tournaments)
),

joined as (
    select
        s.deck_id,
        d.card_category,
        d.card_name,
        d.card_set,
        d.card_count,
        d.labs_tournament_id || '|' || d.player_id as list_key
    from decklists d
    join standings s
      on d.labs_tournament_id = s.labs_tournament_id
     and d.player_id = s.player_id
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
    count(distinct j.list_key)::float / a.total_lists < 0.30             as is_tech_card
from joined j
join archetype_totals a on j.deck_id = a.deck_id
group by j.deck_id, j.card_category, j.card_name, j.card_set, a.total_lists
