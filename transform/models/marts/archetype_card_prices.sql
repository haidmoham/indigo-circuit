-- Per (archetype, card name): how much of a typical list's cost this card is,
-- what the listed printings cost, and the cheapest functionally-identical
-- printing we have a price for. Powers the price page + budget swaps.
{% set lookback = var('price_lookback_days', 90) %}

with lines as (
    select
        s.deck_id,
        d.tournament_id || '|' || d.player_username       as list_key,
        d.card_category,
        d.card_name,
        d.card_count,
        {{ card_key('d.card_set', 'd.card_number') }}     as card_key
    from {{ ref('stg_decklists') }} d
    join {{ source('ptcg_raw', 'standings') }} s
      on s.tournament_id = d.tournament_id and s.player_username = d.player_username
    join {{ source('ptcg_raw', 'tournaments') }} t on t.id = d.tournament_id
    where s.deck_id is not null
      and cast(t.tournament_date as date) >= current_date - interval {{ lookback }} day
    union all
    select
        s.deck_id,
        'M' || d.labs_tournament_id || '|' || d.player_id,
        d.card_category,
        d.card_name,
        d.card_count,
        {{ card_key('d.card_set', 'd.card_number') }}
    from {{ source('ptcg_raw', 'major_decklists') }} d
    join {{ source('ptcg_raw', 'major_standings') }} s
      on s.labs_tournament_id = d.labs_tournament_id and s.player_id = d.player_id
    join {{ source('ptcg_raw', 'major_tournaments') }} t on t.labs_id = d.labs_tournament_id
    where s.deck_id is not null and d.card_name is not null
      and cast(t.tournament_date as date) >= current_date - interval {{ lookback }} day
),

totals as (
    select deck_id, count(distinct list_key) as total_lists from lines group by deck_id
),

priced_lines as (
    select
        l.*,
        p.price_usd,
        case when l.card_category in ('trainer', 'energy')
             then {{ trainer_equiv_key('l.card_name') }}
             else coalesce(m.func_key, 'k:' || l.card_key) end as equiv_key
    from lines l
    left join {{ ref('card_price_latest') }} p on p.card_key = l.card_key
    left join (
        select card_key, max(func_key) as func_key
        from {{ source('ptcg_raw', 'card_id_map') }} where status = 'mapped' group by card_key
    ) m on m.card_key = l.card_key
),

equiv_prices as (
    select
        case when lower(coalesce(supertype, '')) like 'trainer%'
               or lower(coalesce(supertype, '')) like 'energy%'
             then {{ trainer_equiv_key('card_name') }}
             else equiv_key end                               as equiv_key,
        min(price_usd)                                        as cheapest_usd,
        arg_min(card_key, price_usd)                          as cheapest_card_key,
        arg_min(cheapest_variant, price_usd)                  as cheapest_variant,
        max(price_date)                                       as price_date
    from {{ ref('card_price_latest') }}
    group by 1
),

per_list as (
    -- one row per list x card (a list can split copies across printings)
    select
        deck_id, list_key, card_category, card_name,
        sum(card_count)                                       as copies,
        sum(card_count * price_usd)                           as listed_cost,
        bool_and(price_usd is not null)                       as fully_priced,
        min(equiv_key)                                        as equiv_key,
        arg_max(card_key, card_count)                         as main_card_key
    from priced_lines
    group by deck_id, list_key, card_category, card_name
),

per_card as (
    select
        pl.deck_id,
        pl.card_category,
        pl.card_name,
        count(*)                                                       as lists_with_card,
        t.total_lists,
        round(count(*)::double / t.total_lists, 3)                     as inclusion_rate,
        round(avg(pl.copies), 2)                                       as avg_copies,
        mode(pl.main_card_key)                                         as typical_card_key,
        -- reprint lookup follows the most-registered printing's card text only
        mode(pl.equiv_key)                                             as equiv_key,
        -- per-copy cost of what players actually register
        median(case when pl.fully_priced then pl.listed_cost / pl.copies end) as median_listed
    from per_list pl
    join totals t on t.deck_id = pl.deck_id
    group by pl.deck_id, pl.card_category, pl.card_name, t.total_lists
)

select
    c.deck_id,
    c.card_category,
    c.card_name,
    c.lists_with_card,
    c.total_lists,
    c.inclusion_rate,
    c.avg_copies,
    c.typical_card_key,
    round(c.median_listed, 2)                                          as median_listed_usd,
    round(least(e.cheapest_usd, c.median_listed), 2)                   as cheapest_equiv_usd,
    case when e.cheapest_usd < c.median_listed then e.cheapest_card_key
         else c.typical_card_key end                                   as cheapest_card_key,
    e.cheapest_variant,
    -- expected $ this card adds to a list = inclusion x copies x per-copy price
    round(c.inclusion_rate * c.avg_copies * c.median_listed, 2)        as expected_cost_usd,
    e.price_date
from per_card c
left join equiv_prices e on e.equiv_key = c.equiv_key
