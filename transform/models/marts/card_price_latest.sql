-- One row per printing: the most recent snapshot, cheapest variant
-- (a "normal" and a "reverseHolofoil" of the same printing are equally legal).
-- equiv_key groups functionally identical printings (reprints) — see
-- ingest/prices.py func_key(): trainers/energy by name, Pokémon by name + card text.
with latest_day as (
    select card_key, max(price_date) as price_date
    from {{ ref('stg_card_prices') }}
    group by card_key
),

ranked as (
    select
        p.*,
        row_number() over (
            partition by p.card_key
            order by p.price_usd asc, p.variant
        ) as rn
    from {{ ref('stg_card_prices') }} p
    join latest_day l
      on p.card_key = l.card_key
     and p.price_date = l.price_date
)

select
    r.card_key,
    r.set_code,
    r.number,
    coalesce(m.card_name, r.card_name)                       as card_name,
    m.supertype,
    r.variant                                                as cheapest_variant,
    r.price_usd,
    r.market_usd,
    r.low_usd,
    r.source,
    r.price_date,
    coalesce(m.func_key, 'k:' || r.card_key)                 as equiv_key
from ranked r
left join {{ source('ptcg_raw', 'card_id_map') }} m
  on m.card_key = r.card_key
 and m.provider = r.source
where r.rn = 1
