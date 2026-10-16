-- One row per decklist (online + majors) in the recent window:
--   list_cost_usd      sum(count x price) for the printings as listed
--   cheapest_cost_usd  same list, each card swapped to its cheapest
--                      functionally-identical printing (reprints)
--   unpriced_cards     copies with no price (unmapped / no data) — costs are
--                      therefore LOWER BOUNDS when unpriced_cards > 0.
{% set lookback = var('price_lookback_days', 90) %}

with online_lists as (
    select
        'online'                          as source,
        d.tournament_id,
        d.player_username                 as player_key,
        s.deck_id,
        s.deck_name,
        cast(t.tournament_date as date)   as tournament_date,
        d.card_category,
        d.card_name,
        d.card_set,
        d.card_number,
        d.card_count
    from {{ ref('stg_decklists') }} d
    join {{ source('ptcg_raw', 'standings') }} s
      on s.tournament_id = d.tournament_id
     and s.player_username = d.player_username
    join {{ source('ptcg_raw', 'tournaments') }} t
      on t.id = d.tournament_id
    where s.deck_id is not null
),

major_lists as (
    select
        'majors'                          as source,
        d.labs_tournament_id              as tournament_id,
        d.player_id                       as player_key,
        s.deck_id,
        s.deck_name,
        cast(t.tournament_date as date)   as tournament_date,
        d.card_category,
        d.card_name,
        d.card_set,
        d.card_number,
        d.card_count
    from {{ source('ptcg_raw', 'major_decklists') }} d
    join {{ source('ptcg_raw', 'major_standings') }} s
      on s.labs_tournament_id = d.labs_tournament_id
     and s.player_id = d.player_id
    join {{ source('ptcg_raw', 'major_tournaments') }} t
      on t.labs_id = d.labs_tournament_id
    where s.deck_id is not null
      and d.card_name is not null
),

lines as (
    select *,
        {{ card_key('card_set', 'card_number') }} as card_key
    from (select * from online_lists union all select * from major_lists)
    where tournament_date >= current_date - interval {{ lookback }} day
),

lines_keyed as (
    select
        l.*,
        p.price_usd,
        p.price_date,
        case when l.card_category in ('trainer', 'energy')
             then {{ trainer_equiv_key('l.card_name') }}
             else coalesce(m.func_key, 'k:' || l.card_key)
        end as equiv_key
    from lines l
    left join {{ ref('card_price_latest') }} p on p.card_key = l.card_key
    left join (
        select card_key, max(func_key) as func_key
        from {{ source('ptcg_raw', 'card_id_map') }}
        where status = 'mapped'
        group by card_key
    ) m on m.card_key = l.card_key
),

cheapest_equiv as (
    -- trainer/energy price rows are re-keyed by name so they meet decklist lines
    select
        case when lower(coalesce(supertype, '')) like 'trainer%'
               or lower(coalesce(supertype, '')) like 'energy%'
             then {{ trainer_equiv_key('card_name') }}
             else equiv_key end as equiv_key,
        min(price_usd) as cheapest_usd
    from {{ ref('card_price_latest') }}
    group by 1
),

priced as (
    select
        lk.*,
        least(lk.price_usd, ce.cheapest_usd) as cheapest_usd
    from lines_keyed lk
    left join cheapest_equiv ce on ce.equiv_key = lk.equiv_key
)

select
    source,
    tournament_id,
    player_key,
    deck_id,
    max(deck_name)                                                     as deck_name,
    max(tournament_date)                                               as tournament_date,
    sum(card_count)                                                    as total_cards,
    sum(case when price_usd is not null then card_count else 0 end)    as priced_cards,
    sum(case when price_usd is null and cheapest_usd is null
             then card_count else 0 end)                               as unpriced_cards,
    round(sum(card_count * price_usd), 2)                              as list_cost_usd,
    round(sum(card_count * coalesce(cheapest_usd, price_usd)), 2)      as cheapest_cost_usd,
    round(sum(case when price_usd is not null then card_count else 0 end)::double
          / nullif(sum(card_count), 0), 3)                             as priced_share,
    max(price_date)                                                    as prices_as_of
from priced
group by source, tournament_id, player_key, deck_id
