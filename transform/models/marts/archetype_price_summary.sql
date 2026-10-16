-- Per archetype: what does a recent list cost? Median + IQR over lists whose
-- cards are >= 90% priced (the rest would understate cost). Joined to the
-- existing archetype marts for list volume context.
with lists as (
    select * from {{ ref('deck_list_prices') }}
),

usable as (
    select * from lists where priced_share >= 0.9
),

summary as (
    select
        deck_id,
        count(*)                                               as lists_priced,
        sum(case when unpriced_cards = 0 then 1 else 0 end)    as lists_fully_priced,
        round(median(list_cost_usd), 2)                        as median_list_usd,
        round(quantile_cont(list_cost_usd, 0.25), 2)           as p25_list_usd,
        round(quantile_cont(list_cost_usd, 0.75), 2)           as p75_list_usd,
        round(median(cheapest_cost_usd), 2)                    as median_cheapest_usd,
        round(min(cheapest_cost_usd), 2)                       as min_cheapest_usd,
        round(avg(priced_share), 3)                            as avg_priced_share,
        max(prices_as_of)                                      as prices_as_of
    from usable
    group by deck_id
),

volume as (
    select deck_id, count(*) as lists_recent, round(avg(priced_share), 3) as coverage_all_lists,
           -- online names are display names ('Dragapult Blaziken'); majors names are sprite keys
           coalesce(max(case when source = 'online' then deck_name end), max(deck_name)) as deck_name
    from lists group by deck_id
),

online_marts as (
    select deck_id, max(total_lists) as online_total_lists
    from {{ ref('card_archetype_stats') }} group by deck_id
),

major_marts as (
    select deck_id, max(total_lists) as major_total_lists
    from {{ ref('major_card_archetype_stats') }} group by deck_id
)

select
    v.deck_id,
    v.deck_name,
    v.lists_recent,
    coalesce(s.lists_priced, 0)        as lists_priced,
    coalesce(s.lists_fully_priced, 0)  as lists_fully_priced,
    v.coverage_all_lists,
    s.median_list_usd,
    s.p25_list_usd,
    s.p75_list_usd,
    s.median_cheapest_usd,
    s.min_cheapest_usd,
    s.prices_as_of,
    o.online_total_lists,
    m.major_total_lists
from volume v
left join summary s     on s.deck_id = v.deck_id
left join online_marts o on o.deck_id = v.deck_id
left join major_marts m  on m.deck_id = v.deck_id
