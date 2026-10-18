-- Staging: cast and rename, no defensive cleanup — faults must break these loudly.
{{ config(materialized='table') }}
select
    customer_id::integer  as customer_id,
    lower(email)          as email,
    state,
    signup_date::date     as signup_date,
    _batch_id             as _batch_id,
    _loaded_at            as _loaded_at
from {{ source('raw', 'customers') }}
