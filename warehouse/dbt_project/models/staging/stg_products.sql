{{ config(materialized='table') }}
select
    product_id::integer       as product_id,
    category,
    price::numeric(12, 2)     as price,
    _batch_id                 as _batch_id,
    _loaded_at                as _loaded_at
from {{ source('raw', 'products') }}
