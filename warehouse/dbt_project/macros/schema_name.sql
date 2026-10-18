{#
  Custom generate_schema_name (docs/ARCHITECTURE.md §5):
  - dev: use the custom schema as-is (staging, marts) so datasets stay stable;
  - sandbox: prefix with the target schema (dqs_sandbox_<id>_staging/_marts)
    so fix-verification writes are entirely namespaced and droppable.
#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if target.name == 'sandbox' -%}
        {%- if custom_schema_name is none -%}
            {{ target.schema | trim }}
        {%- else -%}
            {{ target.schema | trim }}_{{ custom_schema_name | trim }}
        {%- endif -%}
    {%- else -%}
        {%- if custom_schema_name is none -%}
            {{ target.schema | trim }}
        {%- else -%}
            {{ custom_schema_name | trim }}
        {%- endif -%}
    {%- endif -%}
{%- endmacro %}
