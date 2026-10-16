{#- Mirror of ingest/prices.py card_key(): 'TWM' + '085' -> 'TWM-85'; 'SVP' + 'SVP085' -> 'SVP-85'. -#}
{% macro card_key(set_col, number_col) -%}
upper(trim({{ set_col }})) || '-' ||
regexp_replace(
    regexp_replace(
        regexp_replace(split_part(upper(trim(cast({{ number_col }} as varchar))), '/', 1),
                       '^(SVP|MEP|SWSH|SM|XY|BW|PR)(\d)', '\2'),
        '^([A-Z]*)0+(\d)', '\1\2'),
    '^$', '?')
{%- endmacro %}

{#- Name-only equivalence for Trainers / Energy (same name == same card). -#}
{% macro trainer_equiv_key(name_col) -%}
't:' || regexp_replace(lower(regexp_replace(replace(replace(replace(trim({{ name_col }}), '’', ''''), 'é', 'e'), 'É', 'E'), '\s+', ' ', 'g')), '^basic (.* energy)$', '\1')
{%- endmacro %}
