"""Read stored profile and knowledge. This does not call a model or build a prompt."""
import json

from .profile import public_profile


def read_context(store, workspace_db, source):
    """Return the authorized profile and user knowledge for one data source.

    Schema facts come only from a previously extracted catalog profile.
    Knowledge text remains user-provided and unverified. Callers must not
    insert this object into Selector, Decomposer, or Refiner prompts until a
    later change says so.
    """
    from .knowledge import list_knowledge

    record = store.get_profile_record(source.id)
    profile = None
    if record is not None:
        stored = json.loads(record['profile_json'])
        stored['extracted_at'] = record['extracted_at']
        profile = public_profile(source, stored)
    return {
        'datasource_id': source.id,
        'profile': profile,
        'knowledge': list_knowledge(store, workspace_db, source.id),
        'schema_facts': 'catalog',
        'business_definitions': 'user_provided_unverified',
    }
