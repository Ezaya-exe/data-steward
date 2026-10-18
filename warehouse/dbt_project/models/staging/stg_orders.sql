{{ config(materialized='table') }}
select
    order_id::integer             as order_id,
    customer_id::integer          as customer_id,
    order_ts::timestamptz         as order_ts,
    status,
    total_amount::numeric(12, 2)  as total_amount,
    _batch_id                     as _batch_id,
    _loaded_at                    as _loaded_at
from {{ source('raw', 'orders') }}
