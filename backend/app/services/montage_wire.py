"""Only lossless wire repairs; no inferred continuity or guessed merge verdicts."""
import copy
import json
import re


def parse_montage_payload(raw):
    """Accept only a complete object or complete array; never harvest fragments."""
    value = raw.strip()
    if value.startswith('```'):
        value = re.sub(r'^```(?:json)?\s*|\s*```$', '', value)
    payload = json.loads(value)
    if isinstance(payload, list):
        return {'relations': payload}, ['complete-root-array-to-relations']
    if not isinstance(payload, dict):
        raise ValueError('跨镜输出必须为JSON对象或完整关系数组')
    return payload, []


def repair_context_evidence(relation, shots):
    """Restore references ONLY from verbatim saved facts for asserted values.

    No new identity/location, continuity verdict or state is inferred. This avoids
    asking the model to repeatedly retype a fact the backend already possesses.
    """
    from app.schemas import MontageContextEvidence
    from app.services.event_sequences import _supports
    fields = {'subject': ('primary_subject', 'people', 'initial_state', 'final_state', 'interaction_target'),
              'prop': ('objects', 'interaction_target', 'initial_state', 'final_state'), 'location': ('scene',)}
    saved = []
    for shot in shots:
        for kind, names in fields.items():
            for name in names:
                owner = shot.visual_facts if name in ('people', 'objects', 'scene') else shot.action_details
                value = getattr(owner, name)
                for text in value if isinstance(value, list) else [value]:
                    if isinstance(text, str) and text:
                        saved.append((kind, shot.id, name, text))
    refs, repairs = [], []
    for ref in relation.context_evidence:
        exact = [entry for entry in saved if entry[:3] == (ref.kind, ref.shot_id, ref.source_field) and ref.quote in entry[3]]
        if exact:
            refs.append(ref)
            continue
        # Bad redundant references cannot discard an otherwise directly literal
        # context: replace them with the actual saved source, retaining an audit.
        literal = [entry for entry in saved if entry[0] == ref.kind and _supports(ref.value, [entry[3]])]
        if literal:
            kind, identifier, name, text = literal[0]
            refs.append(MontageContextEvidence(kind=kind, value=ref.value, shot_id=identifier, source_field=name, quote=text))
            repairs.append('context-reference-restored-from-literal-saved-value')
        else:
            refs.append(ref)  # Unsupported aliases still fail strict validation.
    for kind, values in (('subject', relation.shared_subjects), ('prop', [relation.shared_prop]), ('location', [relation.shared_location])):
        for value in values:
            if not value or value == '无' or any(ref.kind == kind and ref.value == value for ref in refs):
                continue
            literal = [entry for entry in saved if entry[0] == kind and _supports(value, [entry[3]])]
            if literal:
                _, identifier, name, text = literal[0]
                refs.append(MontageContextEvidence(kind=kind, value=value, shot_id=identifier, source_field=name, quote=text))
                repairs.append('context-reference-attached-from-literal-saved-value')
    return relation.model_copy(update={'context_evidence': refs}), repairs


def normalize_relation(value):
    if not isinstance(value, dict):
        raise ValueError('关系必须为对象')
    result, repairs = copy.deepcopy(value), []
    def sequence(owner, key, prefix=''):
        if isinstance(owner.get(key), str):
            owner[key] = [owner[key]] if owner[key].strip() else []
            repairs.append(prefix + key + ': string-to-list')
        elif owner.get(key, 'missing') is None:
            owner[key] = []
            repairs.append(prefix + key + ': null-to-empty-list')
    for key in ('evidence', 'shared_subjects'):
        sequence(result, key)
    for key in ('event_goal', 'shared_prop', 'shared_location', 'reason', 'possible_narrative_intent'):
        if key in result and result[key] is None:
            result[key] = ''
            repairs.append(key + ': null-to-unknown')
        elif isinstance(result.get(key), list):
            # Never choose one prop from several: that would change the semantics.
            if len(result[key]) == 1 and isinstance(result[key][0], str):
                result[key] = result[key][0]
                repairs.append(key + ': singleton-list-to-string')
    if isinstance(result.get('state_linkage'), dict):
        sequence(result['state_linkage'], 'evidence', 'state_linkage.')
    return result, repairs
