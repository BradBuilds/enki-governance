"""Substrate algebra — the runtime implementation of collapse ⊕ and orthogonal ⊗.

Public surface:
  properties.get_by_key(key)                  → lookup a property concept
  properties.is_applicable(prop, node_type)   → check type compatibility
  values.find_or_create(prop, label, tier, parent) → resolve or admit a value
  values.walk_chain(value_id)                 → walk from value to root
  values.is_ancestor(ancestor_id, descendant_id) → chain membership test
  collapse.admit_assertion(...)               → admit a typed property assertion,
                                                running the collapse operator
                                                against existing assertions for
                                                the same (node, property)

These are the building blocks. Extractors call collapse.admit_assertion to
write a property fact into the substrate; everything else (refinement,
contradiction detection, attestation chains) happens inside.
"""
