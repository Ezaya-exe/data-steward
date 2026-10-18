{{ config(materialized='table') }}
select
    payment_id::integer   as payment_id,
    order_id::integer     as order_id,
    method,
    amount::numeric(12,2) as amount,
    _batch_id             as _batch_id,
    _loaded_at            as _loaded_at
from {{ source('raw', 'payments') }}
