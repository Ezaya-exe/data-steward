{{ config(materialized='table') }}
select
    order_item_id::integer    as order_item_id,
    order_id::integer         as order_id,
    product_id::integer       as product_id,
    quantity::integer         as quantity,
    unit_price::numeric(12,2) as unit_price,
    _batch_id                 as _batch_id,
    _loaded_at                as _loaded_at
from {{ source('raw', 'order_items') }}
