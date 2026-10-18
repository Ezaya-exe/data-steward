{{ config(materialized='table') }}
-- daily revenue rollup from order facts
select
    order_ts::date       as order_date,
    count(*)             as orders,
    sum(total_amount)    as revenue,
    sum(payment_total)   as collected
from {{ ref('fct_orders') }}
group by 1
order by 1
