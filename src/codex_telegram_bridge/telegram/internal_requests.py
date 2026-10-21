"""Compatibility fingerprints for Desktop's ephemeral generation requests.

These match INPUT templates from the installed Desktop, never reply JSON.
Callers must additionally require a readable profile DB, a missing thread,
one unambiguous task ID, and no explicit or stored source attribution.
Unknown or changed templates are preserved for structural diagnostics.
"""
from __future__ import annotations

import hashlib
import json
import re

MAX_REQUEST_LENGTH = 128 * 1024
AMBIENT_PREFIX = '# Overview\n\nGenerate 0 to 3 hyperpersonalized suggestions for what this user can do with Codex in '
SAFETY_PREFIX = 'You are an expert at upholding safety and compliance standards for Codex ambient suggestions.'
SAFETY_CANDIDATES = '\n\n# Ambient suggestion candidates\nHere are the ambient suggestion candidates to evaluate:\n\n'
# Codex Desktop 26.1013: exact fixed policy and output instructions around the
# variable candidates. Hashes avoid shipping the vendor's full policy text.
SAFETY_POLICY_SHA256 = '0ac1f49a08003c8463bb60852ed7d55df871e40b68eab42bc573f8e4b39520c1'
SAFETY_FOOTER_SHA256 = '995d217a623f5826138f8fd14fdb191d7778f812c8e3b7509863be5e88889bbe'


def latest_generation_input(payload):
    if payload.get('type') != 'agent-turn-complete' or payload.get('client') != 'Codex Desktop':
        return None
    inputs = payload.get('input-messages')
    # Both generators start a new ephemeral thread with exactly one request.
    if not isinstance(inputs, list) or len(inputs) != 1 or not isinstance(inputs[0], str):
        return None
    if len(inputs[0]) > MAX_REQUEST_LENGTH:
        return None
    return inputs[0].strip().replace('\r\n', '\n')


def _ambient_request(text):
    if not text.startswith(AMBIENT_PREFIX):
        return False
    location = text[len(AMBIENT_PREFIX):].split('\n', 1)[0]
    projectless = location == 'this Projectless task'
    if not projectless and not (location.startswith('this local project: ') and location[20:].strip()):
        return False
    sections = ['\n# Rules\n',
                '\nRecent Projectless Codex tasks with the same cwd:\n' if projectless else '\nRecent Codex tasks in this project:\n',
                '\nAvoid repeating these previously dismissed suggestions:\n',
                '\n# Response format\n\nEach suggestion must include:\n']
    if any(text.count(section) != 1 for section in sections):
        return False
    positions = [text.index(section) for section in sections]
    if positions != sorted(positions):
        return False
    response_format = text.partition(sections[-1])[2]
    if re.findall(r'^- ([A-Za-z]+): ', response_format, re.MULTILINE) != [
            'title', 'description', 'prompt', 'appId', 'pluginId']:
        return False
    ending = ('- write the prompt as something that should launch as a new Projectless Codex task'
              if projectless else '- write the prompt as something that should launch as a new Codex task in this project')
    return response_format.endswith('\n' + ending)


def _safety_request(text):
    if not text.startswith(SAFETY_PREFIX + '\n\n') or text.count(SAFETY_CANDIDATES) != 1:
        return False
    policy, _, rest = text.partition(SAFETY_CANDIDATES)
    if hashlib.sha256(policy.encode()).hexdigest() != SAFETY_POLICY_SHA256:
        return False
    if not rest.startswith('```\n') or rest.count('\n```\n\n# Output Format\n') != 1:
        return False
    candidates, _, tail = rest[4:].partition('\n```\n\n# Output Format\n')
    footer = '```\n\n# Output Format\n' + tail
    if hashlib.sha256(footer.encode()).hexdigest() != SAFETY_FOOTER_SHA256:
        return False
    lines = candidates.splitlines()
    if not lines or len(lines) % 5 or len(lines) > 15:
        return False
    names = ['- suggestion_id: ', '  title: ', '  description: ', '  prompt: ', '  app_id: ']
    for index in range(0, len(lines), 5):
        values = []
        for name, line in zip(names, lines[index:index+5]):
            if not line.startswith(name):
                return False
            try:
                values.append(json.loads(line[len(name):]))
            except (TypeError, ValueError):
                return False
        if values[0] != f'suggestion-{index // 5 + 1}':
            return False
        if any(not isinstance(value, str) or not value.strip() for value in values[1:4]):
            return False
        if values[4] is not None and not isinstance(values[4], str):
            return False
    return True


def generation_request_source(payload):
    text = latest_generation_input(payload)
    if text is None:
        return None
    if _ambient_request(text):
        return 'ambient_suggestions'
    if _safety_request(text):
        return 'ambient_suggestion_safety'
    return None
