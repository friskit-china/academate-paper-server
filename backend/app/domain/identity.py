"""Brand-independent stable identifiers shared by persistence pipelines."""

import json
import uuid

# This randomly generated namespace identifies data-format version 1, not a product or vendor.
# It is an immutable persistence contract: changing it requires an explicit data migration.
CONTENT_ID_NAMESPACE_V1 = uuid.UUID("2e282026-685e-46b2-a37f-6b47a93869d5")


def stable_content_id(kind: str, *components: str) -> str:
    """Return a deterministic UUID from a typed, unambiguous sequence of components."""
    payload = json.dumps([kind, *components], ensure_ascii=False, separators=(",", ":"))
    return str(uuid.uuid5(CONTENT_ID_NAMESPACE_V1, payload))
