"""Small typed MCP forms; URL and device verification remain in Desktop."""
from __future__ import annotations
from codex_telegram_bridge.i18n import tr as _t

import math
from typing import Any


def form_questions(params: dict[str, Any]) -> list[dict[str, Any]] | None:
    if params.get('mode') not in {'form', 'openai/form', 'openaiForm'}:
        return None
    schema = params.get('requestedSchema')
    if not isinstance(schema, dict) or schema.get('type') != 'object':
        return None
    if set(schema) - {'type', 'properties', 'required', 'additionalProperties', 'title', 'description', '$schema'}:
        return None
    if 'additionalProperties' in schema and not isinstance(schema['additionalProperties'], bool):
        return None
    properties = schema.get('properties')
    required = schema.get('required', [])
    if (not isinstance(properties, dict) or not 1 <= len(properties) <= 8
            or not isinstance(required, list) or any(key not in properties for key in required)):
        return None
    questions = []
    for key, field in properties.items():
        if not isinstance(key, str) or not isinstance(field, dict):
            return None
        kind = field.get('type')
        if kind not in {'string', 'integer', 'number', 'boolean'} or field.get('format') in {'password', 'secret'}:
            return None
        # No silent partial validation of nested/custom form constraints.
        if set(field) - {'type', 'title', 'description', 'enum', 'minLength', 'maxLength', 'minimum', 'maximum', 'default'}:
            return None
        choices = field.get('enum')
        if kind == 'boolean' and choices is None:
            choices = [True, False]
        if choices is not None and (not isinstance(choices, list) or not 1 <= len(choices) <= 12):
            return None
        options = ([{'label': str(value), 'description': ''} for value in choices]
                   if choices is not None else None)
        questions.append({'id': key, 'header': str(field.get('title') or key),
                          'question': str(field.get('description') or params.get('message') or key),
                          'options': options, 'required': key in required})
    return questions


def form_content(params: dict[str, Any], answers: dict[str, Any]) -> dict[str, Any]:
    properties = params['requestedSchema']['properties']
    result = {}
    for key, field in properties.items():
        if key not in answers:
            if key in params['requestedSchema'].get('required', []):
                raise ValueError(_t('字段 {p0} 必须填写。', p0=format(key, '')))
            # JSON Schema defaults are annotations, not authorization to invent
            # an answer. Skipping leaves optional fields absent from content.
            continue
        answer = str(answers[key]['answers'][0])
        kind = field['type']
        try:
            if kind == 'boolean':
                if answer not in {'True', 'False', 'true', 'false'}:
                    raise ValueError()
                value = answer.lower() == 'true'
            elif kind == 'integer':
                value = int(answer)
            elif kind == 'number':
                value = float(answer)
                if not math.isfinite(value):
                    raise ValueError()
            else:
                value = answer
            if 'enum' in field and value not in field['enum']:
                raise ValueError()
            if kind == 'string' and not field.get('minLength', 0) <= len(value) <= field.get('maxLength', 20000):
                raise ValueError()
            if kind in {'integer', 'number'} and not field.get('minimum', -math.inf) <= value <= field.get('maximum', math.inf):
                raise ValueError()
        except (TypeError, ValueError, OverflowError):
            raise ValueError(_t('字段 {p0} 的回答不符合表单约束。', p0=format(key, ''))) from None
        result[key] = value
    return result
