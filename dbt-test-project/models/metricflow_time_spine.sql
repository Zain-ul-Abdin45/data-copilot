{{ config(materialized='table') }}

with days as (
    {{ dbt.date_spine(
        datepart="day",
        start_date="cast('2025-01-01' as date)",
        end_date="cast('2028-01-01' as date)"
    ) }}
)

select cast(date_day as date) as date_day
from days
