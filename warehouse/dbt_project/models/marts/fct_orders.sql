{{ config(materialized='table') }}
-- order facts enriched with payment totals and item counts
with payments as (
    select
        order_id,
        sum(amount)  as payment_total,
        count(*)     as payment_count
    from {{ ref('stg_payments') }}
    group by order_id
),
items as (
    select
        order_id,
        sum(quantity) as item_qty,
        count(*)      as item_count
    from {{ ref('stg_order_items') }}
    group by order_id
)
select
    o.order_id,
    o.customer_id,
    o.order_ts,
    o.status,
    o.total_amount,
    coalesce(p.payment_total, o.total_amount) as payment_total,
    coalesce(p.payment_count, 0)              as payment_count,
    coalesce(i.item_qty, 0)                   as item_qty,
    coalesce(i.item_count, 0)                 as item_count,
    o._batch_id,
    o._loaded_at
from {{ ref('stg_orders') }} o
left join payments p on p.order_id = o.order_id
left join items   i on i.order_id = o.order_id
