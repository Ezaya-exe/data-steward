{{ config(materialized='table') }}
-- customer profile dimension with lifetime order aggregates
with customer_orders as (
    select
        customer_id,
        count(*)           as order_count,
        sum(total_amount)  as total_spend,
        max(order_ts)      as last_order_ts
    from {{ ref('fct_orders') }}
    group by customer_id
)
select
    c.customer_id,
    c.email,
    c.state,
    c.signup_date,
    coalesce(o.order_count, 0)    as order_count,
    coalesce(o.total_spend, 0)    as total_spend,
    o.last_order_ts               as last_order_ts,
    c._batch_id                   as _first_loaded_batch_id,
    c._loaded_at                  as _first_loaded_at
from {{ ref('stg_customers') }} c
left join customer_orders o on o.customer_id = c.customer_id
