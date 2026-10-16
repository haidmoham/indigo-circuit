-- Daily price snapshots, normalised. Rows with no usable price are dropped.
select
    card_key,
    upper(set_code)                     as set_code,
    number,
    variant,
    market_usd,
    low_usd,
    coalesce(market_usd, low_usd)       as price_usd,
    source,
    fetched_at,
    cast(fetched_at as date)            as price_date,
    card_name,
    provider_id
from {{ source('ptcg_raw', 'card_prices') }}
where coalesce(market_usd, low_usd) is not null
  and coalesce(market_usd, low_usd) >= 0
