from enum import Enum
from collections.abc import Iterable
from typing import Any, Callable, Optional, Sequence, Type, TypeVar, Dict, List, cast

from django.db.models.base import Model
from django.db.models.sql.query import get_field_names_from_opts  # type:ignore
from strawberry import UNSET
from strawberry.field import StrawberryField
from strawberry.utils.typing import __dataclass_transform__
from strawberry_django import filters as _filters
from strawberry_django import utils
from strawberry_django.fields.field import field as _field

from django.db.models import QuerySet
from django.db.models import Q

from . import field
from .relay import GlobalID, connection, node
from .type import input, JointType, _LOGICAL_EXPRESSIONS

_T = TypeVar("_T")


def _normalize_value(value: Any):
    if isinstance(value, list):
        return [_normalize_value(v) for v in value]
    elif isinstance(value, GlobalID):
        return value.node_id

    return value


#  TODO: refactor
def fields(obj):
    if hasattr(obj, "_kwargs_order"):
        type_definition_field_index = {field.name: field for field in obj._type_definition.fields}
        return [type_definition_field_index[field_name] for field_name in obj._kwargs_order]
    return obj._type_definition.fields


# TODO: consider about joint for the methods
# TODO: consider about joint for the methods
def _build_filter_kwargs(filters, joint_type: JointType = JointType.AND):

    filter_kwargs = []
    filter_methods = []
    django_model = cast(Type[Model], utils.get_django_model(filters))

    for f in fields(filters):
        field_name = f.name
        field_value = _normalize_value(getattr(filters, field_name))
        print('field = ', f)
        # Unset means we are not filtering this. None is still acceptable
        if field_value is UNSET:
            continue

        # Logical expressions
        if field_name in _LOGICAL_EXPRESSIONS and utils.is_strawberry_type(field_value):
            joint_filter_kwargs = _build_filter_kwargs(field_value, _LOGICAL_EXPRESSIONS[field_name])[0]
            if len(joint_filter_kwargs) > 1:
                filter_kwargs.append(joint_filter_kwargs)  #{**filter_kwargs, **joint_filter_kwargs}
            else:
                filter_kwargs.extend(joint_filter_kwargs)
            continue

        if isinstance(field_value, Enum):
            field_value = field_value.value

        field_name = _filters.lookup_name_conversion_map.get(field_name, field_name)
        filter_method = getattr(filters, f"filter_{field_name}", None)
        if filter_method:
            filter_methods.append(filter_method)
            continue

        if django_model and field_name not in get_field_names_from_opts(django_model._meta):
            continue
        if utils.is_strawberry_type(field_value):
            print('1')
            subfield_filter_kwargs, subfield_filter_methods = _build_filter_kwargs(field_value)
            for subfield_name_and_joint_type, subfield_value in subfield_filter_kwargs[0].items(): 
                subfield_name, _ , _ = subfield_name_and_joint_type  #!!!!!
                if isinstance(subfield_value, Enum):
                    subfield_value = subfield_value.value
                if isinstance(subfield_value, Iterable):
                    filter_kwargs.append({(f"{field_name}__{subfield_name}", joint_type, tuple(subfield_value)): subfield_value})
                else:
                    filter_kwargs.append({(f"{field_name}__{subfield_name}", joint_type, subfield_value): subfield_value})
            filter_methods.extend(subfield_filter_methods)
        else:
            if isinstance(field_value, Iterable):
                filter_kwargs.append({(field_name, joint_type, tuple(field_value)): field_value})   #!!!!!!!!!!!!!!!!
            else:
                filter_kwargs.append({(field_name, joint_type, field_value): field_value})
    return filter_kwargs, filter_methods


def _apply(filters, queryset: QuerySet, info=UNSET, pk=UNSET) -> QuerySet:
    if pk is not UNSET:
        queryset = queryset.filter(pk=pk)

    if (
        filters is UNSET
        or filters is None
        or not hasattr(filters, "_django_type")
        or not filters._django_type.is_filter
    ):
        return queryset

    filter_method = getattr(filters, "filter", None)
    if filter_method:
        return filter_method(queryset)
    filter_kwargs_list, filter_methods = _build_filter_kwargs(filters)
    filter_kwargs = []
    ext_dict = {}
    for item in filter_kwargs_list:
        if isinstance(item, dict):
            ext_dict = {**ext_dict, **item}
        else:
            filter_kwargs.append(ext_dict)
            ext_dict = {}
            for inner_item in item:
                ext_dict = {**ext_dict, **inner_item}
            filter_kwargs.append(ext_dict)
            ext_dict = {}
    if not filter_kwargs:
        filter_kwargs.append(ext_dict)
    filters_kwargs_expressions = None
    total_expressions = None
    size = len(filter_kwargs)
    try:
        for i, dictionary in enumerate(filter_kwargs):
            for filter_key_and_joint_type, filter_value in dictionary.items():
                filter_key, filter_joint_type, _ = filter_key_and_joint_type
                if filters_kwargs_expressions is None and filter_joint_type != JointType.NOT:
                    expr = filter_joint_type
                    filters_kwargs_expressions = Q(**{filter_key: filter_value})
                elif filters_kwargs_expressions is None and filter_joint_type == JointType.NOT:
                    expr = ~filter_joint_type
                    filters_kwargs_expressions = ~Q(**{filter_key: filter_value})
                elif not(filters_kwargs_expressions is None) and filter_joint_type == JointType.AND:
                    filters_kwargs_expressions &= Q(**{filter_key: filter_value})
                elif not(filters_kwargs_expressions is None) and filter_joint_type == JointType.OR:
                    filters_kwargs_expressions |= Q(**{filter_key: filter_value})
                elif not(filters_kwargs_expressions is None) and filter_joint_type == JointType.NOT:
                    filters_kwargs_expressions &= ~Q(**{filter_key: filter_value})
                else:
                    raise BaseException(f"Not implemented case: (filter_key, filter_joint_type, filter_value) {filter_key}, {filter_joint_type}, {filter_value}")
            if i <= size - 1:
                if total_expressions:
                    if expr == JointType.AND:
                        total_expressions &= filters_kwargs_expressions
                    elif expr == JointType.OR:
                        total_expressions |= filters_kwargs_expressions
                    elif expr == JointType.NOT:
                        total_expressions &= ~filters_kwargs_expressions
            if not total_expressions:
                total_expressions = filters_kwargs_expressions
                filters_kwargs_expressions = None

        queryset = queryset.filter(total_expressions)
        for filter_method in filter_methods:
            if _filters.function_allow_passing_info(filter_method):
                queryset = filter_method(queryset=queryset, info=info)

            else:
                queryset = filter_method(queryset=queryset)
    except Exception as e:
        print('$$$$$ ', e)
    return queryset.distinct()


## Replace build_filter_kwargs by our implementation that can handle GlobalID
#_filters.build_filter_kwargs = _build_filter_kwargs

# Replace apply filter for the generate logical combination of the filters
_filters.apply = _apply


@__dataclass_transform__(
    order_default=True,
    field_descriptors=(
        StrawberryField,
        _field,
        node,
        connection,
        field.field,
        field.node,
        field.connection,
    ),
)
def filter(  # noqa:A001
    model: Type[Model],
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    directives: Optional[Sequence[object]] = (),
    lookups: bool = False,
) -> Callable[[_T], _T]:
    return input(
        model,
        name=name,
        description=description,
        directives=directives,
        is_filter="lookups" if lookups else True,
        partial=True,
    )
